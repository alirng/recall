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
      [--from-candidates RUN_DIR]                              consolidation only (see below)
  recall eval memorycode --data <download> --agent [MODEL] --arm none|tools|notes|hook|placebo|full|jit|…
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

--from-candidates RUN_DIR freezes extraction: each history's candidates come from that earlier
extraction run (repeat r replays its repeat r, cycling when it has fewer; --candidates-repeat K
replays its repeat K every time, and is an error past the run's last repeat) and only
consolidation and grading run again, so a consolidation change is measured without extraction
noise. It takes the earlier run's histories unless --case picks some, and always calls the
model afresh. Consolidation is most of an extraction run's model calls (about 85% on MemoryCode),
so a replay costs nearly as much as a fresh run; what it removes is extraction's share of the
noise.

Agent mode gives the history's final coding requests to Claude Code (evals_agent.run_agent) and
grades its code with the benchmark's checks, ported from the authors' evaluate_model_output.py
and extract_objects.py: per request the share of conventions followed among those whose object
the code contains, averaged per history. Arms:
  none   no tools and no history: the no-memory baseline
  tools  recall's MCP tools over a recall home holding only this history's chats
  notes  tools, plus the notes an extraction run wrote (--notes RUN_DIR)
  hook   as notes, and the system prompt also gets what the session-start hook injects for the
         history's folder (memory.context.session_context over those notes), as in production
  placebo  as hook, with another history's notes in place of this history's: moved into this
         project, people and company renamed, note ids and sources made this history's. The
         donor shares no convention object and no quoted token with this history (a history
         with no such donor is skipped: no_donor). Among the donors that pass, the closest in
         notes size is taken, and a history whose closest donor's notes differ in size by more
         than 25% is skipped too (no_donor_size). Each row logs the donor, its overlap and
         conflicting counts (0 by the filter) and size_ratio (donor notes over this history's).
         Hook minus placebo is an upper bound on what the notes' content is worth; placebo
         minus tools is the effect of loading notes that are not this history's. The skips fall
         mostly on long histories (13 of 19 on the test split), so a filtered run leans short and
         says little about long ones;
         donors repeat, so rows are correlated. Without the filter (runs before 2026-10-15)
         the donor often held this history's conventions or contradicted them, and its size
         was only a tie-break, so such a placebo is not a priming control
  full   the whole history in the prompt and no tools, as the paper's history setting; every
         history fits (the longest is about 72k tokens)
  hook@3000  as hook, with the session-start block capped at 3,000 characters (this process only)
  jit    as hook, and Claude Code also runs recall's prompt hook (`recall hook`, jit.py) on each
         request against this history's home: notes or chats its gate is sure of join the prompt
  jit+hook@3000  jit with the 3,000-character block
  jit-notes  as jit, with the gate open for notes only: the two best-matching notes join each request
Arms are classes in ARMS (see Arm): a new arm subclasses one and registers itself there.
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

from . import config
from . import evals_harness as h
from .chats import store
from .chats.transcript import Message, Session

SHORT_MAX_ID = 210  # evaluate_model_output.py: dialogues 1-210 are "short history", the rest "long"
TRAIN_SHARE = 0.6
AGENT = "claude"  # the chats read as Claude Code chats
TURN_SECONDS = 40
FULL_HISTORY_MAX_TOKENS = 100_000  # the full arm runs only where the history fits (chars / 4)
PLACEBO_MAX_SIZE_GAP = 0.25  # the placebo skips a history whose closest donor's notes differ in size by more
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
    every = load(os.path.expanduser(a.data))
    histories = select(every, a.split, a.case, a.limit)
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
        replay = getattr(a, "from_candidates", None)
        if replay:
            source = candidate_index(replay)
            h.check_candidates_repeat(getattr(a, "candidates_repeat", None), source)
            if not a.case:  # the histories the earlier run read
                histories = [x for x in histories if any(k[0] == str(x.id) for k in source["rows"])]
            fixed = getattr(a, "candidates_repeat", None)
            settings.update(histories=len(histories), from_candidates=source["dir"], candidates_repeat=fixed)
            settings.update(
                short=sum(x.length == "short" for x in histories), long=sum(x.length == "long" for x in histories)
            )
        name = "memorycode-consolidate" if replay else "memorycode-extract"
        run = h.Run(name, a.split, settings, a.who, a.runs_dir)
        return run_extract(
            histories,
            run,
            a.model,
            a.judge or h.JUDGE,
            a.fresh,
            a.repeats,
            a.workers,
            a.against,
            replay,
            getattr(a, "candidates_repeat", None),
        )
    from . import evals_agent

    model = (a.agent or evals_agent.DEFAULT_MODEL).removeprefix("claude:")
    arm = ARMS.get(a.arm)
    if arm is None:
        sys.exit(f"--arm must be one of: {', '.join(ARMS)}")
    if arm.notes and not a.notes:
        sys.exit(f"--arm {a.arm} needs --notes: an extraction run folder (recall eval memorycode without --agent)")
    embedder = cache = None
    if a.hybrid and arm.tools:
        import sqlite3

        from . import embeddings, paths

        embedder = embeddings.configured()
        if embedder is None:
            sys.exit("--hybrid needs an [embeddings] table in config.toml and the embeddings extra")
        cache = sqlite3.connect(os.path.join(paths.HOME, "eval-vectors.db"), timeout=60)
    search = ("-hybrid" if embedder else "-keyword") if arm.tools else ""
    settings.update(
        model=model,
        arm=a.arm,
        notes_run=a.notes,
        queries=a.queries,
        timeout=a.timeout,
        embedder=embedder.model if embedder else None,
        unprompted=a.unprompted,
    )
    name = f"memorycode-agent-{a.arm}{search}{'-unprompted' if a.unprompted else ''}-{model}"
    run = h.Run(name, a.split, settings, a.who, a.runs_dir)
    return run_agent(
        histories,
        run,
        model,
        a.arm,
        a.notes,
        a.queries,
        a.repeats,
        a.workers,
        a.timeout,
        embedder,
        cache,
        a.against,
        {x.id: x for x in every},
        a.unprompted,
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
    from_candidates: str | None = None,
    candidates_repeat: int | None = None,
) -> dict:
    """from_candidates: replay that run's candidates (repeat r takes its repeat r, or always
    candidates_repeat), so only consolidation and grading run."""
    from .llm import Client, from_spec

    provider = from_spec(model, config.load()["model"])
    source = candidate_index(from_candidates) if from_candidates else None
    if problem := provider.check():
        sys.exit(problem)
    if h.same_model(provider.label.split("@")[0], judge):
        sys.exit(f"the judge ({judge}) must differ from the extraction model ({provider.label}): pass --judge")
    if repeats > 1 and not fresh:
        print("--repeats implies --fresh: cached answers would repeat the first run", file=sys.stderr)
        fresh = True
    warnings = []
    if source:
        fresh = True  # a cached answer would replay the earlier run's consolidation, not sample a new one
        if source["model"] and source["model"] != provider.label:
            warnings.append(f"candidates came from {source['model']}; consolidation now runs on {provider.label}")
            print(f"warning: {warnings[-1]}", file=sys.stderr)
    client = Client(provider, cache=not fresh)
    grader = h.judge_client(judge)
    run.meta.update(model=provider.label, judge=judge, judge_version=JUDGE_VERSION, fresh=fresh)
    jobs = [(x, r) for r in range(1, repeats + 1) for x in histories]
    done, lock = [0], threading.Lock()

    def one(job):
        x, repeat = job
        started = time.time()
        replay = None
        if source:
            replay = source_candidates(source, x.id, candidates_repeat or repeat)
        row, transcript = extract_history(x, repeat, run, client, grader, replay)
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
    summary["warnings"] = warnings + h.headroom({"recall": metrics["recall"]})
    if source:
        summary["from_candidates"] = source["dir"]
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


def extract_history(
    x: History, repeat: int, run: h.Run, client, grader, replay: dict | None = None
) -> tuple[dict, dict]:
    """Extract and fold one history into empty notes; returns (row, transcript). replay: the
    candidates an earlier run extracted ({"candidates": {session number: [...]}}), which
    replaces extraction, or {"status": ...} when that run has none for this history."""
    from . import evals_memory
    from .llm import FatalError, LLMError

    row = {"status": h.OK, "detail": None, "sessions": len(x.sessions), "length": x.length}
    transcript: dict = {"mentor": x.mentor, "mentee": x.mentee, "company": x.company, "conventions": _gold(x)}
    if replay is not None:
        row["source_repeat"] = replay.get("repeat")
        if replay.get("status"):
            row.update(status="no_candidates", detail=replay["status"])
            return row, transcript
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
            if replay is not None:
                found = {x.chat_id(int(i) - 1): (c, None) for i, c in replay["candidates"].items()}
                failed = {}
            else:
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
    row.update(score(x, notes, verdict, index_of, candidates))
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


def candidate_index(run_dir: str) -> dict:
    """An earlier extraction run's per-history transcripts, for --from-candidates."""
    folder = os.path.abspath(os.path.expanduser(run_dir))
    if not os.path.exists(os.path.join(folder, "meta.json")):  # a run wrapper folder: follow its output
        from .evals_compare import resolve

        folder = resolve(folder)
    with open(os.path.join(folder, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("eval") not in ("memorycode-extract", "memorycode-consolidate"):
        sys.exit(f"--from-candidates needs a memorycode extraction run; {folder} is {meta.get('eval')}")
    rows: dict[tuple[str, int], dict] = {}
    for part in ("", "test"):
        path = os.path.join(folder, part, "cases.jsonl")
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    r = json.loads(line)
                    r["path"] = os.path.join(folder, part, r["transcript"].removesuffix(".html") + ".json")
                    rows[(r["id"], r.get("repeat") or 1)] = r
    if not rows:
        sys.exit(f"{folder} has no case rows")
    return {"dir": folder, "model": meta.get("model"), "rows": rows, "repeats": max(k[1] for k in rows)}


def source_candidates(source: dict, history: int, repeat: int) -> dict:
    """The candidates the earlier run extracted for this history in the matching repeat."""
    k = (repeat - 1) % source["repeats"] + 1
    r = source["rows"].get((str(history), k))
    if r is None:
        return {"repeat": k, "status": f"history {history} repeat {k} is not in the source run"}
    if r["status"] != h.OK or not os.path.exists(r["path"]):
        return {"repeat": k, "status": f"the source run's repeat {k} was {r['status']}"}
    with open(r["path"]) as f:
        return {"repeat": k, "candidates": json.load(f).get("candidates") or {}}


# --- scoring the notes ---------------------------------------------------------------------


def score(
    x: History, notes: dict, verdict: dict | None, index_of: dict[str, int], candidates: dict | None = None
) -> dict:
    """candidates: {session index: the candidates extracted from it}, for extracted_latest (token
    conventions whose latest form a candidate on its last session states) and lost (of those,
    the ones no active note states: dropped by consolidation)."""
    active = notes["active"]
    texts = [f"{n['title']}\n{n['body']}" for n in active]
    said = {i: [f"{m['title']} {m['body']}" for m in c] for i, c in (candidates or {}).items()}
    extracted_latest = lost = 0
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
            if any(_states(t, tokens) for t in said.get(c.sessions[-1], [])):
                extracted_latest += 1
                lost += not exact_hit
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
        "extracted_latest": extracted_latest,
        "lost": lost,
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
# --unprompted: the same request as a plain task, with no word about past sessions or a mentor's
# guidelines, so the agent has to decide on its own to look in recall.
SYSTEM_PLAIN = (
    "You are {mentee}, a software engineer{at}.\n"
    "Do not acknowledge. Only generate Python code and nothing else before or after. Do not explain the "
    "code. Do not ask for more information but directly give the answer."
)
PLAIN = "Write a {query}. Do not provide example usage."
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
    every: dict | None = None,
    unprompted: bool = False,
) -> dict:
    """Claude Code writes the code each history ends with; the benchmark's checks grade it.
    arm: a name in ARMS. every: all histories by id, for arms that borrow from other histories.
    unprompted: ask as a plain task (PLAIN, SYSTEM_PLAIN), never mentioning past sessions."""
    from . import evals_agent

    if not shutil.which("claude"):
        sys.exit("Claude Code is not installed: `claude` is not on PATH")
    arm = ARMS[arm]
    run.meta.update(server=evals_agent._server_code() if arm.tools else None, agent_harness=evals_agent.HARNESS)
    empty = os.path.join(run.dir, "no-mcp.json")
    with open(empty, "w") as f:
        json.dump({"mcpServers": {}}, f)
    end = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    setup: dict[int, dict] = {}
    every = every or {x.id: x for x in histories}
    for x in histories:
        setup[x.id] = _agent_setup(x, arm, ArmContext(run.dir, notes_run, end, embedder, cache, empty, every))
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
        row.update(info.get("row") or {})
        if info.get("skip"):
            run.record(case, {**row, "status": info["skip"], "detail": info.get("detail")}, {})
            return
        system = (SYSTEM_PLAIN if unprompted else SYSTEM).format(
            mentee=x.mentee, mentor=x.mentor, at=f" at {x.company}" if x.company else ""
        )
        context = evals_agent.session_prompt({"asked_on": end.date().isoformat(), "cwd": x.folder})
        prompt, extra = arm.request(x, query, info)
        if unprompted and arm.name != "full":  # the full arm's prompt is the history itself
            prompt = PLAIN.format(query=query)
        if extra:
            context += "\n\n" + extra
        attempt = evals_agent.run_agent(
            prompt, model, f"{system}\n\n{context}", info["mcp"], timeout, settings=info.get("settings")
        )
        answer, transcript = attempt["answer"], attempt["transcript"]
        row.update(attempt["row"])
        row["used_recall"] = int(row["tool_calls"] > 0)  # tool_calls counts recall's tools only
        if not arm.tools and row["status"] == "mcp_error" and row.get("detail") == "[]":
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
        "arm": arm.name,
        "unprompted": unprompted,
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
        "tool_search_calls": h.spread([r["tool_search_calls"] for r in scored]),
        "tool_result_chars": h.spread([r["tool_chars"] for r in scored]),
        "used_recall": round(statistics.fmean([r["used_recall"] for r in scored]), 3) if scored else None,
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
        f"tools_median={summary['tool_calls'].get('median')} used_recall={summary['used_recall']} arm={arm.name} "
        f"model={model}" + (" unprompted" if unprompted else "")
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


# --- arms: what the agent gets as memory -------------------------------------------------------


@dataclass
class ArmContext:
    """What an arm's setup can use: the run folder, the --notes run, the day the history ends,
    search settings, an MCP config with no servers, and every history by id."""

    run_dir: str
    notes_run: str | None
    end: datetime
    embedder: object
    cache: object
    no_mcp: str
    histories: dict[int, History]
    home: str = ""  # this history's recall home (chats indexed), for arms with tools
    memory: str = ""  # this history's notes folder, as the agent's recall server sees it

    def notes_of(self, history: int) -> str:
        """Where the --notes run kept a history's notes (its first repeat)."""
        return os.path.join(os.path.expanduser(self.notes_run or ""), "notes", str(history), "r1")


class Arm:
    """One way of giving the agent memory. A new arm subclasses one of these and adds itself to
    ARMS; --arm picks it by name. The runner builds what the flags ask for, then calls setup()
    once per history and request() once per request.
      tools      recall's MCP server over a home that holds only this history's chats
      notes      needs --notes RUN_DIR; setup() fills ctx.memory, the server's notes folder
    setup() returns what request() needs; {"skip": status, "detail": ...} skips the history and
    {"row": {...}} adds fields to every row of the history."""

    name = ""
    help = ""
    tools = True
    notes = False

    def setup(self, x: History, ctx: ArmContext) -> dict:
        return {}

    def request(self, x: History, query: str, info: dict) -> tuple[str, str]:
        """One request: (the prompt, text appended to the system prompt)."""
        return REQUEST.format(query=query, mentor=x.mentor), info.get("memory", "")


class NoMemory(Arm):
    name, help, tools = "none", "no tools and no history: the no-memory baseline", False


class Tools(Arm):
    name, help = "tools", "recall's MCP tools over this history's chats"


class Notes(Arm):
    name, help, notes = "notes", "tools, plus the notes an extraction run wrote (--notes RUN_DIR)", True

    def setup(self, x: History, ctx: ArmContext) -> dict:
        source = ctx.notes_of(x.id)
        if not os.path.isdir(source):
            return {"skip": "no_notes", "detail": f"no notes for history {x.id} in {ctx.notes_run}"}
        shutil.copytree(source, ctx.memory, dirs_exist_ok=True)
        return {}


class Hook(Notes):
    name = "hook"
    help = "notes, and the system prompt gets what the session-start hook injects for the folder"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        info = super().setup(x, ctx)
        if not info.get("skip"):  # what the session-start hook would add for this folder, read-only
            from .memory import context
            from .memory.notes import NoteStore

            info["memory"] = context.session_context(x.folder, NoteStore(ctx.memory), agent="claude")
        return info


class Placebo(Hook):
    """Loads another history's notes in place of this history's, to see what part of the notes'
    gain comes from their content. The donor's notes are moved into this history's project
    with the people and company renamed and the note ids and sources made this history's. A
    donor shares no convention object and no quoted token (in any form) with this history, so
    its notes state none of this history's conventions and contradict none of them; a history
    with no such donor is skipped (no_donor). Among those donors the closest in notes size is
    taken (a donor whose company can be renamed first), and when its notes differ in size from
    this history's by more than PLACEBO_MAX_SIZE_GAP the history is skipped too (no_donor_size),
    because the filter leaves few donors and size alone would pair 4,570 characters of notes
    with 139. Each row logs the donor, how many of its conventions still overlap or conflict
    (see _overlap), which is 0 by the filter, and size_ratio, the donor's notes over this
    history's.

    This is not a pure priming control, and what it measures depends on the filter. Read hook
    minus placebo as an upper bound on what the notes' content is worth (the placebo also
    carries the cost of notes that are not this history's), and placebo minus tools as the
    effect of loading notes that are not this history's. Run unfiltered (the placebo x3 test
    run of 2026-10-13), the donor shared a latest-form convention with the history in 21 of
    40 histories and had a conflicting one on the same object in 26, so that run's placebo
    minus tools is mostly the harm of misleading notes.

    Two limits of the filtered run. The skips fall mostly on the long histories (13 of 19 on the
    test split: the filter finds no donor, or none of a similar size), which are the ones with
    the most conventions, so the rows that remain lean short: hook minus placebo from them says nothing about long
    histories, where the notes' gain is. And donors repeat across rows, so the rows are
    correlated and a bootstrap interval over them is optimistic."""

    name = "placebo"
    help = "as hook, with the notes of another history that shares no convention object or token"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        own = _notes_chars(ctx.notes_of(x.id))
        if own is None:
            return {"skip": "no_notes", "detail": f"no notes for history {x.id} in {ctx.notes_run}"}
        mine = _signature(x)
        donors, excluded = [], 0
        for name in os.listdir(os.path.join(os.path.expanduser(ctx.notes_run), "notes")):
            if not name.isdigit() or int(name) == x.id or int(name) not in ctx.histories:
                continue
            other = ctx.histories[int(name)]
            size = _notes_chars(ctx.notes_of(other.id))
            if size is None:
                continue
            theirs = _signature(other)
            if mine.objects & theirs.objects or mine.tokens & theirs.tokens:
                excluded += 1
                continue
            # a donor whose company can be renamed (both have one, or neither) comes first
            donors.append(((other.company is None) != (x.company is None), abs(size - own), other.id, size))
        if not donors:
            return {
                "skip": "no_donor",
                "detail": f"every one of the {excluded} other histories with notes shares a convention object or "
                f"token with history {x.id}",
            }
        _, _, donor, size = min(donors)
        row = {
            "donor": donor,
            "notes_chars": own,
            "donor_chars": size,
            "size_ratio": round(size / own, 3) if own else None,
        }
        if abs(size - own) > PLACEBO_MAX_SIZE_GAP * max(own, 1):
            detail = f"the best of {len(donors)} donors (history {donor}) has {size:,} characters of notes"
            return {"skip": "no_donor_size", "detail": f"{detail} against history {x.id}'s {own:,}", "row": row}
        row["overlap"], row["conflicting"] = _overlap(x, ctx.histories[donor])
        _transplant(ctx.notes_of(donor), ctx.memory, ctx.histories[donor], x)
        from .memory import context
        from .memory.notes import NoteStore

        injected = context.session_context(x.folder, NoteStore(ctx.memory), agent="claude")
        return {"memory": injected, "row": row}


class Full(Arm):
    name, tools = "full", False
    help = "the whole history in the prompt and no tools, as the paper's history setting"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        history = "".join(f"\n\n Session {i} \n\n" + s["text"] for i, s in enumerate(x.sessions))
        if len(history) / 4 > FULL_HISTORY_MAX_TOKENS:
            return {"skip": "too_long", "detail": f"about {len(history) // 4:,} tokens of history"}
        return {"history": history}

    def request(self, x: History, query: str, info: dict) -> tuple[str, str]:
        return FULL.format(mentor=x.mentor, history=info["history"], query=query), ""


@contextmanager
def _block_cap(chars: int | None):
    """The session-start block's size limit, changed in this process only, for budget arms. The
    preference and rule shares shrink with it, in whole characters: 3,000 against the default
    4,500 sets PREFS_CHARS 1,000 and RULES_CHARS 400."""
    from .memory import context

    names = ("MAX_CHARS", "PREFS_CHARS", "RULES_CHARS")
    saved = {n: getattr(context, n) for n in names}
    try:
        if chars:
            for n in names:
                setattr(context, n, saved[n] * chars // saved["MAX_CHARS"])
        yield
    finally:
        for n, v in saved.items():
            setattr(context, n, v)


class HookCap(Hook):
    """hook with a smaller session-start block (e.g. 3,000 characters against the default
    4,500): the preference and rule shares shrink in proportion (see _block_cap)."""

    def __init__(self, cap: int):
        self.cap, self.name = cap, f"hook@{cap}"
        self.help = f"as hook, with the session-start block capped at {cap:,} characters"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        with _block_cap(self.cap):
            return super().setup(x, ctx)


class Jit(Hook):
    """hook, plus recall's prompt hook running for real: on UserPromptSubmit Claude Code runs
    `recall hook` against this history's home, and jit.py's gate decides whether notes or
    earlier chats join the prompt. Fires land in the home's usage log (usage.jsonl)."""

    # The session runs in an empty scratch folder; the hook should see the history's project.
    REWRITE = "import json,sys; d=json.load(sys.stdin); d['cwd']=sys.argv[1]; print(json.dumps(d))"

    def __init__(self, cap: int | None = None):
        self.cap = cap
        self.name = "jit" if cap is None else f"jit+hook@{cap}"
        self.help = "as hook" + (f"@{cap}" if cap else "") + ", and the prompt hook adds notes or chats it is sure of"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        import shlex

        with _block_cap(self.cap):
            info = super().setup(x, ctx)
        if info.get("skip"):
            return info
        from .memory import context
        from .memory.notes import NoteStore

        con = store.connect(os.path.join(ctx.home, "recall.db"))
        context.index_notes(con, NoteStore(ctx.memory).load())  # what the hook's note search reads
        con.close()
        env = {"RECALL_HOME": ctx.home, "RECALL_MEMORY": ctx.memory}
        if os.environ.get("PYTHONPATH"):
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        py = shlex.quote(sys.executable)
        command = (
            f"{py} -c {shlex.quote(self.REWRITE)} {shlex.quote(x.folder)} | "
            + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            + f" {py} -m recall hook --agent claude"
        )
        hook = {"type": "command", "command": command, "timeout": 30}
        info["settings"] = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}})
        return info


class JitNotes(Jit):
    """The mechanism behind jit, without its gate: the prompt hook re-presents the two notes
    that best match each request (at most 1,200 characters) next to it, whatever their score.
    On MemoryCode the shipped gate never fires, so this is what tests whether notes at the
    moment of the request help beyond the same notes at session start."""

    def __init__(self):
        super().__init__()
        self.name = "jit-notes"
        self.help = "as jit, with the gate open for notes and shut for chats: the 2 best notes join each request"

    def setup(self, x: History, ctx: ArmContext) -> dict:
        info = super().setup(x, ctx)
        if not info.get("skip"):  # P = logistic(4) for a note, logistic(0) for a chat
            with open(os.path.join(ctx.home, "jit_gate.json"), "w") as f:
                json.dump({"bias": 0.0, "weights": {"note": 4.0}, "threshold": 0.9}, f)
        return info


ARMS: dict[str, Arm] = {
    a.name: a
    for a in (NoMemory(), Tools(), Notes(), Hook(), Placebo(), Full(), HookCap(3000), Jit(), Jit(3000), JitNotes())
}


def _agent_setup(x: History, arm: Arm, ctx: ArmContext) -> dict:
    """The MCP config (or none) and what the arm's requests need, for one history."""
    from . import evals_agent

    if not arm.tools:
        return {"mcp": ctx.no_mcp, **arm.setup(x, ctx)}
    ctx.home = os.path.join(ctx.run_dir, "homes", str(x.id))
    ctx.memory = os.path.join(ctx.run_dir, "memory", str(x.id))
    os.makedirs(ctx.home, exist_ok=True)
    os.makedirs(ctx.memory, exist_ok=True)
    lines = ['sources = ["frozen-for-evals"]  # matches no agent: this index holds only the benchmark']
    if ctx.embedder is not None:
        e = ctx.embedder
        lines += ["", "[embeddings]", f'provider = "{e.provider}"', f'name = "{e.name}"']
        if e.base_url:
            lines.append(f'base_url = "{e.base_url}"')
    with open(os.path.join(ctx.home, "config.toml"), "w") as f:
        f.write("\n".join(lines) + "\n")
    index(chats(x, ctx.end), os.path.join(ctx.home, "recall.db"), ctx.embedder, ctx.cache).close()
    info = arm.setup(x, ctx)
    if info.get("skip"):
        return info
    mcp = os.path.join(ctx.run_dir, "mcp", f"{x.id}.json")
    os.makedirs(os.path.dirname(mcp), exist_ok=True)
    config = evals_agent.write_mcp_config(mcp, {"RECALL_HOME": ctx.home, "RECALL_MEMORY": ctx.memory})
    return {"mcp": config, "home": ctx.home, "memory_dir": ctx.memory, **info}


def _notes_chars(folder: str) -> int | None:
    """Characters of the active notes in a notes folder (titles and bodies), None if no folder."""
    if not os.path.isdir(folder):
        return None
    return sum(len(n.title) + len(n.body) for n in _read_notes(folder) if n.status == "active")


def _read_notes(folder: str) -> list:
    from .memory.notes import read_note_file

    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        out += [
            n
            for name in filenames
            if name.endswith(".md") and name not in ("MEMORY.md", "README.md")
            if (n := read_note_file(os.path.join(dirpath, name)))
        ]
    return out


class _Signature(NamedTuple):
    objects: frozenset  # what the history's conventions are about ("function argument")
    tokens: frozenset  # the quoted tokens of every form of every convention, as _states reads them


def _tokens(*forms: str) -> frozenset:
    return frozenset(t.lower().lstrip("@") for f in forms for t in QUOTED.findall(f))


def _signature(x: History) -> _Signature:
    return _Signature(
        frozenset(c.object for c in x.conventions), frozenset().union(*(_tokens(*c.forms) for c in x.conventions))
    )


def _overlap(x: History, donor: History) -> tuple[int, int]:
    """(overlap, conflicting): how many of the donor's conventions state one of x's in its
    latest form (the same quoted tokens), and how many are about an object x has a convention
    on, with other tokens. The placebo filter keeps both at 0."""
    mine = [(c.object, _tokens(c.latest)) for c in x.conventions]
    overlap = conflicting = 0
    for c in donor.conventions:
        latest = _tokens(c.latest)
        overlap += bool(latest) and any(latest == t for _, t in mine)
        conflicting += any(c.object == obj and latest != t for obj, t in mine)
    return overlap, conflicting


def _transplant(source: str, target: str, donor: History, x: History) -> None:
    """Write the donor history's notes into target as if they were x's: project notes move to
    x's project, the donor's company and people take x's names (one pass, so a name is never
    replaced twice), and nothing of the donor's is left in the note ids, file names or sources.
    A donor with a company when x has none gets "the company"."""
    from .chats.projects import resolve
    from .memory.notes import NoteStore, new_id

    project = resolve(x.folder)
    names = {
        a.lower(): b
        for a, b in [
            (donor.company, x.company or "the company"),
            (donor.mentor, x.mentor),
            (donor.mentee, x.mentee),
        ]
        if a and b and a.lower() != b.lower()
    }
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")\b", re.I)

    def rename(text: str) -> str:
        def one(m):
            new = names[m.group(1).lower()]
            return new.upper() if m.group(0).isupper() else new.title() if m.group(0).istitle() else new

        return pattern.sub(one, text) if names else text

    notes = _read_notes(source)
    ids: dict[str, str] = {}  # the donor's note ids -> ids made from the renamed titles
    for n in notes:
        title = rename(n.title)
        new, salt = new_id(title), 0
        while new in ids.values():  # two notes with one title
            salt += 1
            new = new_id(title, salt=str(salt))
        ids[n.id] = new
    store_ = NoteStore(target)
    store_.titles = {ids[n.id]: rename(n.title) for n in notes}  # so related-note links read well
    for n in notes:
        n.title, n.body, n.path = rename(n.title), rename(n.body), ""
        n.id = ids[n.id]
        n.supersedes = [ids[i] for i in n.supersedes if i in ids]
        n.superseded_by = ids.get(n.superseded_by or "")
        n.related = [ids[i] for i in n.related if i in ids]
        n.sources = [re.sub(rf"\bmc{donor.id:03d}-", f"mc{x.id:03d}-", s) for s in n.sources]
        n.project, n.project_name = (project.key, project.name) if n.scope == "project" and project else (None, None)
        store_.write(n)
    store_.compile(store_.load())


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
