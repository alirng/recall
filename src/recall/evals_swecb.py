"""SWE-ContextBench (Zhu et al., 2026): can recall find the past fix that helps with a new bug?

The benchmark pairs 362 "related" SWE-bench tasks with earlier "experience" tasks from the
same repo whose fix helps. Its Lite subset ships 300 real Claude Code transcripts, one per
experience task, so recall can index them exactly as it indexes real chats. Each related
task's problem statement is the query; a hit is any gold experience task in the top k.

  recall eval swe-contextbench --data <download> [--subset lite|full] [--corpus transcripts|records]
      [--query problem|title] [--scope repo|all] [--hybrid] [--split train|test|all] [--against RUN]

--data is a folder holding the GitHub repo (repo/cases/...) and the Hugging Face dataset
(hf/data/*.parquet); see github.com/jiayuanz3/SWEContextBench. Reading the gold links needs
pyarrow, which recall does not install: run under `uv run --with pyarrow`.

corpus: "transcripts" (Lite only) indexes the 300 sessions with sources.claude.load, so the
index holds what it would hold for real chats; "records" makes one two-message chat per
experience task from its problem statement and gold patch (1,007 for Full), a larger but less
realistic guard. scope: "repo" searches only the task's own repo, as an agent searching its
own project would; "all" searches every repo at once.

The train/test split is computed here: a seeded 60/40 split of the related tasks, stratified
by repo (Lite tasks keep their Lite split in Full). Test tasks are only reported in aggregate.

With --agent MODEL, Claude Code (recall's MCP tools only) gets each problem statement and is
asked to name the past chats it would rely on; see run_agent.
"""

import glob
import json
import os
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from . import evals_harness as h
from .chats import store
from .chats.transcript import Message, Session

KS = (1, 3, 5, 10, 15)  # 15: the cutoff the paper reports for Supermemory
TRAIN_SHARE = 0.6
INSTANCE = re.compile(r"instance_id:\s*(\S+)")
TRANSCRIPTS = os.path.join("repo", "cases", "SWEContextBench Lite Past Experience")
TASKS = {"lite": "SWEContextBench Lite", "full": "SWEContextBench Full"}
EXPERIENCE = {"lite": "SWEContextBench_Lite_Experience", "full": "SWEContextBench_Experience"}


@dataclass
class Bench:
    tasks: list[dict]  # related tasks: id, repo, problem, split
    gold: dict[str, set[str]]  # related id -> experience ids that help
    sessions: dict[str, Session] = field(default_factory=dict)  # experience id -> chat
    repo_of: dict[str, str] = field(default_factory=dict)  # experience id -> owner/name


def from_args(a) -> dict:
    """`recall eval swe-contextbench`, from the CLI's arguments."""
    if not a.data:
        sys.exit("recall eval swe-contextbench needs --data: the folder holding repo/ and hf/ from the benchmark")
    if a.subset == "full" and a.corpus == "transcripts":
        sys.exit("only Lite ships transcripts: use --corpus records with --subset full")
    agent = a.agent is not None
    bench = load(os.path.expanduser(a.data), a.subset, a.corpus, agent="claude" if agent else "swecb")
    tasks = [t for t in bench.tasks if a.split == "all" or t["split"] == a.split]
    if a.case:
        tasks = [t for t in tasks if t["id"] in a.case]
    if a.limit and len(tasks) > a.limit:
        keep = {t["id"] for t in random.Random(h.SEED).sample(tasks, a.limit)}
        tasks = [t for t in tasks if t["id"] in keep]
    if not tasks:
        sys.exit(f"no {a.subset} tasks in the {a.split} split")
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
        "subset": a.subset,
        "corpus": a.corpus,
        "query": a.query,
        "search": search,
        "embedder": embedder.model if embedder else None,
        "tasks": len(tasks),
        "experience_chats": len(bench.sessions),
        "note": a.note or "",
    }
    name = (
        f"swecb-{a.subset}" + ("-records" if a.corpus == "records" else "") + ("-title" if a.query == "title" else "")
    )
    if agent:
        from . import evals_agent

        model = (a.agent or evals_agent.DEFAULT_MODEL).removeprefix("claude:")
        settings.update(model=model, workers=a.workers, timeout=a.timeout)
        run = h.Run(f"{name}-agent-{model}-{search}", a.split, settings, a.who, a.runs_dir)
        return run_agent(bench, tasks, run, model, a.query, embedder, cache, a.workers, a.timeout, a.against)
    settings["scope"] = a.scope
    run = h.Run(f"{name}-{a.scope}-{search}", a.split, settings, a.who, a.runs_dir)
    return run_search(bench, tasks, run, a.query, a.scope, embedder, cache, a.against)


# --- the data -------------------------------------------------------------------------


def load(data: str, subset: str = "lite", corpus: str = "transcripts", agent: str = "swecb") -> Bench:
    """The related tasks with their split and gold links, and the experience chats.
    agent: the agent label the chats get, so chat ids read "<agent>:<instance id>"."""
    experience = _parquet(data, EXPERIENCE[subset])
    repo_of = {r["instance_id"]: r["repo"].lower() for r in experience}  # case differs between files
    gold: dict[str, set[str]] = {}
    for link in _parquet(data, "SWEContextBench_Relationship"):
        gold.setdefault(link["related_instance_id"], set()).add(link["experience_instance_id"])
    tasks = _tasks(data, subset)
    lite = {t["id"] for t in _tasks(data, "lite")} if subset == "full" else {t["id"] for t in tasks}
    splits = assign_splits([t for t in tasks if t["id"] in lite])
    splits.update(assign_splits([t for t in tasks if t["id"] not in lite]))
    for t in tasks:
        t["split"] = splits[t["id"]]
    bench = Bench(tasks, gold, repo_of=repo_of)
    if corpus == "transcripts":
        for path in sorted(glob.glob(os.path.join(data, TRANSCRIPTS, "*.jsonl"))):
            s = _transcript(path, agent)
            if s is not None:
                bench.sessions[s.native_id] = s
                bench.repo_of.setdefault(s.native_id, _repo_from_id(s.native_id))
    else:
        for r in experience:
            bench.sessions[r["instance_id"]] = _record(r, agent)
    if not bench.sessions:
        sys.exit(f"no experience chats under {data}: expected {TRANSCRIPTS} or hf/data/{EXPERIENCE[subset]}.parquet")
    return bench


def assign_splits(tasks: list[dict]) -> dict[str, str]:
    """Seeded 60/40 train/test split, stratified by repo: each repo's sorted ids are shuffled
    and its share of train places goes to the first of them. Shares are rounded by largest
    remainder, so the total is 60% of all tasks, rounded."""
    by_repo: dict[str, list[str]] = {}
    for t in sorted(tasks, key=lambda t: t["id"]):
        by_repo.setdefault(t["repo"], []).append(t["id"])
    rng = random.Random(h.SEED)
    quota = {repo: len(ids) * TRAIN_SHARE for repo, ids in sorted(by_repo.items())}
    places = {repo: int(q) for repo, q in quota.items()}
    spare = round(len(tasks) * TRAIN_SHARE) - sum(places.values())
    for repo in sorted(quota, key=lambda r: (places[r] - quota[r], r))[:spare]:
        places[repo] += 1
    out = {}
    for repo, ids in sorted(by_repo.items()):
        rng.shuffle(ids)
        out.update({i: "train" if n < places[repo] else "test" for n, i in enumerate(ids)})
    return out


def _parquet(data: str, name: str) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit(
            "recall eval swe-contextbench reads the benchmark's parquet files with pyarrow, which recall "
            "does not install: run it as `uv run --with pyarrow recall eval swe-contextbench ...`"
        )
    path = os.path.join(data, "hf", "data", f"{name}.parquet")
    if not os.path.exists(path):
        sys.exit(f"no {path}: --data should hold hf/data/ from huggingface.co/datasets/jiayuanz3/SWEContextBench")
    return pq.read_table(path).to_pylist()


def _tasks(data: str, subset: str) -> list[dict]:
    folder = os.path.join(data, "repo", "cases", TASKS[subset])
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        out.append({"id": d["instance_id"], "repo": d["repo"].lower(), "problem": d["problem_statement"]})
    if not out:
        sys.exit(f"no task files in {folder}: --data should hold repo/ from github.com/jiayuanz3/SWEContextBench")
    return out


def _transcript(path: str, agent: str) -> Session | None:
    """A past-experience session as recall reads any Claude Code chat, named by its task."""
    from .chats.sources import claude

    s = claude.load(path)
    first = next((m.text for m in s.messages if m.role == "user"), "")
    found = INSTANCE.search(first)
    if not found:
        print(f"skipped {os.path.basename(path)}: no instance_id in its first message", file=sys.stderr)
        return None
    s.agent, s.native_id = agent, found.group(1)
    s.project = None  # every session ran in ./swebench_9_15/testbed
    return s


def _record(r: dict, agent: str) -> Session:
    """An experience task with no transcript, as a two-message chat: the issue, then its fix."""
    files = re.findall(r"^diff --git a/(\S+) b/", r["patch"] or "", re.M)
    issue = "\n\n".join(x for x in (r["problem_statement"], r.get("hints_text")) if x)
    fix = f"Fixed by changing {', '.join(files) or 'the code'}.\n\n{r['patch'] or ''}"
    ts = _time(r.get("created_at"))
    messages = [Message("user", issue, ts), Message("assistant", fix, ts)]
    return Session(agent, r["instance_id"], "swe-contextbench", started_at=ts, updated_at=ts, messages=messages)


def _repo_from_id(instance_id: str) -> str:
    return instance_id.rsplit("-", 1)[0].replace("__", "/").lower()  # django__django-13401 -> django/django


def _time(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _query(task: dict, kind: str) -> str:
    text = task["problem"].strip()
    return text.split("\n", 1)[0] if kind == "title" else text


# --- retrieval (M1) ---------------------------------------------------------------------


def run_search(bench: Bench, tasks: list[dict], run: h.Run, query: str, scope: str, embedder, cache, against=None):
    """Rank the experience chats for each task with store.search, in an in-memory index of
    the task's repo (scope "repo") or of every repo ("all")."""
    pools: dict[str, list[str]] = {}
    for iid in bench.sessions:
        pools.setdefault(bench.repo_of[iid] if scope == "repo" else "all", []).append(iid)
    indexes = {key: _index([bench.sessions[i] for i in ids], embedder, cache) for key, ids in pools.items()}
    top = max(KS)
    for t in tasks:
        pool = pools.get(t["repo"] if scope == "repo" else "all", [])
        gold = bench.gold.get(t["id"], set()) & set(pool)
        case = {"id": t["id"], "split": t["split"], "tags": [t["repo"]]}
        if not gold:
            detail = "no gold link" if t["id"] not in bench.gold else "no gold experience chat in the pool"
            run.record(case, {"status": "no_gold", "detail": detail, "repo": t["repo"]}, {})
            continue
        q = _query(t, query)
        vector = False
        if embedder is not None:
            from . import embeddings

            vector = embeddings.query_vector(q, embedder)
            if vector is None:
                sys.exit(f"the embedding model {embedder.model} is unreachable: start it, or drop --hybrid")
        con = indexes[t["repo"] if scope == "repo" else "all"]
        own = f"{bench.sessions[pool[0]].agent}:{t['id']}"
        rows = [r for r in store.search(con, q, limit=top + 1, vector=vector) if r["chat_id"] != own][:top]
        ranked = [r["chat_id"].split(":", 1)[1] for r in rows]
        rank = next((i for i, x in enumerate(ranked, 1) if x in gold), None)
        row = {"status": h.OK, "repo": t["repo"], "rank": rank, "gold": sorted(gold)}
        row.update({f"hit@{k}": rank is not None and rank <= k for k in KS})
        row["rr"] = 1 / rank if rank else 0.0
        results = [
            {"rank": i, "expected": x in gold, **{k: r.get(k) for k in ("chat_id", "title", "snippet")}}
            for i, (x, r) in enumerate(zip(ranked, rows, strict=True), 1)
        ]
        run.record(case, row, {"query": q, "gold": sorted(gold), "results": results})
        if t["split"] != "test" and not row["hit@5"]:
            print(f"miss  {t['id']:34} rank {rank or '-':>3}  {q.splitlines()[0][:70]}", file=sys.stderr)
    for con in indexes.values():
        con.close()

    scored = [r for r in run.rows if r["status"] == h.OK]
    metrics = {f"hit@{k}": h.ci([float(r[f"hit@{k}"]) for r in scored]) for k in KS}
    metrics["mrr"] = h.ci([r["rr"] for r in scored])
    summary = {**metrics}
    train = [r for r in scored if r["split"] != "test"]  # per repo is too close to per case for test
    if train:
        hits, mrr = h.by_tag(train, lambda r: float(r["hit@5"])), h.by_tag(train, lambda r: r["rr"])
        summary["by_repo_train"] = {t: {"hit@5": hits[t], "mrr": mrr[t]} for t in hits}
    summary["plumbing"] = h.plumbing(run.rows)
    summary["warnings"] = h.headroom(metrics)
    if against:
        summary["against"] = {"run": against}
        for metric, value in (("hit@5", lambda r: float(r["hit@5"])), ("mrr", lambda r: r["rr"])):
            summary["against"][metric] = h.paired({r["id"]: value(r) for r in scored}, h.against(against, value))
    headline = (
        f"hit@5={h.fmt(metrics['hit@5'])} hit@1={metrics['hit@1']['mean']} hit@10={metrics['hit@10']['mean']} "
        f"mrr={metrics['mrr']['mean']}"
    )
    if against:
        headline += f" Δhit@5={h.fmt(summary['against']['hit@5'], signed=True)}"
    columns = [("repo", "repo"), ("rank", "rank"), ("hit@5", "hit@5"), ("gold", "gold")]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


def _index(sessions: list[Session], embedder=None, cache=None, path: str = ":memory:"):
    con = store.connect(path)
    for s in sessions:
        store.upsert(con, s)
    con.commit()
    if embedder is not None:
        from . import embeddings

        embeddings.index_messages(con, embedder, cache=cache)
    return con


# --- agent retrieval (M2 proxy, M3) ---------------------------------------------------

PROMPT = """I'm about to work on the issue below in this repository. Before I start, look through my \
past coding-agent chats for sessions in this project that would help me fix it: the same bug, a \
closely related fix, or work on the same code. Don't try to fix the issue yourself.

End your reply with one line in this form, listing the chat ids of the past sessions you would rely \
on, most useful first:
CHATS: <chat_id>, <chat_id>
or "CHATS: none" if no past session would help.

<issue>
{issue}
</issue>"""
CHATS_LINE = re.compile(r"^\W*CHATS\W*:(.*)$", re.I | re.M)
CHAT_ID = re.compile(r"(?:[a-z]+:)?([A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+)")


def run_agent(
    bench: Bench,
    tasks: list[dict],
    run: h.Run,
    model: str,
    query: str = "problem",
    embedder=None,
    cache=None,
    workers: int = 4,
    timeout: int = 300,
    against: str | None = None,
) -> dict:
    """Claude Code with recall's MCP tools and nothing else, over a recall home made for this
    run that holds only the experience chats, each in a project folder named after its repo.
    The agent is told the task's project folder, gets the issue, and names the chats it would
    rely on. Scores: a gold chat among them (hit), the share of named chats that are gold
    (precision), whether a gold chat ever came back from a tool (seen), and the input tokens,
    tool calls and time it took."""
    import shutil

    from . import evals_agent

    if not shutil.which("claude"):
        sys.exit("Claude Code is not installed: `claude` is not on PATH")
    home, memory = os.path.join(run.dir, "home"), os.path.join(run.dir, "memory")
    _agent_home(bench, home, memory, embedder, cache)
    mcp = evals_agent.write_mcp_config(
        os.path.join(run.dir, "mcp.json"), {"RECALL_HOME": home, "RECALL_MEMORY": memory}
    )
    run.meta.update(
        recall_home=home, recall_memory=memory, server=evals_agent._server_code(), agent_harness=evals_agent.HARNESS
    )
    known = set(bench.sessions)
    done, lock = [0], threading.Lock()

    def one(t: dict) -> None:
        gold = bench.gold.get(t["id"], set()) & known
        case = {"id": t["id"], "split": t["split"], "tags": [t["repo"]]}
        if not gold:
            run.record(case, {"status": "no_gold", "repo": t["repo"]}, {})
            return
        system = evals_agent.session_prompt({"cwd": _folder(t["repo"])})
        attempt = evals_agent.run_agent(PROMPT.format(issue=_query(t, query)), model, system, mcp, timeout)
        answer, row, transcript = attempt["answer"], attempt["row"], attempt["transcript"]
        cited, formatted = _cited(answer, known)
        seen = {x for s in transcript.get("steps", []) if s["kind"] == "tool_result" for x in _ids(s["text"], known)}
        opened = {
            x
            for c in transcript.get("tool_calls", [])
            if c.get("tool") == "read_chat"
            for x in _ids(str((c.get("input") or {}).get("chat_id", "")), known)
        }
        row.update(
            repo=t["repo"],
            cited=cited,
            n_cited=len(cited),
            gold_cited=len(gold & set(cited)),
            hit=bool(gold & set(cited)),
            top1=bool(cited) and cited[0] in gold,
            seen=bool(gold & seen),
            opened=bool(gold & opened),
            formatted=formatted,
            gold=sorted(gold),
        )
        written = run.record(case, row, {"gold": sorted(gold), **transcript})
        with lock:
            done[0] += 1
            label = t["id"] if t["split"] != "test" else "test task"
            outcome = ("hit" if row["hit"] else "miss") if row["status"] == h.OK else row["status"]
            print(f"[{done[0]}/{len(tasks)}] {label}: {outcome} {written.get('seconds', 0):.0f}s", file=sys.stderr)

    leaks_before = evals_agent._leaks()
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, tasks))
    leaks = sorted(set(evals_agent._leaks()) - set(leaks_before))

    scored = [r for r in run.rows if r["status"] == h.OK]
    metrics = {
        "hit": h.ci([float(r["hit"]) for r in scored]),
        "top1": h.ci([float(r["top1"]) for r in scored]),
        "precision": h.ratio_ci([(r["gold_cited"], r["n_cited"]) for r in scored]),
        "gold_seen": h.ci([float(r["seen"]) for r in scored]),
        "gold_opened": h.ci([float(r["opened"]) for r in scored]),
        "named_none": h.ci([float(not r["n_cited"]) for r in scored]),
    }
    tools: dict[str, int] = {}
    for r in scored:
        for name, n in r["tools"].items():
            tools[name] = tools.get(name, 0) + n
    summary = {
        "model": model,
        **metrics,
        "tokens_in": h.spread([r["tokens_in"] for r in scored]),
        "tokens_out": h.spread([r["tokens_out"] for r in scored]),
        "tool_calls": {**h.spread([r["tool_calls"] for r in scored]), "by_tool": dict(sorted(tools.items()))},
        "tool_search_calls": h.spread([r["tool_search_calls"] for r in scored]),
        "tool_result_chars": h.spread([r["tool_chars"] for r in scored]),
        "chats_named": h.spread([r["n_cited"] for r in scored]),
        "seconds": h.spread([r["seconds"] for r in scored]),
        "cost_usd_api_equivalent": round(sum(r["cost_usd"] for r in scored), 2),
        "unformatted_replies": sum(not r["formatted"] for r in scored),
        "plumbing": h.plumbing(run.rows),
        "transcript_leaks": leaks,
        "warnings": h.headroom({"hit": metrics["hit"]}),
    }
    if leaks:
        summary["warnings"].append(f"{len(leaks)} folders appeared in {evals_agent.PROJECTS}: a run saved a transcript")
    if against:
        value = lambda r: float(r["hit"]) if r.get("hit") is not None else None  # noqa: E731
        hits = {r["id"]: float(r["hit"]) for r in scored}
        summary["against"] = {"run": against, "hit": h.paired(hits, h.against(against, value))}
    headline = (
        f"hit={h.fmt(metrics['hit'])} precision={metrics['precision']['mean']} "
        f"tokens_in_median={summary['tokens_in'].get('median')} tools_median={summary['tool_calls'].get('median')} "
        f"model={model}"
    )
    if against:
        headline += f" Δhit={h.fmt(summary['against']['hit'], signed=True)}"
    columns = [
        ("repo", "repo"),
        ("hit", "hit"),
        ("named", "cited"),
        ("tokens in", "tokens_in"),
        ("tools", "tool_calls"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


def _agent_home(bench: Bench, home: str, memory: str, embedder=None, cache=None) -> None:
    """A recall home holding only the experience chats. Its config names no agent source, so
    the MCP server never indexes live history into it; with embeddings, the vectors are made
    here and the server embeds only queries."""
    os.makedirs(home, exist_ok=True)
    os.makedirs(memory, exist_ok=True)
    lines = ['sources = ["frozen-for-evals"]  # matches no agent: this index holds only the benchmark']
    if embedder is not None:
        lines += ["", "[embeddings]", f'provider = "{embedder.provider}"', f'name = "{embedder.name}"']
        if embedder.base_url:
            lines.append(f'base_url = "{embedder.base_url}"')
    with open(os.path.join(home, "config.toml"), "w") as f:
        f.write("\n".join(lines) + "\n")
    for iid, s in bench.sessions.items():
        s.project = _folder(bench.repo_of[iid])
    _index(list(bench.sessions.values()), embedder, cache, os.path.join(home, "recall.db")).close()


def _folder(repo: str) -> str:
    """The project folder a repo's chats ran in; the agent is told the same folder."""
    return f"/work/{repo.split('/')[-1]}"


def _ids(text: str, known: set[str]) -> list[str]:
    out = []
    for x in CHAT_ID.findall(text or ""):
        if x in known and x not in out:
            out.append(x)
    return out


def _cited(answer: str, known: set[str]) -> tuple[list[str], bool]:
    """The chats the reply names on its CHATS line (or anywhere, if it has none), in order."""
    lines = CHATS_LINE.findall(answer or "")
    if lines:
        return _ids(lines[-1], known), True
    return _ids(answer, known), False
