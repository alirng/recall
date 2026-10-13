"""M4, note quality: what does the memory job learn from a set of chats, and does it keep
up when a later chat changes an earlier answer?

Each memory_v2 item names chats in time order and what should, and should not, be learned:
  {"id": "m-001", "chats": ["<chat id>", ...], "expect": [{"claim": "...", "type": "decision",
   "scope": "project"}], "forbid": ["..."], "supersedes": [{"old": "...", "new": "..."}],
   "negative": false, "max": 8, "tags": [...], "split": "train"}

Extraction runs as the nightly job runs it (dream._read): every item's chats are planned into
batches together and read with the configured model, or --model. Items with several chats or
with supersedes are then folded into notes one chat at a time, as successive nights would
(dream._fold_notes), into a temporary copy of the notes folder. The copy leaves out notes
already learned from the item's own chats, which would give the answer away.

--from-candidates RUN_DIR replays an earlier run's extracted memories instead of extracting, so a
folding (consolidation) change is measured without extraction noise: recall and precision then
repeat the earlier run's, and the note metrics (note_recall, updates, note_leaks) are what moves.

A Claude Sonnet judge, which also reads the chats, scores:
  recall       each expected claim is stated by some extracted memory
  precision    each extracted memory is right about the chat, durable and not forbidden
  type/scope   on the matches, against the expected type and scope ("any" matches all)
  updates      after folding, an active note states the new claim, none states the old as current
  leaks        memories or notes stating something forbidden
"""

import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, paths
from . import evals_harness as h
from .llm import Client, FatalError, LLMError, from_spec
from .memory import consolidate, digest, extract, links, routing
from .memory.notes import NoteStore

# Bump when the judge's prompts or rubric change: scores graded by different versions do not compare.
JUDGE_VERSION = "memory-judge-v3"
# v2: precision against each memory's own chat; cut chats are plumbing
# v3: a memory is forbidden only when it asserts the forbidden content, not when it states the correct version
JUDGE_CHAT_CHARS = 480_000  # of chat digests the judge reads per item, ~120k tokens; past it is plumbing


def run(
    con,
    items: list[dict],
    run: h.Run,
    model: str | None = None,
    judge: str = h.JUDGE,
    fresh: bool = False,
    repeats: int = 1,
    consistency: int = 8,
    against: str | None = None,
    from_candidates: str | None = None,
) -> dict:
    """from_candidates: an earlier memory_v2 run whose extracted memories replace extraction, so
    only folding (consolidation) and grading run again; repeat r replays its repeat r."""
    provider = from_spec(model, config.load()["model"])
    if problem := provider.check():
        sys.exit(problem)
    if h.same_model(provider.label.split("@")[0], judge):
        sys.exit(f"the judge ({judge}) must differ from the extraction model ({provider.label}): pass --judge")
    if repeats > 1 and not fresh:
        print("--repeats implies --fresh: cached answers would repeat the first run", file=sys.stderr)
        fresh = True
    source = _source(from_candidates) if from_candidates else None
    if source:
        fresh = True  # a cached answer would replay the earlier run's folding, not sample a new one
        run.meta["from_candidates"] = source["dir"]
    client = Client(provider, cache=not fresh)
    grader = h.judge_client(judge)
    run.meta.update(model=provider.label, judge=judge, judge_version=JUDGE_VERSION, fresh=fresh)

    ids = {r[0] for r in con.execute("select id from sessions")}
    by_native = {i.split(":", 1)[1]: i for i in ids}
    sessions, messages, digests = {}, {}, {}
    for item in items:
        item["chats"] = [by_native.get(c, c) for c in item["chats"]]
        for cid in item["chats"]:
            if cid in ids and cid not in sessions:
                sessions[cid] = con.execute("select * from sessions where id=?", (cid,)).fetchone()
                messages[cid] = con.execute(
                    "select seq, role, text from messages where session_id=? order by seq", (cid,)
                ).fetchall()
                d = digest.build(sessions[cid], messages[cid])
                digests[cid] = d.chunks if d else []

    results = []
    for repeat in range(1, repeats + 1):
        if source:
            found, failed = _replay(source, repeat, items)
        else:
            print(f"extracting from {len(sessions)} chats with {provider.label}", file=sys.stderr)
            found, failed = read(client, list(sessions.values()), messages)

        def one(item, repeat=repeat, found=found, failed=failed):
            return _score_item(
                item, repeat if repeats > 1 else None, run, client, grader, sessions, digests, found, failed
            )

        with ThreadPoolExecutor(provider.profile.workers) as pool:
            for done, result in enumerate(pool.map(one, items), 1):
                results.append(result)
                row = result["row"]
                label = row["id"] if row["split"] != "test" else "test item"
                print(f"[{done}/{len(items)}] {label}: {row['status']} {_short(row)}", file=sys.stderr)

    scored = [r for r in run.rows if r["status"] == h.OK]
    pooled = _pool(scored)
    metrics = {
        "recall": h.ratio_ci([(p["found"], p["expected"]) for p in pooled.values()]),
        "precision": h.ratio_ci([(p["good"], p["memories"]) for p in pooled.values()]),
        "type_accuracy": h.ratio_ci([(p["type_ok"], p["found"]) for p in pooled.values()]),
        "scope_accuracy": h.ratio_ci([(p["scope_ok"], p["found"]) for p in pooled.values()]),
        "update_correctness": h.ratio_ci([(p["updates_ok"], p["updates"]) for p in pooled.values()]),
        "note_recall": h.ratio_ci([(p["note_found"], p["expected"]) for p in pooled.values() if p["folded"]]),
    }
    negatives = [r["memories"] for r in scored if r.get("negative")]
    summary = {
        "model": provider.label,
        "judge": judge,
        "judge_version": JUDGE_VERSION,
        "repeats": repeats,
        **metrics,
        "forbidden_leaks": sum(r["leaks"] for r in scored),
        "note_leaks": sum(r["note_leaks"] for r in scored),
        "items_with_leaks": sorted({r["id"] for r in scored if r["leaks"] or r["note_leaks"]})
        if run.split == "train"
        else len({r["id"] for r in scored if r["leaks"] or r["note_leaks"]}),
        "items_over_max": sum(r["over_max"] for r in scored),
        "memories_per_item": round(statistics.fmean([r["memories"] for r in scored]), 2) if scored else None,
        "memories_per_negative_item": round(statistics.fmean(negatives), 2) if negatives else None,
        "recall_by_tag": _recall_by_tag(scored),
        "plumbing": h.plumbing(run.rows),
        "model_calls": {  # extraction and folding together
            "calls": client.calls,
            "tokens_in": client.tokens_in,
            "tokens_out": client.tokens_out,
        },
        "judge_calls": grader.calls,
    }
    if repeats > 1:
        summary["variance"] = _variance(scored)
    summary["grader"] = _consistency(judge, results, consistency)
    summary["warnings"] = h.headroom({k: v for k, v in metrics.items() if k in ("recall", "precision")})
    if against:
        summary["against"] = {"run": against}
        summary["warnings"] += h.mismatch(against, run.meta)
        for metric in ("recall", "precision"):
            value = lambda r, m=metric: r.get(m)  # noqa: E731
            mine = {k: v for k, p in pooled.items() if (v := _ratio(p, metric)) is not None}
            summary["against"][metric] = h.paired(mine, h.against(against, value))
    headline = (
        f"recall={h.fmt(metrics['recall'])} precision={h.fmt(metrics['precision'])} "
        f"updates={metrics['update_correctness']['mean']} leaks={summary['forbidden_leaks']} model={provider.label}"
    )
    if against:
        headline += f" Δrecall={h.fmt(summary['against']['recall'], signed=True)}"
    columns = [
        ("recall", "recall"),
        ("precision", "precision"),
        ("memories", "memories"),
        ("updates ok", "updates_ok"),
        ("leaks", "leaks"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


# --- the production path -------------------------------------------------------------


def read(client: Client, sessions: list, messages: dict) -> tuple[dict, dict]:
    """Extract candidates from chats as dream._read does: newest first, planned into
    batches, read in parallel. Returns ({chat id: (candidates, digest hash)}, {chat id: error})."""
    sessions = sorted(sessions, key=lambda s: -(s["updated_at"] or 0))
    batches, skipped = extract.plan([(s, messages[s["id"]]) for s in sessions], client.provider.profile)
    found = {s["id"]: ([], None) for s in skipped}  # too short to read: the job stores nothing
    failed = {}
    collector = extract.Collector(batches)
    with ThreadPoolExecutor(client.provider.profile.workers) as pool:
        futures = {pool.submit(extract.run_batch, client, b): b for b in batches}
        for future in as_completed(futures):
            try:
                for unit, candidates in future.result():
                    if complete := collector.add(unit, candidates):
                        session, kept, digest_hash = complete
                        found[session["id"]] = (kept, digest_hash)
            except FatalError:
                raise
            except (LLMError, OSError) as e:  # one bad batch fails its chats, not the run
                for unit in futures[future]:
                    if (session := collector.fail(unit)) is not None:
                        failed[session["id"]] = str(e)[:300]
    return found, failed


def _source(run_dir: str) -> dict:
    """An earlier memory_v2 run's rows, with where each transcript is."""
    folder = os.path.abspath(os.path.expanduser(run_dir))
    if not os.path.exists(os.path.join(folder, "meta.json")):  # a run wrapper folder: follow its output
        from .evals_compare import resolve

        folder = resolve(folder)
    with open(os.path.join(folder, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("eval") != "memory_v2":
        sys.exit(f"--from-candidates needs a memory_v2 run; {folder} is {meta.get('eval')}")
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
    return {"dir": folder, "rows": rows, "repeats": max(k[1] for k in rows)}


def _replay(source: dict, repeat: int, items: list[dict]) -> tuple[dict, dict]:
    """read()'s result rebuilt from the memories an earlier run extracted for the same chats."""
    k = (repeat - 1) % source["repeats"] + 1
    found: dict[str, tuple] = {}
    failed: dict[str, str] = {}
    for item in items:
        r = source["rows"].get((item["id"], k))
        if r is None or "memories" not in (t := _load(r["path"])):
            for c in item["chats"]:
                failed.setdefault(c, f"no memories for {item['id']} in the source run's repeat {k}")
            continue
        for c in item["chats"]:
            mine = [{key: v for key, v in m.items() if key != "chat"} for m in t["memories"] if m.get("chat") == c]
            found.setdefault(c, (mine, None))
            failed.pop(c, None)
    return found, {c: e for c, e in failed.items() if c not in found}


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def fold(chats: list[str], sessions: dict, found: dict, client: Client) -> dict:
    """Fold each chat's candidates into a temporary copy of the notes, one chat at a time
    and in order, as dream._fold_notes does on successive nights. Returns the notes the run
    created or changed."""
    tmp = tempfile.mkdtemp(prefix="recall-eval-notes-")
    try:
        root = os.path.join(tmp, "memory")
        shutil.copytree(paths.MEMORY, root, ignore=shutil.ignore_patterns(".git"))
        notes_store = NoteStore(root)
        forgotten = _forget(notes_store, set(chats))
        notes = notes_store.load()
        consolidate.expire(notes_store, notes)  # as if the last nightly run had just finished
        before = {n.id: _key(n) for n in notes_store.load(include_archived=True).values()}
        db = sqlite3.connect(f"file:{os.path.join(tmp, 'fold.db')}", uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("attach database ? as corpus", (f"file:{paths.DB}?mode=ro",))
        db.execute("create temp view sessions as select * from corpus.sessions")  # routing reads it
        extract.ensure_tables(db)
        log: list[str] = []
        stats: Counter = Counter()
        for cid in chats:
            candidates, digest_hash = found[cid]
            extract.store(db, sessions[cid], "done", candidates, digest_hash, client.provider.label)
            db.commit()
            notes = notes_store.load()
            notes_store.reconcile(notes)
            routing.unify_moved(db)
            routing.reroute_mentions(db)
            db.commit()
            stats.update(consolidate.run(db, client, notes_store, notes, log=log.append))
            links.relink(notes_store, notes)
            notes_store.compile(notes)
            notes_store.reconcile(notes)
        db.close()
        after = notes_store.load(include_archived=True).values()
        touched = [n for n in after if before.get(n.id) != _key(n)]
        return {
            "active": [_note(n) for n in touched if n.status == "active"],
            "retired": [_note(n) for n in touched if n.status != "active"],
            "stats": dict(stats),
            "log": log,
            "left_out_notes": forgotten,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _forget(notes_store: NoteStore, chats: set[str]) -> int:
    """Delete notes learned from these chats, and drop them from the deletion ledger too, so
    consolidation does not see them as notes the developer rejected."""
    gone = set()
    for note in notes_store.load(include_archived=True).values():
        if chats & set(note.sources):
            os.remove(note.path)
            gone.add(note.id)
    ledger_path = os.path.join(notes_store.root, ".recall", "notes.json")
    if gone and os.path.exists(ledger_path):
        with open(ledger_path) as f:
            ledger = json.load(f)
        for part in ("known", "rejected"):
            ledger[part] = {k: v for k, v in ledger.get(part, {}).items() if k not in gone}
        with open(ledger_path, "w") as f:
            json.dump(ledger, f)
    return len(gone)


def _key(note) -> tuple:
    return (note.title, note.body, note.status, tuple(note.sources))


def _note(note) -> dict:
    return {
        "id": note.id,
        "type": note.type,
        "scope": note.scope,
        "status": note.status,
        "title": note.title,
        "body": note.body,
        "sources": note.sources,
        "supersedes": note.supersedes,
    }


# --- scoring --------------------------------------------------------------------------


def _score_item(item, repeat, run, client, grader, sessions, digests, found, failed) -> dict:
    chats = item["chats"]
    row = {"status": h.OK, "detail": None, "chats": len(chats), "negative": bool(item.get("negative"))}
    if repeat:
        row["repeat"] = repeat
    transcript: dict = {"chats": [_chat(sessions.get(c), digests.get(c), c) for c in chats]}
    missing = [c for c in chats if c not in sessions]
    broken = {c: failed[c] for c in chats if c in failed}
    if missing:
        row.update(status="missing_chat", detail=f"not in the index: {missing}")
        return {"row": run.record(item, row, transcript)}
    if broken:
        row.update(status="api_error", detail=json.dumps(broken)[:300])
        return {"row": run.record(item, row, transcript)}
    memories = [dict(m, chat=c) for c in chats for m in found[c][0]]
    transcript["memories"] = memories
    expect = item.get("expect") or []
    folds = len(chats) > 1 or bool(item.get("supersedes"))
    step = "fold"
    try:
        if folds:
            transcript["notes"] = fold(chats, sessions, found, client)
        step = "judge"
        verdict = transcript["judge"] = grade(grader, item, memories, [digests[c] for c in chats])
        if folds:
            transcript["update_judge"] = grade_notes(grader, item, transcript["notes"])
    except FatalError:
        raise
    except LLMError as e:
        row.update(status="api_error" if step == "fold" else "judge_error", detail=str(e)[:300])
        return {"row": run.record(item, row, transcript)}
    if verdict["chats_cut"]:  # the judge could not read every chat, so its verdicts do not count
        row.update(status="truncated", detail=f"chat digests over {JUDGE_CHAT_CHARS:,} chars")
        return {"row": run.record(item, row, transcript)}
    matches = [m for m in (e["memory"] for e in verdict["expected"]) if 1 <= m <= len(memories)]
    good = sum(1 for m in verdict["memories"] if m["correct"] and m["durable"] and not m["forbidden"])
    type_ok = scope_ok = 0
    for e in verdict["expected"]:
        if 1 <= e["expected"] <= len(expect) and 1 <= e["memory"] <= len(memories):
            want, got = expect[e["expected"] - 1], memories[e["memory"] - 1]
            type_ok += want.get("type", "any") in ("any", got["type"])
            scope_ok += want.get("scope", "any") in ("any", got["scope"]) or (want.get("scope"), got["scope"]) == (
                "project",
                "unfiled",
            )
    row.update(
        memories=len(memories),
        expected=len(expect),
        found=len(matches),
        good=good,
        type_ok=type_ok,
        scope_ok=scope_ok,
        leaks=sum(m["forbidden"] for m in verdict["memories"]),
        over_max=len(memories) > item.get("max", 8),
        recall=round(len(matches) / len(expect), 3) if expect else None,
        precision=round(good / len(memories), 3) if memories else None,
        folded=folds,
        updates=0,
        updates_ok=0,
        note_found=0,
        note_leaks=0,
    )
    if folds:
        u = transcript["update_judge"]
        row.update(
            updates=len(item.get("supersedes") or []),
            updates_ok=sum(1 for x in u["updates"] if x["new_stated"] and not x["old_as_current"]),
            note_found=sum(1 for e in u["expected"] if 1 <= e["note"] <= len(transcript["notes"]["active"])),
            note_leaks=len(u["forbidden_notes"]),
            notes=len(transcript["notes"]["active"]),
        )
    return {
        "row": run.record(item, row, transcript),
        "item": item,
        "memories": memories,
        "digests": [digests[c] for c in chats],
        "verdict": verdict,
    }


# Forbid entries often say why in brackets ("X (the developer chose Y)"); v2 judges flagged memories stating Y.
FORBID_RULE = (
    "A memory or note breaks this list only when it asserts an item's content as true or as a rule. Text "
    "in brackets after an item explains why it is wrong, so stating that correct version, or only naming "
    "the topic, does not break it. Secrets (passwords, keys, tokens) always do."
)
JUDGE_SYSTEM = (
    "You grade the memories a model extracted from a developer's coding-agent chats. A memory is "
    "meant to help an agent in a later session: the developer's preferences, project facts, "
    "decisions, lessons and open threads. Judge meaning, not wording."
)
_PICK = {"type": "integer", "minimum": 0}
MEMORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["expected", "memories"],
    "properties": {
        "expected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["expected", "reasoning", "memory"],
                "properties": {"expected": _PICK, "reasoning": {"type": "string"}, "memory": _PICK},
            },
        },
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["memory", "reasoning", "correct", "durable", "forbidden"],
                "properties": {
                    "memory": _PICK,
                    "reasoning": {"type": "string"},
                    "correct": {"type": "boolean"},
                    "durable": {"type": "boolean"},
                    "forbidden": {"type": "boolean"},
                },
            },
        },
    },
}
NOTES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["updates", "expected", "forbidden_notes"],
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["update", "reasoning", "new_stated", "old_as_current"],
                "properties": {
                    "update": _PICK,
                    "reasoning": {"type": "string"},
                    "new_stated": {"type": "boolean"},
                    "old_as_current": {"type": "boolean"},
                },
            },
        },
        "expected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["expected", "reasoning", "note"],
                "properties": {"expected": _PICK, "reasoning": {"type": "string"}, "note": _PICK},
            },
        },
        "forbidden_notes": {"type": "array", "items": _PICK},
    },
}


def grade(client, item: dict, memories: list[dict], chat_digests: list[list[str]]) -> dict:
    """Match expected claims to memories, and check each memory against the chat it came from.
    A later chat that changes an earlier answer does not make the earlier memory wrong:
    extraction reads one chat at a time, and folding in updates is scored apart."""
    n = len(chat_digests)
    chats = "\n\n".join(
        f"=== Chat {i} of {n}{' (oldest first)' if n > 1 and i == 1 else ''}\n" + "\n".join(chunks)
        for i, chunks in enumerate(chat_digests, 1)
    )
    cut = len(chats) > JUDGE_CHAT_CHARS
    if cut:
        chats = chats[:JUDGE_CHAT_CHARS] + "\n…[the rest of the chats is cut]"
    number = {cid: i for i, cid in enumerate(item["chats"], 1)}
    prompt = (
        "The chats, as the extraction model read them ([U] is the developer, [A] the agent; agent "
        f"turns are shortened):\n\n{chats or '(none)'}\n\n"
        f"Expected memories (each should be stated by at least one memory):\n{_expected(item)}\n\n"
        f"Never to be stored:\n{_lines(item.get('forbid'))}\n{FORBID_RULE}\n\n"
        f"Memories the model produced:\n{_memories(memories, number if n > 1 else None)}\n\n"
        "For each expected memory, give the number of the produced memory that states it (the same "
        "meaning; it may say more), or 0 if none does. For each produced memory decide: correct (the "
        "chat it came from supports it; something the agent proposed and the developer did not accept is "
        "not the developer's choice; a later chat changing it does not make it incorrect, since updates "
        "are graded separately), durable (still useful to an agent weeks later, not a step of this one "
        "task), forbidden (it breaks the never-to-store list). Cover every expected "
        "memory and every produced memory, by number."
    )
    verdict = client.json("memory-judge", JUDGE_SYSTEM, prompt, MEMORY_SCHEMA)
    verdict["chats_cut"] = cut
    # an entry the judge left out counts against the item: unmatched, or not a good memory
    judged = {m["memory"] for m in verdict["memories"]}
    verdict["memories"] += [
        {"memory": i, "reasoning": "(not judged)", "correct": False, "durable": False, "forbidden": False}
        for i in range(1, len(memories) + 1)
        if i not in judged
    ]
    covered = {e["expected"] for e in verdict["expected"]}
    verdict["expected"] += [
        {"expected": i, "reasoning": "(not judged)", "memory": 0}
        for i in range(1, len(item.get("expect") or []) + 1)
        if i not in covered
    ]
    return verdict


def grade_notes(client, item: dict, notes: dict) -> dict:
    """After folding: are updates applied, and do the notes hold the expected claims?"""
    updates = "\n".join(
        f"{i}. old: {u['old']}\n   new: {u['new']}" for i, u in enumerate(item.get("supersedes") or [], 1)
    )
    active = "\n".join(
        f"[{i}] ({n['type']}, {n['scope']}) {n['title']}: {n['body']}" for i, n in enumerate(notes["active"], 1)
    )
    retired = "\n".join(f"- ({n['status']}) {n['title']}: {n['body']}" for n in notes["retired"])
    prompt = (
        f"Expected memories:\n{_expected(item)}\n\n"
        f"Updates, where a later chat replaced an earlier claim:\n{updates or '(none)'}\n\n"
        f"Never to be stored:\n{_lines(item.get('forbid'))}\n{FORBID_RULE}\n\n"
        f"Active memory notes after the chats were folded in, in order:\n{active or '(none)'}\n\n"
        f"Notes retired along the way (superseded or expired; not shown to agents):\n{retired or '(none)'}\n\n"
        "For each update: new_stated (an active note states the new claim as what holds now) and "
        "old_as_current (an active note still states the old claim as what holds now; mentioning it as "
        "history is fine). For each expected memory, the number of the active note that states it, or 0. "
        "forbidden_notes: the numbers of active notes that break the never-to-store list."
    )
    verdict = client.json("memory-notes-judge", JUDGE_SYSTEM, prompt, NOTES_SCHEMA)
    covered = {u["update"] for u in verdict["updates"]}
    verdict["updates"] += [
        {"update": i, "reasoning": "(not judged)", "new_stated": False, "old_as_current": True}
        for i in range(1, len(item.get("supersedes") or []) + 1)
        if i not in covered
    ]
    return verdict


def _expected(item: dict) -> str:
    expect = item.get("expect") or []
    if not expect:
        return "(none: little or nothing here is worth remembering)"
    return "\n".join(
        f"{i}. [{e.get('type', 'any')}, {e.get('scope', 'any')}] {e['claim']}" for i, e in enumerate(expect, 1)
    )


def _memories(memories: list[dict], chat_number: dict | None = None) -> str:
    if not memories:
        return "(none)"
    lines = []
    for i, m in enumerate(memories, 1):
        source = f"(from chat {chat_number[m['chat']]}) " if chat_number else ""
        lines.append(f"{i}. {source}[{m['type']}, {m['scope']}] {m['title']}: {m['body']} (why: {m.get('why', '')})")
    return "\n".join(lines)


def _lines(values) -> str:
    return "\n".join(f"- {v}" for v in values) if values else "(nothing listed)"


def _chat(session, chunks, cid) -> dict:
    if session is None:
        return {"id": cid, "missing": True}
    return {"id": cid, "title": session["title"], "updated": session["updated_at"], "digest": "\n".join(chunks or [])}


def _short(row: dict) -> str:
    if row["status"] != h.OK:
        return row.get("detail") or ""
    parts = [f"recall {row['found']}/{row['expected']}", f"precision {row['good']}/{row['memories']}"]
    if row.get("folded"):
        parts.append(f"updates {row['updates_ok']}/{row['updates']}")
    return ", ".join(parts)


def _pool(rows: list[dict]) -> dict[str, dict]:
    """Per item, sums over repeats, so each item counts once in a bootstrap."""
    keys = ("found", "expected", "good", "memories", "type_ok", "scope_ok", "updates_ok", "updates", "note_found")
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(r["id"], {**dict.fromkeys(keys, 0), "folded": r["folded"], "tags": r["tags"]})
        for k in keys:
            p[k] += r[k]
    return out


def _ratio(p: dict, metric: str) -> float | None:
    num, den = {"recall": ("found", "expected"), "precision": ("good", "memories")}[metric]
    return p[num] / p[den] if p[den] else None


def _recall_by_tag(rows: list[dict]) -> dict:
    tags: dict[str, list] = {}
    for r in rows:
        for t in r.get("tags") or ["(untagged)"]:
            tags.setdefault(t, []).append((r["found"], r["expected"]))
    return {t: h.ratio_ci(v) for t, v in sorted(tags.items())}


def _variance(rows: list[dict]) -> dict:
    by_repeat: dict[int, list] = {}
    for r in rows:
        by_repeat.setdefault(r["repeat"], []).append(r)
    recall = [sum(r["found"] for r in v) / max(1, sum(r["expected"] for r in v)) for _, v in sorted(by_repeat.items())]
    precision = [
        sum(r["good"] for r in v) / max(1, sum(r["memories"] for r in v)) for _, v in sorted(by_repeat.items())
    ]
    return {
        "recall_by_repeat": [round(x, 3) for x in recall],
        "precision_by_repeat": [round(x, 3) for x in precision],
        "recall_sd": round(statistics.stdev(recall), 3) if len(recall) > 1 else None,
        "precision_sd": round(statistics.stdev(precision), 3) if len(precision) > 1 else None,
    }


def _consistency(judge: str, results: list[dict], sample: int) -> dict:
    """Judge a sample of items again, skipping the cache, and count changed verdicts."""
    graded = [r for r in results if r.get("verdict")]
    if not sample or not graded:
        return {"sampled": 0}
    fresh = h.judge_client(judge, cache=False)
    match_flips = memory_flips = expected = judged = errors = 0
    for r in random.Random(h.SEED).sample(graded, min(sample, len(graded))):
        try:
            second = grade(fresh, r["item"], r["memories"], r["digests"])
        except LLMError:
            errors += 1
            continue
        first_match = {e["expected"]: e["memory"] > 0 for e in r["verdict"]["expected"]}
        for e in second["expected"]:
            expected += 1
            match_flips += first_match.get(e["expected"]) != (e["memory"] > 0)
        first_good = {m["memory"]: _good(m) for m in r["verdict"]["memories"]}
        for m in second["memories"]:
            judged += 1
            memory_flips += first_good.get(m["memory"]) != _good(m)
    return {
        "sampled": min(sample, len(graded)) - errors,
        "expected_match_flips": match_flips,
        "expected_judged": expected,
        "memory_verdict_flips": memory_flips,
        "memories_judged": judged,
        **({"errors": errors} if errors else {}),
    }


def _good(m: dict) -> bool:
    return m["correct"] and m["durable"] and not m["forbidden"]
