"""Exploration cache evals (explore.py), on the owner's own past exploration subagents.

offline  (`recall eval explore`) Replays every past exploration in time order: what the cache
         would have served from reports finished before it began, and how much of the new
         report the served one already held, measured by the files the two reports cite (the
         share of the new report's files the served report also cites). Cursor records no
         end time, so an earlier report counts as available only if it began 10 minutes
         before the new one, which keeps parallel siblings apart. The earlier half of the
         cases (in time) is train, where the gate's threshold is chosen; the later half is
         test. Baselines on the same cases: the newest earlier report of the repo (no
         matching), and the best earlier report in hindsight (an upper bound).
         --judge MODEL with --limit N: a judge reads N served test cases (the new task and
         report, and the block exactly as the subagent would have got it at the new commit)
         and says how much of the new report the block held and whether it was still valid.

agent    (`recall eval explore --agent MODEL`) Reruns --limit past exploration prompts that the
         cache serves, headless in Claude Code, in a `git clone --shared` scratch copy at the
         commit of the original exploration: a parent session calls the Agent tool once with
         subagent_type Explore and the original prompt, with recall's hook (arm "with") or
         without it ("without"), --repeats times each. It measures the subagent's tokens, tool
         calls and time from Claude Code's own accounting, and a blind judge compares the two
         reports of each repeat. The copies only read; Ali's working repos are never touched.
"""

import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from . import evals_harness as h
from . import explore

GAP = 600  # seconds an earlier exploration must have started before the new one
USEFUL = 0.3  # a served report is useful if it cites this share of the new report's files
TARGET = 0.65  # the gate: the lowest threshold where this share of served reports is useful (train)
SWEEP = [round(0.25 + 0.05 * i, 2) for i in range(8)]
SCRATCH = os.path.expanduser("~/.agents/recall-eval/scratch/explore-agent")


# --- offline replay -----------------------------------------------------------------------


def _key(path: str) -> str:
    """A cited file by its last two path parts, so the same file cited from two checkouts
    (or relative and absolute) is one file."""
    return "/".join(path.strip("./").split("/")[-2:]).lower()


def _files(report: str) -> set[str]:
    return {_key(p) for p in explore.cited_files(report)}


def build(index) -> sqlite3.Connection:
    """The owner's past explorations, in a throwaway database."""
    con = explore.connect(":memory:")
    explore.backfill(con, index)
    return con


def cases(con) -> tuple[list[dict], dict[str, set[str]]]:
    """Every exploration with an earlier one in its repo and at least three cited files, oldest
    first; the earlier half is train."""
    rows = con.execute("select * from explorations order by coalesce(started_at, created_at)").fetchall()
    files = {r["id"]: _files(r["report"]) for r in rows}
    out = []
    for r in rows:
        start = r["started_at"] or r["created_at"]
        earlier = con.execute(
            "select id, created_at from explorations where repo = ? and created_at < ? order by created_at desc",
            (r["repo"], start - GAP),
        ).fetchall()
        if earlier and len(files[r["id"]]) >= 3:
            out.append({"id": r["id"], "row": r, "start": start, "earlier": earlier})
    half = len(out) // 2
    for i, c in enumerate(out):
        c["split"] = "train" if i < half else "test"
    return out, files


def replay(con, c: dict, files: dict) -> dict:
    """What the cache would have served for one past exploration, and how good it was."""
    mine = files[c["id"]]

    def recall_of(other_id: str) -> float:
        return len(mine & files[other_id]) / len(mine)

    found = explore.candidates(con, c["row"]["repo"], c["row"]["prompt"], before=c["start"] - GAP)
    pick = explore.best(found, 0.0)
    row = {
        "status": h.OK,
        "files": len(mine),
        "earlier": len(c["earlier"]),
        "latest_recall": recall_of(c["earlier"][0]["id"]),
        "oracle_recall": max(recall_of(e["id"]) for e in c["earlier"]),
        "top_score": round(found[0].score, 4) if found else 0.0,
        "pick_score": round(next(m.score for m in found if m.row["id"] == pick.row["id"]), 4) if pick else 0.0,
        "pick_recall": recall_of(pick.row["id"]) if pick else 0.0,
        "pick_age_days": round((c["start"] - pick.row["created_at"]) / 86400, 2) if pick else None,
        "pick": pick.row["id"] if pick else None,
    }
    row["served"] = int(pick is not None and found[0].score >= explore.THRESHOLD)
    row["useful"] = int(row["pick_recall"] >= USEFUL)
    return row


def _sweep(rows: list[dict]) -> list[dict]:
    out = []
    for th in SWEEP:
        served = [r for r in rows if r["top_score"] >= th]
        out.append(
            {
                "threshold": th,
                "serve_rate": round(len(served) / len(rows), 3) if rows else 0,
                "useful_when_served": round(sum(r["useful"] for r in served) / len(served), 3) if served else None,
                "recall_when_served": round(sum(r["pick_recall"] for r in served) / len(served), 3) if served else None,
            }
        )
    return out


def offline(run, judge: str | None, sample: int | None) -> dict:
    con = build(h.corpus())
    all_cases, files = cases(con)
    rows = []
    for c in all_cases:
        row = replay(con, c, files)
        rows.append(run.record({"id": c["id"], "split": c["split"]}, row, {"served_from": row["pick"]}))
    summary = {"cases": {s: sum(1 for r in rows if r["split"] == s) for s in ("train", "test")}}
    train = [r for r in rows if r["split"] == "train"]
    sweep = _sweep(train)
    chosen = next((s["threshold"] for s in sweep if (s["useful_when_served"] or 0) >= TARGET), None)
    summary["train_sweep"] = sweep
    summary["threshold_chosen_on_train"] = chosen
    summary["threshold_in_code"] = explore.THRESHOLD
    for split in ("train", "test"):
        part = [r for r in rows if r["split"] == split]
        served = [r for r in part if r["served"]]
        summary[split] = {
            "serve_rate": h.ci([r["served"] for r in part]),
            "useful_when_served": h.ci([r["useful"] for r in served]) if served else None,
            "file_recall_when_served": h.ci([r["pick_recall"] for r in served]) if served else None,
            "latest_file_recall_same_cases": h.ci([r["latest_recall"] for r in served]) if served else None,
            "oracle_file_recall_same_cases": h.ci([r["oracle_recall"] for r in served]) if served else None,
            "file_recall_all_top1": h.ci([r["pick_recall"] for r in part]),
            "latest_file_recall_all": h.ci([r["latest_recall"] for r in part]),
            "served_older_than_1d": round(sum(1 for r in served if r["pick_age_days"] > 1) / len(served), 3)
            if served
            else None,
        }
    if judge:
        summary["judge"] = _judge_offline(con, all_cases, rows, judge, sample or 40, run)
    test = summary["test"]
    headline = (
        f"test serve={test['serve_rate']['mean']:.3f} useful|served={_m(test['useful_when_served'])} "
        f"file_recall|served={_m(test['file_recall_when_served'])} "
        f"vs latest={_m(test['latest_file_recall_same_cases'])} "
        f"oracle={_m(test['oracle_file_recall_same_cases'])} threshold={explore.THRESHOLD} (train picks {chosen})"
    )
    if "judge" in summary:
        j = summary["judge"]
        headline += (
            f" judge n={j['n']} coverage={_m(j['coverage'])} stale={_m(j['stale'])} misleading={_m(j['misleading'])}"
        )
    columns = [("served", "served"), ("score", "top_score"), ("recall", "pick_recall"), ("latest", "latest_recall")]
    return run.finish(summary, headline, columns)


def _m(s: dict | None) -> str:
    return f"{s['mean']:.3f}" if s and s.get("mean") is not None else "-"


JUDGE_SYSTEM = (
    "You grade whether an earlier exploration report of a code repository, handed to a new exploration "
    "of the same repository, would have helped it. Judge only from the texts given. Return JSON."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "coverage": {"type": "integer", "minimum": 0, "maximum": 100},
        "stale": {"type": "integer", "minimum": 0, "maximum": 100},
        "misleading": {"type": "boolean"},
        "saves_work": {"enum": ["none", "some", "much"]},
    },
    "required": ["reasoning", "coverage", "stale", "misleading", "saves_work"],
}
JUDGE_PROMPT = """A new exploration subagent was given TASK. Before starting, it could have been handed EARLIER
(an earlier exploration's findings, with the commit they were made at and what changed since). NEW REPORT is
what the new exploration actually found, at the new commit.

Answer:
- coverage: of NEW REPORT's substantive findings that answer TASK (files, symbols, behaviour, conclusions),
  what percentage does EARLIER already state? 0 = none, 100 = all.
- stale: of EARLIER's findings that bear on TASK, what percentage are contradicted by NEW REPORT or are
  likely no longer true given the changes EARLIER lists? 0 = none.
- misleading: would starting from EARLIER likely lead the new exploration to a wrong answer on TASK?
- saves_work: how much exploration would EARLIER have saved: none, some or much.

<task>
{task}
</task>

<earlier>
{earlier}
</earlier>

<new_report>
{report}
</new_report>"""


def _judge_offline(con, all_cases, rows, judge: str, sample: int, run) -> dict:
    """A judge reads a fixed sample of served test cases."""
    by_id = {c["id"]: c for c in all_cases}
    served = sorted(r["id"] for r in rows if r["split"] == "test" and r["served"])
    picked = random.Random(h.SEED).sample(served, min(sample, len(served)))
    pick_of = {r["id"]: r["pick"] for r in rows}
    client = h.judge_client(judge)
    verdicts = []

    def one(cid: str):
        c = by_id[cid]
        s = con.execute("select * from explorations where id = ?", (pick_of[cid],)).fetchone()
        here = explore.Checkout(s["root"], s["repo"], c["row"]["commit_sha"], None)
        earlier = explore.block(s, here, now=c["start"], to=c["row"]["commit_sha"])
        prompt = JUDGE_PROMPT.format(task=c["row"]["prompt"][:4000], earlier=earlier, report=c["row"]["report"][:16000])
        v = client.json("explore-judge", JUDGE_SYSTEM, prompt, JUDGE_SCHEMA)
        return cid, v, len(earlier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for cid, v, chars in pool.map(one, picked):
            verdicts.append(v | {"id": cid, "block_chars": chars})
    with open(os.path.join(run.dir, "test", "judge.jsonl"), "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v) + "\n")
    return {
        "n": len(verdicts),
        "judge": judge,
        "coverage": h.ci([v["coverage"] / 100 for v in verdicts]),
        "stale": h.ci([v["stale"] / 100 for v in verdicts]),
        "misleading": h.ci([int(v["misleading"]) for v in verdicts]),
        "saves_much": h.ci([int(v["saves_work"] == "much") for v in verdicts]),
        "saves_none": h.ci([int(v["saves_work"] == "none") for v in verdicts]),
        "block_chars_mean": round(sum(v["block_chars"] for v in verdicts) / max(1, len(verdicts))),
    }


# --- agent replay ---------------------------------------------------------------------------

PARENT = """Call the Agent tool exactly once, with subagent_type "Explore", description "explore", \
run_in_background false, and as prompt exactly the text between <task> and </task>, unchanged. When it \
returns, reply DONE and nothing else.

<task>
{prompt}
</task>"""


def agent(run, model: str, judge: str, limit: int, repeats: int, workers: int, timeout: int) -> dict:
    if h.same_model(model, judge):
        raise SystemExit(f"the judge ({judge}) must differ from the model under test ({model})")
    con = build(h.corpus())
    all_cases, files = cases(con)
    served = []
    for c in all_cases:
        if c["split"] != "test" or not os.path.isdir(c["row"]["root"] or ""):
            continue
        r = replay(con, c, files)
        if r["served"]:
            served.append((c, r))
    random.Random(h.SEED).shuffle(served)
    chosen = served[:limit]
    os.makedirs(SCRATCH, exist_ok=True)
    work = tempfile.mkdtemp(prefix=os.path.basename(run.dir) + "-", dir=SCRATCH)
    jobs = [(c, r, arm, rep) for c, r in chosen for rep in range(repeats) for arm in ("without", "with")]
    results: dict[tuple, dict] = {}

    def one(job):
        c, _, arm, rep = job
        return (c["id"], arm, rep), _run_one(con, c, arm, rep, model, timeout, work)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as pool:
        for key, res in pool.map(one, jobs):
            results[key] = res
    grader = h.judge_client(judge)
    rows = []
    for c, r in chosen:
        for rep in range(repeats):
            a, b = results[(c["id"], "without", rep)], results[(c["id"], "with", rep)]
            verdict = _judge_pair(grader, c, a, b, rep) if a["status"] == h.OK and b["status"] == h.OK else None
            for arm, res in (("without", a), ("with", b)):
                row = {k: v for k, v in res.items() if k not in ("report", "block")}
                row |= {"arm": arm, "repeat": rep, "score": r["pick_score"]}
                if verdict:
                    row["judge_score"] = verdict[arm]
                    row["judge_win"] = 1.0 if verdict["winner"] == arm else 0.5 if verdict["winner"] == "tie" else 0.0
                transcript = {"result": res, "judge": verdict}
                rows.append(run.record({"id": f"{c['id']}-{arm}", "split": "test"}, row, transcript))
    return run.finish(_agent_summary(rows, model, judge, repeats), _agent_headline(rows), [("arm", "arm")])


def _run_one(con, c: dict, arm: str, rep: int, model: str, timeout: int, work: str) -> dict:
    """One headless Claude Code run in a fresh scratch clone at the original exploration's commit."""
    row = c["row"]
    folder = os.path.join(work, f"{re.sub(r'[^A-Za-z0-9]+', '-', c['id'])[:40]}-{arm}-{rep}")
    repo = os.path.join(folder, os.path.basename(row["root"]))
    os.makedirs(folder, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", row["root"], repo], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", row["commit_sha"]], check=True)
    origin = subprocess.run(["git", "-C", row["root"], "remote", "get-url", "origin"], capture_output=True, text=True)
    if origin.returncode == 0:  # so recall sees the same repo as the original checkout
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", origin.stdout.strip()], check=True)
    home = os.path.join(folder, "home")
    os.makedirs(home)
    if arm == "with":
        # Only the explorations that had begun 10 minutes before this one.
        db = explore.connect(os.path.join(home, "recall.db"))
        cols = [d[1] for d in con.execute("pragma table_info(explorations)")]
        for e in con.execute("select * from explorations where created_at < ?", (c["start"] - GAP,)):
            db.execute(f"insert into explorations values ({','.join('?' * len(cols))})", tuple(e))
            db.execute("insert into explorations_fts values (?,?,?)", (e["id"], e["prompt"], e["report"]))
        db.commit()
        # This checkout's code answers the hook, with its own data folder.
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hook = " ".join(
            shlex.quote(x)
            for x in ("env", f"RECALL_HOME={home}", f"PYTHONPATH={src}", sys.executable, "-m", "recall", "hook")
        )
        hook += " --agent claude"
        settings = {
            "hooks": {"PreToolUse": [{"matcher": "Agent|Task", "hooks": [{"type": "command", "command": hook}]}]}
        }
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "settings.json"), "w") as f:
            json.dump(settings, f)
    from .llm.agent_cli import plan_env

    env = plan_env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    for name in ("RECALL_HOME", "RECALL_MEMORY", "RECALL_RUN_DIR"):  # the installed recall uses its own
        env.pop(name, None)
    cmd = [
        "claude",
        "-p",
        "--model",
        model.removeprefix("claude:"),
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            [*cmd, PARENT.format(prompt=row["prompt"])],
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = proc.stdout
        status = h.OK
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        status = "timeout"
    res = _measure(out) | {"wall_s": round(time.time() - started, 1)}
    if status == h.OK and not res.get("report"):
        status = "no_report"
    res["status"] = status
    usage = os.path.join(home, "usage.jsonl")
    if os.path.exists(usage):
        with open(usage) as f:
            res["hook"] = [json.loads(line) for line in f if '"explore_start"' in line]
    res["served"] = int(any(x.get("served") for x in res.get("hook", [])))
    shutil.rmtree(repo, ignore_errors=True)
    return res


def _measure(stream: str) -> dict:
    """The Explore subagent's own numbers, from Claude Code's stream-json events."""
    seen, tokens_in, tokens_out, tools, report, meta, cost = set(), 0, 0, 0, None, {}, None
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "assistant" and d.get("parent_tool_use_id"):
            msg = d.get("message") or {}
            tools += sum(1 for b in msg.get("content") or [] if b.get("type") == "tool_use")
            if msg.get("id") not in seen:
                seen.add(msg.get("id"))
                u = msg.get("usage") or {}
                tokens_in += (
                    u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                )
                tokens_out += u.get("output_tokens", 0)
        elif d.get("type") == "user" and not d.get("parent_tool_use_id") and isinstance(d.get("tool_use_result"), dict):
            r = d["tool_use_result"]
            if r.get("agentType"):
                meta = r
                content = r.get("content")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                report = content if isinstance(content, str) else str(content)
        elif d.get("type") == "result":
            cost = d.get("total_cost_usd")
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tool_calls": meta.get("totalToolUseCount", tools),
        "subagent_s": round((meta.get("totalDurationMs") or 0) / 1000, 1),
        "cost_usd": cost,
        "report": report,
        "report_chars": len(report or ""),
    }


PAIR_SYSTEM = (
    "You compare two reports written by code-exploration agents for the same task in the same repository at "
    "the same commit. Judge which better answers the task: correct, specific (files, symbols, line-level "
    "pointers), complete for what was asked, and not padded. Return JSON."
)
PAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "score_a": {"type": "integer", "minimum": 1, "maximum": 10},
        "score_b": {"type": "integer", "minimum": 1, "maximum": 10},
        "winner": {"enum": ["A", "B", "tie"]},
    },
    "required": ["reasoning", "score_a", "score_b", "winner"],
}
PAIR_PROMPT = """<task>
{task}
</task>

A reference report written for the same task some time ago, for orientation only (it may be partly out of date):
<reference>
{reference}
</reference>

<report_a>
{a}
</report_a>

<report_b>
{b}
</report_b>

Score each report 1-10 for how well it answers the task, and name the better one (or tie)."""


def _judge_pair(grader, c: dict, without: dict, with_: dict, rep: int) -> dict:
    """Blind: the arms' order is shuffled per case and repeat, and nothing says which is which.
    The served block is not shown; only the reports."""
    flip = random.Random(f"{c['id']}-{rep}").random() < 0.5
    a, b = (with_, without) if flip else (without, with_)
    prompt = PAIR_PROMPT.format(
        task=c["row"]["prompt"][:4000],
        reference=c["row"]["report"][:12000],
        a=_unmark(a["report"])[:16000],
        b=_unmark(b["report"])[:16000],
    )
    v = grader.json("explore-pair-judge", PAIR_SYSTEM, prompt, PAIR_SCHEMA)
    arm_a, arm_b = ("with", "without") if flip else ("without", "with")
    winner = {"A": arm_a, "B": arm_b, "tie": "tie"}[v["winner"]]
    return {"winner": winner, arm_a: v["score_a"], arm_b: v["score_b"], "reasoning": v["reasoning"], "a_was": arm_a}


def _unmark(report: str | None) -> str:
    """Hide words that would tell the judge a report started from an earlier one."""
    return re.sub(r"(?i)\b(earlier|previous|prior) (exploration|report)\b", "background", report or "")


def _agent_summary(rows: list[dict], model: str, judge: str, repeats: int) -> dict:
    ok = [r for r in rows if r["status"] == h.OK]
    cases_ok = {r["id"].rsplit("-", 1)[0] for r in ok}

    def per_case(arm: str, key: str) -> dict[str, float]:
        vals: dict[str, list] = {}
        for r in ok:
            if r["arm"] == arm and r.get(key) is not None:
                vals.setdefault(r["id"].rsplit("-", 1)[0], []).append(r[key])
        return {k: sum(v) / len(v) for k, v in vals.items()}

    out = {"model": model, "judge": judge, "repeats": repeats, "cases": len(cases_ok), "plumbing": h.plumbing(rows)}
    for key in (
        "tokens_in",
        "tokens_out",
        "tool_calls",
        "subagent_s",
        "wall_s",
        "cost_usd",
        "report_chars",
        "judge_score",
    ):
        w, wo = per_case("with", key), per_case("without", key)
        out[key] = {"with": h.ci(list(w.values())), "without": h.ci(list(wo.values())), "paired": h.paired(w, wo)}
    out["judge_win_with"] = h.ci([r["judge_win"] for r in ok if r["arm"] == "with" and "judge_win" in r])
    out["served_rate_with"] = h.ci([r["served"] for r in ok if r["arm"] == "with"])
    return out


def _agent_headline(rows: list[dict]) -> str:
    ok = [r for r in rows if r["status"] == h.OK]

    def mean(arm: str, key: str) -> float:
        v = [r[key] for r in ok if r["arm"] == arm and r.get(key) is not None]
        return sum(v) / len(v) if v else float("nan")

    parts = [f"{k} {mean('without', k):.0f}->{mean('with', k):.0f}" for k in ("tokens_in", "tool_calls", "subagent_s")]
    wins = [r["judge_win"] for r in ok if r["arm"] == "with" and "judge_win" in r]
    return " ".join(parts) + f" judge_win_with={sum(wins) / len(wins) if wins else float('nan'):.2f} n={len(ok)}"


def from_args(a) -> None:
    if a.agent is not None:  # --agent, optionally naming the model
        model = a.agent or "claude-haiku-5-5"
        timeout = max(a.timeout, 1200)  # an exploration at medium thoroughness can take minutes
        settings = {"model": model, "repeats": a.repeats, "limit": a.limit or 10, "workers": min(a.workers, 2)}
        settings["flags"] = "claude -p --model M --output-format stream-json --verbose --no-session-persistence"
        run = h.Run("explore_agent", "test", settings, a.who, a.runs_dir)
        agent(run, model, a.judge or h.JUDGE, a.limit or 10, max(1, a.repeats), a.workers, timeout)
        return
    run = h.Run("explore_offline", "all", {"gap_s": GAP, "useful": USEFUL, "target": TARGET}, a.who, a.runs_dir)
    offline(run, a.judge, a.limit)
