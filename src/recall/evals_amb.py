"""agent-memory-bench, "AMB" (D'Erme, 2026): does recall find the session that settles a coding
task, and does it rank the current decision above a superseded one?

AMB's corpus is recorded Claude Code sessions: for each task, the session where its governing
fact was decided, plus 156 distractor sessions. Its planting conditions change what the corpus
holds for one task (docs/PLANTING_ADJACENT_AND_CONTRADICTORY.md in its repo):
  present        the real session (the corpus as published)
  superseded     the real session and an older one stating a fact it replaced
  contradictory  two memos that disagree, the real session withheld
  adjacent       a true memo about a different subsystem, the real session withheld
(absent, the real session withheld and nothing added, has nothing to find and is left out.)
Three cross-session tasks spread their fact over several sessions: "evolve" restates one value
three times, so only the newest is current; "join" and "widen" need every session.

  recall eval amb --data <folder> [--hybrid] [--split train|test|all] [--case TASK] [--against RUN]

--data holds hf/corpus/ (huggingface.co/datasets/Gde05/agent-memory-bench-corpus) and repo/
with tasks/*/task.json, tasks/*/plants.json and corpus/plants/ from github.com/GiulioDER/
agent-memory-bench at c1b15726. Each session becomes a chat as recall's Claude Code parser
would keep it: the user's and the agent's text, without tool calls or tool output.

For each task and condition, the corpus is the published feed with only that task changed, as
AMB's scripts/assemble_condition_corpus.py builds it for one selected task (contradictory memos
re-dated with its seed-1 permutation), indexed in memory; the query is the task prompt
verbatim, as AMB's retrieval probe uses it. Scores:
  present        hit@k and MRR of the task's sessions; join/widen also all@5
  superseded     current@5, and current_first: the current session ranks above every
                 superseded one (also the "evolve" task under present)
  contradictory  both@5: both memos in the top 5, so an agent can see the conflict
  adjacent       adjacent@5: the out-of-scope memo in the top 5 (no right answer; reported)
The train/test split is computed here: seeded 60/40 over tasks, stratified by whether a task
has plants, so a task's conditions share a split.
"""

import glob
import json
import os
import random
import sys
from datetime import UTC, datetime, timedelta

from . import evals_harness as h
from .chats import store
from .chats.transcript import Message, Session

KS = (1, 3, 5, 10)
DEPTH = 50  # ranks past this count as not found
CONDITIONS = ("present", "superseded", "contradictory", "adjacent")
CONTRADICTION_DATES = ("2026-05-06", "2026-06-17")  # AMB harness/plants.py
SEED = 1  # AMB's default assembly seed
AGENT = "amb"


def from_args(a) -> dict:
    """`recall eval amb`, from the CLI's arguments."""
    if not a.data:
        sys.exit("recall eval amb needs --data: a folder holding hf/corpus/ and repo/ (see evals_amb.py)")
    data = os.path.expanduser(a.data)
    corpus, tasks = load(data)
    cases = [c for c in build_cases(tasks) if a.split == "all" or c["split"] == a.split]
    if a.case:
        cases = [c for c in cases if c["task"] in a.case or c["id"] in a.case]
    if a.limit:
        tasks_left = sorted({c["task"] for c in cases})
        keep = set(random.Random(h.SEED).sample(tasks_left, min(a.limit, len(tasks_left))))
        cases = [c for c in cases if c["task"] in keep]
    if not cases:
        sys.exit(f"no AMB cases in the {a.split} split")
    embedder = cache = None
    if a.hybrid:
        import sqlite3

        from . import embeddings, paths

        embedder = embeddings.configured()
        if embedder is None:
            sys.exit("--hybrid needs an [embeddings] table in config.toml and the embeddings extra")
        cache = sqlite3.connect(os.path.join(paths.HOME, "eval-vectors.db"), timeout=60)
    search = "hybrid" if a.hybrid else "keyword"
    settings = {
        "data": a.data,
        "search": search,
        "embedder": embedder.model if embedder else None,
        "cases": len(cases),
        "tasks": len({c["task"] for c in cases}),
        "feed_sessions": len(corpus),
        "note": a.note or "",
    }
    run = h.Run(f"amb-{search}", a.split, settings, a.who, a.runs_dir)
    return run_search(corpus, cases, run, embedder, cache, a.against)


# --- the data -------------------------------------------------------------------------


def load(data: str) -> tuple[dict[str, Session], list[dict]]:
    """The published feed as chats keyed by path ("sessions/<task>/p01", "distractors/d001"),
    and every task with its prompt, real sessions, shards and plants."""
    root = os.path.join(data, "hf", "corpus")
    corpus = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        key = os.path.relpath(path, root).removesuffix(".jsonl")
        corpus[key] = session(key, _read(path))
    if not corpus:
        sys.exit(f"no sessions under {root}: download huggingface.co/datasets/Gde05/agent-memory-bench-corpus there")
    tasks = []
    for path in sorted(glob.glob(os.path.join(data, "repo", "tasks", "*", "task.json"))):
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        tid = t["task_id"]
        real = sorted(k for k in corpus if k.startswith(f"sessions/{tid}/"))
        if not real:
            continue  # no recorded session to find
        plants_path = os.path.join(os.path.dirname(path), "plants.json")
        plants = {}
        if os.path.exists(plants_path):
            with open(plants_path, encoding="utf-8") as f:
                plants = json.load(f)
        tasks.append(
            {"id": tid, "prompt": t["prompt"], "real": real, "synthesis": t.get("synthesis"), "plants": plants}
        )
    if not tasks:
        sys.exit(f"no tasks under {data}/repo/tasks: fetch tasks/*/task.json from the AMB repo at c1b15726")
    plants_root = os.path.join(data, "repo", "corpus", "plants")
    for t in tasks:
        t["recorded"] = {}
        for name in t["plants"].get("plants", {}):
            p = os.path.join(plants_root, t["id"], f"{name}.jsonl")
            if os.path.exists(p):
                t["recorded"][name] = _read(p)
    return corpus, tasks


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def session(key: str, lines: list[dict]) -> Session:
    """A recorded session as recall would index it: text turns only, tool calls dropped."""
    messages = []
    for x in lines:
        text = (x.get("content") or "").strip()
        if text and x.get("role") in ("user", "assistant"):
            messages.append(Message(x["role"], text, _time(x.get("ts"))))
    times = [m.ts for m in messages if m.ts] or [None]
    return Session(AGENT, key, "agent-memory-bench", None, None, None, times[0], times[-1], messages=messages)


def _time(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def restamp(lines: list[dict], date: str) -> list[dict]:
    """AMB's restamp: every turn moved onto a new day, 40 seconds apart from 09:00 UTC."""
    base = datetime.strptime(date, "%Y-%m-%d").replace(hour=9, tzinfo=UTC)
    return [{**x, "ts": (base + timedelta(seconds=40 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i, x in enumerate(lines)]


def contradiction_dates(names: list[str], seed: int = SEED) -> dict[str, str]:
    """AMB's assign_contradiction_dates: which memo is older, permuted per seed."""
    dates = list(CONTRADICTION_DATES)
    while len(dates) < len(names):
        dates.append(dates[-1])
    order = list(names)
    random.Random(f"contradiction:{seed}").shuffle(order)
    return {name: dates[i] for i, name in enumerate(order)}


def build_cases(tasks: list[dict]) -> list[dict]:
    """One case per task and condition it can be built for, with the sessions that condition
    adds and withholds, and what counts as found."""
    splits = assign_splits(tasks)
    cases = []
    for t in tasks:
        base = {"task": t["id"], "prompt": t["prompt"], "split": splits[t["id"]]}
        shape = (t["synthesis"] or {}).get("shape")
        shards = [f"sessions/{t['id']}/{s['precursor']}" for s in (t["synthesis"] or {}).get("shards", [])]
        present = {**base, "condition": "present", "add": {}, "drop": [], "gold": t["real"], "shape": shape}
        if shape == "evolve" and shards:
            present.update(current=[shards[-1]], stale=shards[:-1])
        if shape in ("join", "widen"):
            present["all"] = shards or t["real"]
        cases.append(present)
        conditions = t["plants"].get("conditions", {})
        for condition in CONDITIONS[1:]:
            spec = conditions.get(condition)
            if not spec or not all(p in t["recorded"] for p in spec["plants"]):
                continue  # not declared, or a plant not yet recorded
            names = spec["plants"]
            dates = contradiction_dates(names) if condition == "contradictory" else {}
            add = {
                f"plants/{t['id']}/{n}": restamp(t["recorded"][n], dates[n]) if n in dates else t["recorded"][n]
                for n in names
            }
            case = {**base, "condition": condition, "add": add, "drop": [] if spec["include_real"] else t["real"]}
            planted = list(add)
            if condition == "superseded":
                case.update(gold=t["real"], current=t["real"], stale=planted)
            else:
                case.update(gold=planted)
            cases.append(case)
    for c in cases:
        c["id"] = f"{c['task']}/{c['condition']}"
        c["tags"] = [c["condition"]] + ([c["shape"]] if c.get("shape") else [])
    return cases


def assign_splits(tasks: list[dict]) -> dict[str, str]:
    """Seeded 60/40 train/test split over tasks, stratified by whether a task has plants."""
    from .evals_swecb import assign_splits as by_group

    return by_group([{"id": t["id"], "repo": "planted" if t["plants"] else "plain"} for t in tasks])


# --- retrieval ------------------------------------------------------------------------


def run_search(corpus: dict[str, Session], cases: list[dict], run: h.Run, embedder=None, cache=None, against=None):
    """Rank the condition's corpus for each case with store.search, in an in-memory index."""
    vectors = {}
    for c in cases:
        q = c["prompt"]
        vector = False
        if embedder is not None:
            from . import embeddings

            if q not in vectors:
                vectors[q] = embeddings.query_vector(q, embedder)
            vector = vectors[q]
            if vector is None:
                sys.exit(f"the embedding model {embedder.model} is unreachable: start it, or drop --hybrid")
        sessions = [s for k, s in corpus.items() if k not in c["drop"]]
        sessions += [session(k, lines) for k, lines in c["add"].items()]
        con = _index(sessions, embedder, cache)
        rows = store.search(con, q, limit=DEPTH, vector=vector)
        con.close()
        ranked = [r["chat_id"].split(":", 1)[1] for r in rows]
        rank = {k: ranked.index(k) + 1 for k in ranked}
        best = min((rank[g] for g in c["gold"] if g in rank), default=None)
        row = {"status": h.OK, "task": c["task"], "condition": c["condition"], "rank": best}
        row["ranks"] = {g: rank.get(g) for g in c["gold"] + c.get("stale", []) if g not in c.get("current", [])}
        if c["condition"] == "present":
            row.update({f"hit@{k}": best is not None and best <= k for k in KS})
            row["rr"] = 1 / best if best else 0.0
        if "all" in c:
            row["all@5"] = all((rank.get(g) or DEPTH + 1) <= 5 for g in c["all"])
        if "current" in c:
            cur = min((rank[g] for g in c["current"] if g in rank), default=None)
            stale = [rank.get(g) for g in c["stale"]]
            row["current_rank"] = cur
            row["current@5"] = cur is not None and cur <= 5
            row["stale@5"] = any(s is not None and s <= 5 for s in stale)
            row["current_first"] = cur is not None and all(s is None or cur < s for s in stale)
        if c["condition"] == "contradictory":
            row["both@5"] = all((rank.get(g) or DEPTH + 1) <= 5 for g in c["gold"])
            row["any@5"] = best is not None and best <= 5
        if c["condition"] == "adjacent":
            row["adjacent@5"] = best is not None and best <= 5
        results = [
            {"rank": i, "chat": k, "gold": k in c["gold"], "stale": k in c.get("stale", []), "title": r.get("title")}
            for i, (k, r) in enumerate(zip(ranked[:10], rows[:10], strict=True), 1)
        ]
        case = {"id": c["id"], "split": c["split"], "tags": c["tags"]}
        run.record(case, row, {"query": q, "condition": c["condition"], "gold": c["gold"], "results": results})
        if c["split"] != "test" and c["condition"] == "present" and not row["hit@5"]:
            print(f"miss  {c['id']:34} rank {best or '-':>3}", file=sys.stderr)

    scored = [r for r in run.rows if r["status"] == h.OK]

    def rate(key: str, condition: str | None = None) -> dict:
        return h.ci([float(r[key]) for r in scored if key in r and (condition is None or r["condition"] == condition)])

    present = [r for r in scored if r["condition"] == "present"]
    metrics = {f"hit@{k}": rate(f"hit@{k}") for k in KS}
    metrics["mrr"] = h.ci([r["rr"] for r in present])
    staleness = {
        "current_first": rate("current_first"),
        "current@5": rate("current@5"),
        "stale@5": rate("stale@5"),
    }
    summary = {
        **metrics,
        "all@5_join_widen": rate("all@5"),
        "superseded": {k: rate(k, "superseded") for k in ("current_first", "current@5", "stale@5")},
        "staleness_all": staleness,
        "contradictory": {k: rate(k, "contradictory") for k in ("both@5", "any@5")},
        "adjacent": {"adjacent@5": rate("adjacent@5", "adjacent")},
        "cases_by_condition": {c: sum(r["condition"] == c for r in scored) for c in CONDITIONS},
        "plumbing": h.plumbing(run.rows),
        "warnings": h.headroom({"hit@5": metrics["hit@5"]}),
    }
    if against:
        value = lambda r: r.get("rr") if r.get("condition") == "present" else None  # noqa: E731
        mine = {r["id"]: r["rr"] for r in present}
        summary["against"] = {"run": against, "mrr": h.paired(mine, h.against(against, value))}
        value = lambda r: float(r["current_first"]) if "current_first" in r else None  # noqa: E731
        mine = {r["id"]: float(r["current_first"]) for r in scored if "current_first" in r}
        summary["against"]["current_first"] = h.paired(mine, h.against(against, value))
    headline = (
        f"hit@5={h.fmt(metrics['hit@5'])} hit@1={metrics['hit@1']['mean']} hit@10={metrics['hit@10']['mean']} "
        f"mrr={metrics['mrr']['mean']} current_first={h.fmt(staleness['current_first'])} "
        f"both@5={summary['contradictory']['both@5']['mean']} adjacent@5={summary['adjacent']['adjacent@5']['mean']}"
    )
    if against:
        headline += f" Δmrr={h.fmt(summary['against']['mrr'], signed=True)}"
    columns = [
        ("condition", "condition"),
        ("rank", "rank"),
        ("current rank", "current_rank"),
        ("current first", "current_first"),
        ("ranks", "ranks"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


def _index(sessions: list[Session], embedder=None, cache=None):
    con = store.connect(":memory:")
    for s in sessions:
        store.upsert(con, s)
    con.commit()
    if embedder is not None:
        from . import embeddings

        embeddings.index_messages(con, embedder, cache=cache)
    return con
