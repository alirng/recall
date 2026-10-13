"""MemoryCode (Rakotonirina et al., ACL 2025): does recall keep up with coding conventions a
developer sets, and later changes, over many sessions?

Each of the benchmark's 360 histories is 1 to 100 synthetic sessions between a mentor and a new
engineer. The mentor introduces coding conventions ("start function names with 'y_'"), updates
some of them later ("now start them with 'c_'"), and talks about other things in between. Every
session is labelled: which conventions it adds or updates, and which sessions are filler. The
history ends with coding requests whose code must follow the latest form of every convention,
checked by regexes over the code's syntax tree.

Here each session becomes a chat, the mentor the user and the mentee the agent, one session a
day ending yesterday, in a project folder named after the mentor's company.

  recall eval memorycode --data <download> [--split train|test|all] [--limit N] [--case ID]
      [--model PROVIDER:NAME] [--repeats N] [--fresh]          extraction (M4)
  recall eval memorycode --data <download> --agent [MODEL] --arm none|tools|notes|full
      [--notes RUN_DIR] [--queries N] [--hybrid]               agent (M2, M3)

--data is a folder holding the dataset's parquet file (huggingface.co/datasets/
CohereLabsCommunity/memorycode); reading it needs pyarrow (`uv run --with pyarrow`).

Extraction runs recall's production path on each history: extract.plan / run_batch over the
sessions (as dream._read), then consolidation one session a night, oldest first (as
dream._fold_notes on successive nights), into an empty notes folder. The final active notes are
scored against the session labels:
  recall          each convention, in its latest form, is stated by an active note. Exact match
                  on the convention's quoted token ('c_', '@timer') where it has one; otherwise a
                  Claude Sonnet judge decides. The judge also grades the token conventions, and
                  its agreement with the exact match is reported as a check on the grader.
  stale           an earlier, replaced form of a convention is still stated as current (judge)
  updates         a changed convention has its latest form stated and no replaced form current
  filler_notes    share of active notes learned only from filler sessions
Each history's notes are kept in the run folder (notes/<id>/r<repeat>/) for --arm notes.

Agent mode gives the history's final coding requests to Claude Code (evals_agent.run_agent) and
grades its code with the benchmark's checks, ported from the authors' evaluate_model_output.py
and extract_objects.py: per request the share of conventions followed among those whose object
the code contains, averaged per history. Arms:
  none   no tools and no history: the no-memory baseline
  tools  recall's MCP tools over a recall home holding only this history's chats
  notes  tools, plus the notes an extraction run wrote (--notes RUN_DIR)
  hook   as notes, and the system prompt also gets what the session-start hook injects for the
         history's folder (memory.context.session_context over those notes), as in production
  full   the whole history in the prompt and no tools, as the paper's history setting; every
         history fits (the longest is about 72k tokens)
The paper asks every request of a history; --queries samples N of them per history (seeded), so
a run takes minutes. Every request's code is checked against every convention, as in the paper.

Histories with ids up to 210 (1 to 15 sessions) are short and the rest (20 to 100) long, as the
authors' evaluate_model_output.py splits them. The train/test split is computed here: seeded
60/40 over histories, stratified by session count. --limit takes a fixed sample spread evenly
over session counts. Test histories are only reported in aggregate.
"""

import ast
import glob
import json
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from . import evals_harness as h
from .chats import store
from .chats.transcript import Message, Session

SHORT_MAX_ID = 210  # evaluate_model_output.py: dialogues 1-210 are "short history", the rest "long"
TRAIN_SHARE = 0.6
AGENT = "claude"  # the chats read as Claude Code chats
TURN_SECONDS = 40
FULL_HISTORY_MAX_TOKENS = 100_000  # the full arm runs only where the history fits (chars / 4)
TURN = re.compile(r"(?m)^[ \t]*([A-Z][\w.'-]*(?:[ -][A-Z][\w.'-]*)?):[ \t]")
QUOTED = re.compile(r"'([^']+)'")
COMPANY = re.compile(r"\b(?:at|called) ([A-Z][A-Z0-9&]{2,})\b")
CAPS = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")
ACRONYMS = frozenset(
    "API SQL HTTP HTTPS JSON CSV PEP OOP URL IDE AWS TDD REST XML HTML CSS GPU CPU RAM SSH VPN DNS TCP ISP KPI ETL NLP "
    "SDK CLI MVP UML YAML".split()
)


# --- the data -------------------------------------------------------------------------


@dataclass
class Convention:
    """One coding instruction across its updates: forms[i] is the text of update i."""

    id: int
    object: str
    regex: object
    forms: list[str] = field(default_factory=list)
    sessions: list[int] = field(default_factory=list)

    @property
    def latest(self) -> str:
        return self.forms[-1]

    @property
    def replaced(self) -> list[str]:
        out = []
        for f in self.forms[:-1]:
            if f != self.latest and f not in out:
                out.append(f)
        return out


@dataclass
class History:
    id: int
    sessions: list[dict]  # the dataset's session records, in order
    mentor: str
    mentee: str
    company: str | None
    conventions: list[Convention]
    filler: set[int]  # sessions with no coding instruction
    split: str = "train"

    @property
    def length(self) -> str:
        return "short" if self.id <= SHORT_MAX_ID else "long"

    @property
    def folder(self) -> str:
        return f"/work/{(self.company or 'codebase').lower()}"

    def chat_id(self, i: int) -> str:
        return f"{AGENT}:mc{self.id:03d}-s{i + 1:03d}"

    def tags(self) -> list[str]:
        return [self.length, f"{len(self.sessions)}-sessions"]


def load(data: str) -> list[History]:
    files = sorted(glob.glob(os.path.join(data, "*.parquet")) + glob.glob(os.path.join(data, "data", "*.parquet")))
    if not files:
        sys.exit(f"no parquet file in {data}: download huggingface.co/datasets/CohereLabsCommunity/memorycode")
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("recall eval memorycode reads parquet with pyarrow: run it as `uv run --with pyarrow recall eval ...`")
    rows = [r for f in files for r in pq.read_table(f).to_pylist()]
    histories = [_history(r) for r in sorted(rows, key=lambda r: r["id"])]
    splits = assign_splits(histories)
    for x in histories:
        x.split = splits[x.id]
    return histories


def _history(r: dict) -> History:
    sessions = json.loads(r["sessions"])
    ids = json.loads(r["instruction_ids"])
    conventions: dict[int, Convention] = {}
    filler = set()
    for i, s in enumerate(sessions):
        coding = [top for t, top in zip(s["type"], s["topic"], strict=True) if t.startswith("instruction")]
        if not coding:
            filler.add(i)
            continue
        for top, (obj, regex), (pid, _update) in zip(coding, s["session_regex"], ids[i], strict=True):
            c = conventions.setdefault(pid, Convention(pid, obj, regex))
            c.object, c.regex = obj, regex
            c.forms.append(top)
            c.sessions.append(i)
    mentor, mentee = _speakers(sessions)
    company = next((m.group(1) for s in sessions[:3] for m in COMPANY.finditer(s["text"])), None)
    if company is None:  # else the capitalised name said most often, if said twice
        caps = Counter(w for s in sessions for w in CAPS.findall(s["text"]) if w not in ACRONYMS).most_common(1)
        company = caps[0][0] if caps and caps[0][1] >= 2 else None
    return History(r["id"], sessions, mentor, mentee, company, list(conventions.values()), filler)


def _speakers(sessions: list[dict]) -> tuple[str, str]:
    """(mentor, mentee). The mentor is whoever first says the quoted token of a coding
    instruction; with no quoted token, whoever opens the most sessions (the mentor leads)."""
    names: Counter = Counter()
    openers: Counter = Counter()
    says: Counter = Counter()
    for s in sessions:
        turns = list(TURN.finditer(s["text"]))
        names.update(m.group(1) for m in turns)
        if turns:
            openers[turns[0].group(1)] += 1
        for t, top in zip(s["type"], s["topic"], strict=True):
            if not t.startswith("instruction"):
                continue
            for token in QUOTED.findall(top):
                for k, m in enumerate(turns):
                    end = turns[k + 1].start() if k + 1 < len(turns) else len(s["text"])
                    if token in s["text"][m.end() : end]:
                        says[m.group(1)] += 1
                        break
    two = [n for n, _ in names.most_common(2)] + ["Mentor", "Mentee"]
    a, b = two[0], two[1]
    mentor = a if (says[a], openers[a]) >= (says[b], openers[b]) else b
    return mentor, b if mentor == a else a


def assign_splits(histories: list[History]) -> dict[int, str]:
    """Seeded 60/40 train/test split, stratified by session count (30 histories each)."""
    from .evals_swecb import assign_splits as by_group

    tasks = [{"id": f"{x.id:03d}", "repo": f"{len(x.sessions):03d}"} for x in histories]
    return {int(k): v for k, v in by_group(tasks).items()}


def select(histories: list[History], split: str, ids: list[str] | None, limit: int | None) -> list[History]:
    picked = [x for x in histories if split == "all" or x.split == split]
    if ids:
        picked = [x for x in picked if str(x.id) in ids]
    if limit and len(picked) > limit:
        # an even spread over session counts: one history from each, round robin, seeded
        groups: dict[int, list[History]] = {}
        for x in picked:
            groups.setdefault(len(x.sessions), []).append(x)
        rng = random.Random(h.SEED)
        for g in groups.values():
            rng.shuffle(g)
        keep: list[History] = []
        while len(keep) < limit:
            for n in sorted(groups):
                if groups[n] and len(keep) < limit:
                    keep.append(groups[n].pop())
        picked = sorted(keep, key=lambda x: x.id)
    if not picked:
        sys.exit(f"no MemoryCode histories in the {split} split")
    return picked


def chats(x: History, end: datetime | None = None) -> list[Session]:
    """The history as chats: one session a day at 10:00, the last one yesterday."""
    end = end or datetime.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    out = []
    for i, s in enumerate(x.sessions):
        start = (end - timedelta(days=len(x.sessions) - 1 - i)).timestamp()
        messages = []
        for k, (speaker, text) in enumerate(_turns(s["text"])):
            role = "user" if _who(speaker, x) == x.mentor else "assistant"
            messages.append(Message(role, text, start + k * TURN_SECONDS))
        last = messages[-1].ts if messages else start
        native = x.chat_id(i).split(":", 1)[1]
        out.append(Session(AGENT, native, "memorycode", x.folder, None, None, start, last, messages=messages))
    return out


def _turns(text: str) -> list[tuple[str, str]]:
    marks = list(TURN.finditer(text))
    if not marks:
        return [("", text.strip())]
    out = []
    if text[: marks[0].start()].strip():
        out.append(("", text[: marks[0].start()].strip()))
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        out.append((m.group(1), text[m.end() : end].strip()))
    return [(s, t) for s, t in out if t]


def _who(speaker: str, x: History) -> str:
    """A speaker label as one of the two people; the generated text has typos ("Ethon", "Y.S.")."""
    if speaker in (x.mentor, x.mentee):
        return speaker
    if not speaker:
        return x.mentor
    firsts = [n for n in (x.mentor, x.mentee) if n[:1].lower() == speaker[:1].lower()]
    if len(firsts) == 1:
        return firsts[0]
    from difflib import SequenceMatcher

    return max((x.mentor, x.mentee), key=lambda n: SequenceMatcher(None, n.lower(), speaker.lower()).ratio())


def index(sessions: list[Session], path: str, embedder=None, cache=None):
    con = store.connect(path)
    for s in sessions:
        store.upsert(con, s)
    con.commit()
    if embedder is not None:
        from . import embeddings

        embeddings.index_messages(con, embedder, cache=cache)
    return con


# --- the command --------------------------------------------------------------------------


def from_args(a) -> dict:
    """`recall eval memorycode`, from the CLI's arguments."""
    if not a.data:
        sys.exit("recall eval memorycode needs --data: the folder holding the dataset's parquet file")
    histories = select(load(os.path.expanduser(a.data)), a.split, a.case, a.limit)
    settings = {
        "data": a.data,
        "histories": len(histories),
        "short": sum(x.length == "short" for x in histories),
        "long": sum(x.length == "long" for x in histories),
        "repeats": a.repeats,
        "workers": a.workers,
        "note": a.note or "",
    }
    if a.agent is None:
        run = h.Run("memorycode-extract", a.split, settings, a.who, a.runs_dir)
        return run_extract(histories, run, a.model, a.judge or h.JUDGE, a.fresh, a.repeats, a.workers, a.against)
    from . import evals_agent

    model = (a.agent or evals_agent.DEFAULT_MODEL).removeprefix("claude:")
    if a.arm in ("notes", "hook") and not a.notes:
        sys.exit(f"--arm {a.arm} needs --notes: an extraction run folder (recall eval memorycode without --agent)")
    embedder = cache = None
    if a.hybrid and a.arm in ("tools", "notes", "hook"):
        import sqlite3

        from . import embeddings, paths

        embedder = embeddings.configured()
        if embedder is None:
            sys.exit("--hybrid needs an [embeddings] table in config.toml and the embeddings extra")
        cache = sqlite3.connect(os.path.join(paths.HOME, "eval-vectors.db"), timeout=60)
    search = ("-hybrid" if embedder else "-keyword") if a.arm in ("tools", "notes", "hook") else ""
    settings.update(
        model=model,
        arm=a.arm,
        notes_run=a.notes,
        queries=a.queries,
        timeout=a.timeout,
        embedder=embedder.model if embedder else None,
    )
    run = h.Run(f"memorycode-agent-{a.arm}{search}-{model}", a.split, settings, a.who, a.runs_dir)
    return run_agent(
        histories, run, model, a.arm, a.notes, a.queries, a.repeats, a.workers, a.timeout, embedder, cache, a.against
    )


# --- extraction and consolidation (M4) -------------------------------------------------------


def run_extract(
    histories: list[History],
    run: h.Run,
    model: str | None = None,
    judge: str = h.JUDGE,
    fresh: bool = False,
    repeats: int = 1,
    workers: int = 4,
    against: str | None = None,
) -> dict:
    from .llm import Client, from_spec

    provider = from_spec(model, config.load()["model"])
    if problem := provider.check():
        sys.exit(problem)
    if h.same_model(provider.label.split("@")[0], judge):
        sys.exit(f"the judge ({judge}) must differ from the extraction model ({provider.label}): pass --judge")
    if repeats > 1 and not fresh:
        print("--repeats implies --fresh: cached answers would repeat the first run", file=sys.stderr)
        fresh = True
    client = Client(provider, cache=not fresh)
    grader = h.judge_client(judge)
    run.meta.update(model=provider.label, judge=judge, judge_version=JUDGE_VERSION, fresh=fresh)
    jobs = [(x, r) for r in range(1, repeats + 1) for x in histories]
    done, lock = [0], threading.Lock()

    def one(job):
        x, repeat = job
        started = time.time()
        row, transcript = extract_history(x, repeat, run, client, grader)
        row["seconds"] = round(time.time() - started, 1)
        if repeats > 1:
            row["repeat"] = repeat
        case = {"id": str(x.id), "split": x.split, "tags": x.tags()}
        run.record(case, row, transcript)
        with lock:
            done[0] += 1
            label = f"history {x.id} r{repeat}" if x.split != "test" else "test history"
            print(f"[{done[0]}/{len(jobs)}] {label}: {row['status']} {_short(row)}", file=sys.stderr)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, jobs))

    scored = [r for r in run.rows if r["status"] == h.OK]
    pooled = _pool(scored)
    p = list(pooled.values())
    metrics = {
        "recall": h.ratio_ci([(x["found"], x["conventions"]) for x in p]),
        "stale": h.ratio_ci([(x["stale"], x["replaced"]) for x in p]),
        "updates": h.ratio_ci([(x["updates_ok"], x["updated"]) for x in p]),
        "filler_notes": h.ratio_ci([(x["notes_filler"], x["notes"]) for x in p]),
        "on_convention": h.ratio_ci([(x["notes_on_convention"], x["notes"]) for x in p]),
    }
    by_length = {}
    for length in ("short", "long"):
        part = [x for x in p if length in x["tags"]]
        by_length[length] = {
            "recall": h.ratio_ci([(x["found"], x["conventions"]) for x in part]),
            "stale": h.ratio_ci([(x["stale"], x["replaced"]) for x in part]),
        }
    summary = {
        "model": provider.label,
        "judge": judge,
        "judge_version": JUDGE_VERSION,
        "repeats": repeats,
        **metrics,
        "by_length": by_length,
        "grader": {
            "exact_vs_judge_recall": _agreement(scored, "agree", "exact_n"),
            "exact_vs_judge_stale": _agreement(scored, "stale_agree", "stale_exact_n"),
            "recall_judge_only": h.ratio_ci([(x["found_judge"], x["conventions"]) for x in p]),
            "recall_exact_only": h.ratio_ci([(x["found_exact"], x["exact_n"]) for x in p]),
        },
        "notes_per_history": h.spread([r["notes"] for r in scored]),
        "candidates": {
            "per_history": h.spread([r["candidates"] for r in scored]),
            "from_filler_sessions": sum(r["candidates_filler"] for r in scored),
            "total": sum(r["candidates"] for r in scored),
        },
        "seconds": h.spread([r["seconds"] for r in scored]),
        "plumbing": h.plumbing(run.rows),
        "model_calls": {"calls": client.calls, "tokens_in": client.tokens_in, "tokens_out": client.tokens_out},
        "judge_calls": grader.calls,
    }
    if repeats > 1:
        summary["variance"] = _variance(scored)
    summary["warnings"] = h.headroom({"recall": metrics["recall"]})
    if against:
        summary["against"] = {"run": against}
        summary["warnings"] += h.mismatch(against, run.meta)
        for metric, num, den in (("recall", "found", "conventions"), ("stale", "stale", "replaced")):
            mine = {k: x[num] / x[den] for k, x in pooled.items() if x[den]}
            value = lambda r, n=num, d=den: r[n] / r[d] if r.get(d) else None  # noqa: E731
            summary["against"][metric] = h.paired(mine, h.against(against, value))
    headline = (
        f"recall={h.fmt(metrics['recall'])} stale={h.fmt(metrics['stale'])} updates={metrics['updates']['mean']} "
        f"filler_notes={metrics['filler_notes']['mean']} short={by_length['short']['recall']['mean']} "
        f"long={by_length['long']['recall']['mean']} model={provider.label}"
    )
    if repeats > 1:
        headline += f" recall_by_repeat={summary['variance']['recall_by_repeat']}"
    if against:
        headline += f" Δrecall={h.fmt(summary['against']['recall'], signed=True)}"
    columns = [
        ("sessions", "sessions"),
        ("recall", "recall"),
        ("conventions", "conventions"),
        ("stale", "stale"),
        ("replaced", "replaced"),
        ("notes", "notes"),
        ("filler notes", "notes_filler"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


def extract_history(x: History, repeat: int, run: h.Run, client, grader) -> tuple[dict, dict]:
    """Extract and fold one history into empty notes; returns (row, transcript)."""
    from . import evals_memory
    from .llm import FatalError, LLMError

    row = {"status": h.OK, "detail": None, "sessions": len(x.sessions), "length": x.length}
    transcript: dict = {"mentor": x.mentor, "mentee": x.mentee, "company": x.company, "conventions": _gold(x)}
    tmp = tempfile.mkdtemp(prefix="recall-eval-memorycode-")
    try:
        db = index(chats(x), os.path.join(tmp, "recall.db"))
        rows = db.execute("select * from sessions order by started_at").fetchall()
        messages = {
            s["id"]: db.execute(
                "select seq, role, text from messages where session_id=? order by seq", (s["id"],)
            ).fetchall()
            for s in rows
        }
        step = "extract"
        try:
            found, failed = evals_memory.read(client, rows, messages)
            if failed:
                row.update(status="api_error", detail=json.dumps(failed)[:300])
                return row, transcript
            step = "fold"
            notes = fold(db, rows, found, client, os.path.join(tmp, "memory"))
            step = "judge"
            verdict = grade(grader, x, notes["active"]) if notes["active"] else None
        except FatalError:
            raise
        except LLMError as e:
            row.update(status="judge_error" if step == "judge" else "api_error", detail=f"{step}: {str(e)[:300]}")
            return row, transcript
        db.close()
        keep = os.path.join(run.dir, "notes", str(x.id), f"r{repeat}")
        shutil.copytree(os.path.join(tmp, "memory"), keep, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    index_of = {x.chat_id(i): i for i in range(len(x.sessions))}
    candidates = {index_of[cid]: c for cid, (c, _) in found.items()}
    transcript.update(
        candidates={str(i + 1): c for i, c in sorted(candidates.items()) if c},
        notes=notes,
        judge=verdict,
        notes_folder=keep,
    )
    row.update(score(x, notes, verdict, index_of))
    row.update(
        candidates=sum(len(c) for c in candidates.values()),
        candidates_filler=sum(len(c) for i, c in candidates.items() if i in x.filler),
        retired=len(notes["retired"]),
    )
    return row, transcript


def fold(db, rows, found: dict, client, root: str) -> dict:
    """Fold each session's candidates into empty notes, oldest first, one session a night, as
    dream._fold_notes does on successive nights. A night with no candidates changes nothing."""
    from .memory import consolidate, extract, links, routing
    from .memory.notes import NoteStore

    notes_store = NoteStore(root)
    extract.ensure_tables(db)
    log: list[str] = []
    stats: Counter = Counter()
    for s in rows:
        candidates, digest_hash = found.get(s["id"], ([], None))
        extract.store(db, s, "done", candidates, digest_hash, client.provider.label)
        db.commit()
        if not candidates:
            continue
        notes = notes_store.load()
        notes_store.reconcile(notes)
        routing.unify_moved(db)
        routing.reroute_mentions(db)
        db.commit()
        stats.update(consolidate.run(db, client, notes_store, notes, log=log.append))
        links.relink(notes_store, notes)
        notes_store.compile(notes)
        notes_store.reconcile(notes)
    notes = notes_store.load()
    stats["expired"] += consolidate.expire(notes_store, notes)  # the morning after the last session
    notes_store.compile(notes)
    from .evals_memory import _note

    every = notes_store.load(include_archived=True).values()
    return {
        "active": [_note(n) for n in every if n.status == "active"],
        "retired": [_note(n) for n in every if n.status != "active"],
        "stats": dict(stats),
        "log": log,
    }


# --- scoring the notes ---------------------------------------------------------------------


def score(x: History, notes: dict, verdict: dict | None, index_of: dict[str, int]) -> dict:
    active = notes["active"]
    texts = [f"{n['title']}\n{n['body']}" for n in active]
    judged = {e["convention"]: e["note"] for e in (verdict or {}).get("conventions", [])}
    as_current = {e["replaced"]: e["as_current"] for e in (verdict or {}).get("replaced", [])}
    about = {e["note"]: e["conventions"] for e in (verdict or {}).get("notes", [])}
    found = found_judge = found_exact = exact_n = agree = 0
    stale = stale_exact = stale_exact_n = stale_agree = updated = updates_ok = 0
    k = 0  # replaced forms are numbered across conventions, as grade() numbers them
    for i, c in enumerate(x.conventions, 1):
        judge_hit = 1 <= judged.get(i, 0) <= len(active)
        found_judge += judge_hit
        tokens = QUOTED.findall(c.latest)
        if tokens:
            exact_hit = any(_states(t, tokens) for t in texts)
            exact_n += 1
            found_exact += exact_hit
            agree += exact_hit == judge_hit
            found += exact_hit
        else:
            found += judge_hit
            exact_hit = judge_hit
        any_stale = False
        for form in c.replaced:
            k += 1
            judged_stale = bool(as_current.get(k, False))
            stale += judged_stale
            any_stale |= judged_stale
            old = [t for t in QUOTED.findall(form) if t not in tokens]
            if old and tokens:  # a note with the old token and not the new one states the old as current
                hit = any(_states(t, old) and not _states(t, tokens) for t in texts)
                stale_exact_n += 1
                stale_exact += hit
                stale_agree += hit == judged_stale
        if c.replaced:
            updated += 1
            updates_ok += exact_hit and not any_stale
    sources = [{index_of.get(s) for s in n["sources"]} - {None} for n in active]
    n_conv = len(x.conventions)
    n_replaced = sum(len(c.replaced) for c in x.conventions)
    return {
        "conventions": n_conv,
        "found": found,
        "found_judge": found_judge,
        "found_exact": found_exact,
        "exact_n": exact_n,
        "agree": agree,
        "replaced": n_replaced,
        "stale": stale,
        "stale_exact": stale_exact,
        "stale_exact_n": stale_exact_n,
        "stale_agree": stale_agree,
        "updated": updated,
        "updates_ok": updates_ok,
        "notes": len(active),
        "notes_filler": sum(1 for s in sources if s and s <= x.filler),
        "notes_on_convention": sum(1 for i in range(1, len(active) + 1) if about.get(i)),
        "recall": round(found / n_conv, 3) if n_conv else None,
        "stale_rate": round(stale / n_replaced, 3) if n_replaced else None,
    }


def _states(text: str, tokens: list[str]) -> bool:
    """Does a note's text carry every token, as a token rather than inside a longer word? 'e_'
    must not match "type_hint"; '_i' must not match "_id"."""
    low = text.lower()
    for t in tokens:
        t = t.lower().lstrip("@")
        before = r"(?<![a-z0-9])" if t[:1].isalnum() else ""
        after = r"(?![a-z0-9])" if t[-1:].isalnum() else ""
        if not re.search(before + re.escape(t) + after, low):
            return False
    return True


# Bump when the judge's prompt or rubric changes: scores graded by different versions do not compare.
JUDGE_VERSION = "memorycode-judge-v1"
JUDGE_SYSTEM = (
    "You grade the memory notes that a memory system wrote for a developer's coding agent after reading "
    "a series of chats. In the chats the developer set coding conventions for the agent, and later changed "
    "some of them. Agents read the active notes as what holds now. Judge meaning, not wording."
)
_PICK = {"type": "integer", "minimum": 0}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["conventions", "replaced", "notes"],
    "properties": {
        "conventions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["convention", "reasoning", "note"],
                "properties": {"convention": _PICK, "reasoning": {"type": "string"}, "note": _PICK},
            },
        },
        "replaced": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["replaced", "reasoning", "as_current"],
                "properties": {"replaced": _PICK, "reasoning": {"type": "string"}, "as_current": {"type": "boolean"}},
            },
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["note", "conventions"],
                "properties": {"note": _PICK, "conventions": {"type": "array", "items": _PICK}},
            },
        },
    },
}


def grade(client, x: History, active: list[dict]) -> dict:
    """Which active note states each convention's latest form, whether any states a replaced
    form as current, and which conventions each note is about."""
    conventions = "\n".join(f"{i}. ({c.object}) {c.latest}" for i, c in enumerate(x.conventions, 1))
    replaced, k = [], 0
    for i, c in enumerate(x.conventions, 1):
        for form in c.replaced:
            k += 1
            replaced.append(f"{k}. (an earlier form of convention {i}) {form}")
    notes = "\n".join(f"[{i}] {n['title']}: {' '.join(n['body'].split())}" for i, n in enumerate(active, 1))
    prompt = (
        f"Coding conventions the developer set, each in its latest form (what holds now):\n{conventions}\n\n"
        f"Earlier forms the developer later replaced (they no longer hold):\n{chr(10).join(replaced) or '(none)'}\n\n"
        f"Active memory notes after the last chat:\n{notes}\n\n"
        "For each convention, give the number of the active note that states its latest form as a rule that "
        "holds now (the same meaning: an agent following the note would write code that obeys it; it may say "
        "more), or 0 if none does. For each earlier form, as_current: does some active note still present it as "
        "a rule to follow now? Stating it as history that was replaced is fine. For each note, list the numbers "
        "of the conventions (latest forms) it states, or an empty list. Cover every convention, every earlier "
        "form and every note, by number, with one short sentence of reasoning each."
    )
    verdict = client.json("memorycode-judge", JUDGE_SYSTEM, prompt, SCHEMA)
    covered = {e["convention"] for e in verdict["conventions"]}
    verdict["conventions"] += [
        {"convention": i, "reasoning": "(not judged)", "note": 0}
        for i in range(1, len(x.conventions) + 1)
        if i not in covered
    ]
    return verdict


def _gold(x: History) -> list[dict]:
    return [
        {"id": c.id, "object": c.object, "regex": c.regex, "forms": c.forms, "sessions": [i + 1 for i in c.sessions]}
        for c in x.conventions
    ]


def _short(row: dict) -> str:
    if row["status"] != h.OK:
        return row.get("detail") or ""
    if "conventions" in row:
        return (
            f"recall {row['found']}/{row['conventions']}, stale {row['stale']}/{row['replaced']}, "
            f"{row['notes']} notes ({row['notes_filler']} filler)"
        )
    return f"score {row.get('score')}"


def _pool(rows: list[dict]) -> dict[str, dict]:
    """Per history, sums over repeats, so each history counts once in a bootstrap."""
    keys = (
        "found",
        "conventions",
        "found_judge",
        "found_exact",
        "exact_n",
        "stale",
        "replaced",
        "updates_ok",
        "updated",
        "notes",
        "notes_filler",
        "notes_on_convention",
    )
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(r["id"], {**dict.fromkeys(keys, 0), "tags": r["tags"]})
        for k in keys:
            p[k] += r[k]
    return out


def _agreement(rows: list[dict], agree: str, n: str) -> dict:
    rows = [r for r in rows if r["notes"]]  # with no notes there is no verdict to agree with
    total = sum(r[n] for r in rows)
    return {
        "checked": total,
        "agree": sum(r[agree] for r in rows),
        "rate": round(sum(r[agree] for r in rows) / total, 3) if total else None,
    }


def _variance(rows: list[dict]) -> dict:
    by_repeat: dict[int, list] = {}
    for r in rows:
        by_repeat.setdefault(r["repeat"], []).append(r)
    recall = [
        sum(r["found"] for r in v) / max(1, sum(r["conventions"] for r in v)) for _, v in sorted(by_repeat.items())
    ]
    stale = [sum(r["stale"] for r in v) / max(1, sum(r["replaced"] for r in v)) for _, v in sorted(by_repeat.items())]
    return {
        "recall_by_repeat": [round(v, 3) for v in recall],
        "stale_by_repeat": [round(v, 3) for v in stale],
        "recall_sd": round(statistics.stdev(recall), 3) if len(recall) > 1 else None,
        "stale_sd": round(statistics.stdev(stale), 3) if len(stale) > 1 else None,
    }


# --- the agent writing code (M2, M3) -------------------------------------------------------

SYSTEM = (
    "You are {mentee}, a software engineer{at}. Your mentor {mentor} has given you coding guidelines "
    "in your past sessions together, and you must follow the latest version of each.\n"
    "Do not acknowledge. Only generate Python code and nothing else before or after. Do not explain the "
    "code. Do not ask for more information but directly give the answer."
)
REQUEST = (
    "Write a {query}. Do not provide example usage. You must follow all the latest coding guidelines "
    "provided by your mentor {mentor} in your past sessions, including any possible updates."
)
# The paper's history prompt (generate_model_output.py), for the full arm
FULL = (
    "This is a thread of dialogues between you and your mentor {mentor}:\n {history} \nBased on information "
    "provided, write a {query}. Do not provide example usage. You must follow all the latest coding "
    "guidelines provided by your mentor, including any possible updates."
)


def run_agent(
    histories: list[History],
    run: h.Run,
    model: str,
    arm: str = "tools",
    notes_run: str | None = None,
    queries: int = 3,
    repeats: int = 1,
    workers: int = 4,
    timeout: int = 300,
    embedder=None,
    cache=None,
    against: str | None = None,
) -> dict:
    """Claude Code writes the code each history ends with; the benchmark's checks grade it."""
    from . import evals_agent

    if not shutil.which("claude"):
        sys.exit("Claude Code is not installed: `claude` is not on PATH")
    run.meta.update(server=evals_agent._server_code() if arm in ("tools", "notes", "hook") else None)
    empty = os.path.join(run.dir, "no-mcp.json")
    with open(empty, "w") as f:
        json.dump({"mcpServers": {}}, f)
    end = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    setup: dict[int, dict] = {}
    for x in histories:
        setup[x.id] = _agent_setup(x, run, arm, notes_run, end, embedder, cache, empty)
    jobs = []
    for x in histories:
        asks = x.sessions[-1]["history_eval_query"]
        picked = list(range(len(asks)))
        if queries and len(picked) > queries:
            picked = sorted(random.Random(f"{h.SEED}:{x.id}").sample(picked, queries))
        jobs += [(x, q, r) for r in range(1, repeats + 1) for q in picked]
    done, lock = [0], threading.Lock()

    def one(job):
        x, q, repeat = job
        query = x.sessions[-1]["history_eval_query"][q]
        case = {"id": f"{x.id}-q{q + 1}", "split": x.split, "tags": x.tags()}
        info = setup[x.id]
        row: dict = {"history": x.id, "length": x.length, "sessions": len(x.sessions), "query": query}
        if repeats > 1:
            row["repeat"] = repeat
        if info.get("skip"):
            run.record(case, {**row, "status": info["skip"], "detail": info.get("detail")}, {})
            return
        system = SYSTEM.format(mentee=x.mentee, mentor=x.mentor, at=f" at {x.company}" if x.company else "")
        context = evals_agent.session_prompt({"asked_on": end.date().isoformat(), "cwd": x.folder})
        if info.get("memory"):
            context += "\n\n" + info["memory"]
        if arm == "full":
            prompt = FULL.format(mentor=x.mentor, history=info["history"], query=query)
        else:
            prompt = REQUEST.format(query=query, mentor=x.mentor)
        attempt = evals_agent.run_agent(prompt, model, f"{system}\n\n{context}", info["mcp"], timeout)
        answer, transcript = attempt["answer"], attempt["transcript"]
        row.update(attempt["row"])
        if arm in ("none", "full") and row["status"] == "mcp_error" and row.get("detail") == "[]":
            row.update(status=h.OK, detail=None)  # no MCP server is the point of these arms
        checks = []
        if row["status"] == h.OK:
            updated = {(c.object, json.dumps(c.regex)): bool(c.replaced) for c in x.conventions}
            for obj, regex in x.sessions[-1]["history_regex"]:
                s = compute_score(answer, obj, regex)
                checks.append({"object": obj, "regex": regex, "score": s, "updated": updated[(obj, json.dumps(regex))]})
            got = [c["score"] for c in checks if c["score"] is not None]
            row["score"] = round(statistics.fmean(got), 3) if got else None
            row["checked"] = len(got)
            for key, flag in (("updated", True), ("unchanged", False)):
                mine = [c["score"] for c in checks if c["score"] is not None and c["updated"] == flag]
                row[f"{key}_ok"], row[f"{key}_n"] = sum(mine), len(mine)
            if not got:
                row.update(status="no_objects", detail="the code holds none of the objects the conventions cover")
        transcript = {"prompt": prompt, "checks": checks, "code": extract_code(answer or ""), **transcript}
        written = run.record(case, row, transcript)
        with lock:
            done[0] += 1
            label = f"{x.id} q{q + 1}" if x.split != "test" else "test history"
            outcome = row.get("score") if row["status"] == h.OK else row["status"]
            print(f"[{done[0]}/{len(jobs)}] {label}: {outcome} {written.get('seconds', 0):.0f}s", file=sys.stderr)

    leaks_before = evals_agent._leaks()
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, jobs))
    leaks = sorted(set(evals_agent._leaks()) - set(leaks_before))

    scored = [r for r in run.rows if r["status"] == h.OK]
    per_history = _per_history(scored)
    lengths = {x.id: x.length for x in histories}
    checks = _checks(scored)
    metrics = {
        "accuracy": h.ci(list(per_history.values())),
        "short": h.ci([v for k, v in per_history.items() if lengths[k] == "short"]),
        "long": h.ci([v for k, v in per_history.items() if lengths[k] == "long"]),
    }
    tools: dict[str, int] = {}
    for r in scored:
        for name, n in (r.get("tools") or {}).items():
            tools[name] = tools.get(name, 0) + n
    sessions_of = {x.id: f"{len(x.sessions):03d}-sessions" for x in histories}
    summary = {
        "model": model,
        "arm": arm,
        **metrics,
        "checks_updated": h.ratio_ci([(v["updated_ok"], v["updated_n"]) for v in checks.values()]),
        "checks_unchanged": h.ratio_ci([(v["unchanged_ok"], v["unchanged_n"]) for v in checks.values()]),
        "by_sessions": {
            n: h.ci([v for k, v in per_history.items() if sessions_of[k] == n])
            for n in sorted(set(sessions_of.values()))
        },
        "tokens_in": h.spread([r["tokens_in"] for r in scored]),
        "tokens_out": h.spread([r["tokens_out"] for r in scored]),
        "tool_calls": {**h.spread([r["tool_calls"] for r in scored]), "by_tool": dict(sorted(tools.items()))},
        "tool_result_chars": h.spread([r["tool_chars"] for r in scored]),
        "seconds": h.spread([r["seconds"] for r in scored]),
        "cost_usd_api_equivalent": round(sum(r["cost_usd"] for r in scored), 2),
        "requests": len(jobs),
        "plumbing": h.plumbing(run.rows),
        "transcript_leaks": leaks,
    }
    if repeats > 1:
        by_repeat: dict[int, list[dict]] = {}
        for r in scored:
            by_repeat.setdefault(r["repeat"], []).append(r)
        accs = [statistics.fmean(_per_history(v).values()) for _, v in sorted(by_repeat.items())]
        summary["variance"] = {
            "accuracy_by_repeat": [round(a, 3) for a in accs],
            "run_to_run_sd": round(statistics.stdev(accs), 3) if len(accs) > 1 else None,
        }
    summary["warnings"] = h.headroom({"accuracy": metrics["accuracy"]})
    if leaks:
        summary["warnings"].append(f"{len(leaks)} folders appeared in {evals_agent.PROJECTS}: a run saved a transcript")
    if against:
        value = lambda r: r.get("score")  # noqa: E731
        theirs: dict[str, list[float]] = {}
        for k, v in h.against(against, value).items():
            theirs.setdefault(k.rsplit("-q", 1)[0], []).append(v)
        summary["against"] = {
            "run": against,
            "accuracy": h.paired(
                {str(k): v for k, v in per_history.items()}, {k: statistics.fmean(v) for k, v in theirs.items()}
            ),
        }
    headline = (
        f"acc={h.fmt(metrics['accuracy'])} short={metrics['short']['mean']} long={metrics['long']['mean']} "
        f"updated={summary['checks_updated']['mean']} tokens_in_median={summary['tokens_in'].get('median')} "
        f"tools_median={summary['tool_calls'].get('median')} arm={arm} model={model}"
    )
    if repeats > 1:
        headline += f" acc_by_repeat={summary['variance']['accuracy_by_repeat']}"
    if against:
        headline += f" Δacc={h.fmt(summary['against']['accuracy'], signed=True)}"
    columns = [
        ("sessions", "sessions"),
        ("score", "score"),
        ("checked", "checked"),
        ("tokens in", "tokens_in"),
        ("tools", "tool_calls"),
        ("query", "query"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


def _agent_setup(x: History, run: h.Run, arm: str, notes_run, end, embedder, cache, empty: str) -> dict:
    """The MCP config (or none) and prompt material one history needs for this arm."""
    from . import evals_agent

    if arm == "none":
        return {"mcp": empty}
    if arm == "full":
        history = "".join(f"\n\n Session {i} \n\n" + s["text"] for i, s in enumerate(x.sessions))
        if len(history) / 4 > FULL_HISTORY_MAX_TOKENS:
            return {"skip": "too_long", "detail": f"about {len(history) // 4:,} tokens of history"}
        return {"mcp": empty, "history": history}
    home = os.path.join(run.dir, "homes", str(x.id))
    memory = os.path.join(run.dir, "memory", str(x.id))
    os.makedirs(home, exist_ok=True)
    lines = ['sources = ["frozen-for-evals"]  # matches no agent: this index holds only the benchmark']
    if embedder is not None:
        lines += ["", "[embeddings]", f'provider = "{embedder.provider}"', f'name = "{embedder.name}"']
        if embedder.base_url:
            lines.append(f'base_url = "{embedder.base_url}"')
    with open(os.path.join(home, "config.toml"), "w") as f:
        f.write("\n".join(lines) + "\n")
    index(chats(x, end), os.path.join(home, "recall.db"), embedder, cache).close()
    injected = ""
    if arm in ("notes", "hook"):
        source = os.path.join(os.path.expanduser(notes_run), "notes", str(x.id), "r1")
        if not os.path.isdir(source):
            return {"skip": "no_notes", "detail": f"no notes for history {x.id} in {notes_run}"}
        shutil.copytree(source, memory, dirs_exist_ok=True)
        if arm == "hook":  # what the session-start hook would add for this folder, read-only
            from .memory import context
            from .memory.notes import NoteStore

            injected = context.session_context(x.folder, NoteStore(memory), agent="claude")
    else:
        os.makedirs(memory, exist_ok=True)
    mcp = os.path.join(run.dir, "mcp", f"{x.id}.json")
    os.makedirs(os.path.dirname(mcp), exist_ok=True)
    config = evals_agent.write_mcp_config(mcp, {"RECALL_HOME": home, "RECALL_MEMORY": memory})
    return {"mcp": config, "memory": injected}


def _per_history(rows: list[dict]) -> dict[int, float]:
    scores: dict[int, list[float]] = {}
    for r in rows:
        if r.get("score") is not None:
            scores.setdefault(r["history"], []).append(r["score"])
    return {k: statistics.fmean(v) for k, v in scores.items()}


def _checks(rows: list[dict]) -> dict[int, dict]:
    """Per history, regex checks passed on conventions the history updated, and on the rest."""
    keys = ("updated_ok", "updated_n", "unchanged_ok", "unchanged_n")
    out: dict[int, dict] = {}
    for r in rows:
        p = out.setdefault(r["history"], dict.fromkeys(keys, 0))
        for k in keys:
            p[k] += r.get(k, 0)
    return out


# --- the benchmark's checks, ported from evaluate_model_output.py and extract_objects.py ------

OBJECTS = (
    "function",
    "function argument",
    "function docstring",
    "function try",
    "function assert",
    "function annotation",
    "function decorator",
    "class",
    "class decorator",
    "method",
    "method docstring",
    "method try",
    "method assert",
    "method annotation",
    "method decorator",
    "attribute",
    "variable",
    "import",
    "comment",
)


def extract_code(text: str) -> str:
    """The first ```python block, else the first ``` block, else the whole reply."""
    found = re.findall(r"```python\n(.*?)```", text, re.S) or re.findall(r"```(.*?)```", text, re.S)
    return found[0] if found else text


def compute_score(text: str, obj: str, regex) -> float | None:
    """1.0 if the code follows the convention on every object of its kind, 0.0 if not, None
    when the code has no such object (the paper does not penalise that). Code that does not
    parse scores 0. Quirks kept from the original: only a function's first argument is
    checked, `from x import y` is not an import, and comments are one group."""
    code = extract_code(text)
    try:
        found = objects(code)[obj]
    except (SyntaxError, ValueError, RecursionError):
        return 0.0
    if not found and obj not in ("comment", "import"):
        return None
    if isinstance(regex, bool):
        present = [len(o) > 0 for o in found]
        ok = all(present) if regex else not any(present)
    elif isinstance(regex, list):
        name, value = regex
        grouped = obj in ("comment", "import")  # one list of names, not one list per object
        ok = (name in found) == value if grouped else all((name in o) == value for o in found)
    else:
        matches = [bool(re.match(regex, o[0])) for o in found if len(o) > 0]
        ok = bool(matches) and all(matches)  # the original's np.mean([]) is nan, which never equals 1
    return float(ok)


def objects(code: str) -> dict[str, list]:
    v = _Objects()
    v.visit(ast.parse(code))
    v.found["comment"] = [re.findall(r"#.*", code)]
    return v.found


class _Objects(ast.NodeVisitor):
    def __init__(self):
        self.found: dict[str, list] = {o: [] for o in OBJECTS}
        self.cls = None

    def visit_FunctionDef(self, node):
        f = self.found
        kind = "function" if self.cls is None else "method"
        if kind == "method" and node.name == "__init__":
            f["attribute"].append(
                [
                    t.attr
                    for s in node.body
                    if isinstance(s, ast.Assign)
                    for t in s.targets
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
                ]
            )
        elif kind == "function" or not node.name.startswith("__"):
            f[kind].append([node.name])
            if kind == "function":
                f["function argument"].append([a.arg for a in node.args.args])
            doc = ast.get_docstring(node)
            f[f"{kind} docstring"].append([doc] if doc else [])
            f[f"{kind} try"].append([n for n in node.body if isinstance(n, ast.Try)])
            f[f"{kind} assert"].append([n for n in node.body if isinstance(n, ast.Assert)])
            notes = [a.annotation for a in node.args.args if a.annotation]
            f[f"{kind} annotation"].append(notes + ([node.returns] if node.returns else []))
            f[f"{kind} decorator"].append([d for d in map(_decorator, node.decorator_list) if d])
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.found["class"].append([node.name])
        self.found["class decorator"].append([d for d in map(_decorator, node.decorator_list) if d])
        outer, self.cls = self.cls, node
        self.generic_visit(node)
        self.cls = outer

    def visit_Assign(self, node):
        for t in node.targets:
            self.found["variable"] += [[n] for n in _names(t)]
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.found["variable"] += [[n] for n in _names(node.target)]
        self.generic_visit(node)

    def visit_Import(self, node):
        self.found["import"] += [a.name for a in node.names]
        self.generic_visit(node)


def _names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple):
        return [e.id for e in target.elts if isinstance(e, ast.Name)]
    return []


def _decorator(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator(node.func)
    return None
