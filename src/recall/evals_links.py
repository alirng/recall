"""Commit links eval: are the chats recall links to a commit the ones that produced it?

Samples commits from your own repos (git is only read), stratified by the agent whose chats
touched them, and asks a judge model, for each commit and candidate chat, whether the chat
wrote part of the commit or made it. Candidates are every chat that edited one of the
commit's files in the linking window, plus chats that ran `git commit` in the repo around
then, so links recall missed are judged too. A chat that changed files only through shell
commands is not a candidate, so recall here is an upper bound.

  precision  judged yes among the pairs recall links
  recall     pairs recall links among those judged yes

Cases split by commit sha: train is a first hex digit 0-7, test 8-f. Each case is one
commit; the transcripts hold chat text and stay in the run folder.

  recall eval links [--split train|test] [--limit 60] [--judge MODEL] [--workers 2]
"""

import random
import time
from concurrent.futures import ThreadPoolExecutor

from . import commits, paths
from . import evals_harness as h
from .chats import store
from .chats.sources import compressed

AGENTS = ("claude", "codex", "cursor")
DIFF_CHARS, CHAT_CHARS = 7000, 7000
MAX_CANDIDATES = 5
JUDGE_SYSTEM = """You check whether a coding-agent chat produced a git commit.

You get one commit (its message and diff) and one chat: the edits its agent made to the
files the commit touches, what the person asked before each edit, and any git commit
commands the chat ran with their output.

Answer "yes" if the agent in this chat wrote at least part of the code this commit adds
(the same lines, or clearly the same change even if later reformatted, rebased or
squash-merged), or ran the `git commit` that created this commit.
Answer "no" if the chat's edits are a different change to the same files (an earlier or a
later one), were replaced before the commit, or the overlap is only boilerplate that any
change would contain.
Answer "unsure" only when the evidence cannot decide it."""
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"enum": ["yes", "no", "unsure"]}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def split_of(sha: str) -> str:
    return "train" if sha[0] in "01234567" else "test"


def population(con) -> list[dict]:
    """Every commit some chat could have produced, with its candidate chats and the links."""
    agent = dict(con.execute("select id, agent from sessions"))
    out = []
    for (repo,) in con.execute("select distinct repo from link_commits").fetchall():
        edits: dict[str, list] = {}
        for r in con.execute("select session_id, path, first_at, last_at from link_edits where repo=?", (repo,)):
            edits.setdefault(r["path"], []).append(r)
        events = con.execute(
            "select e.session_id, e.at from link_events e where e.kind='commit' and e.session_id in "
            "(select session_id from link_edits where repo=? union select id from sessions where project_key=?)",
            (repo, repo),
        ).fetchall()
        files: dict[str, list[str]] = {}
        for r in con.execute("select sha, path from link_commit_files where repo=?", (repo,)):
            files.setdefault(r["sha"], []).append(r["path"])
        linked: dict[str, dict[str, str]] = {}
        for r in con.execute("select sha, session_id, how from commit_links where repo=?", (repo,)):
            linked.setdefault(r["sha"], {})[r["session_id"]] = r["how"]
        for c in con.execute("select sha, at, subject from link_commits where repo=?", (repo,)).fetchall():
            near: dict[str, float] = {}
            for p in files.get(c["sha"], []):
                for e in edits.get(p, []):
                    if e["first_at"] - commits.SLACK <= c["at"] <= e["last_at"] + commits.WINDOW:
                        near[e["session_id"]] = max(near.get(e["session_id"], 0), e["last_at"])
            for e in events:
                if (e["at"] or 0) - 120 <= c["at"] <= (e["at"] or 0) + 12 * 3600:
                    near.setdefault(e["session_id"], e["at"])
            if not near and c["sha"] not in linked:
                continue
            mine = linked.get(c["sha"], {})
            # Linked chats first, then the chats that touched the files closest before the commit.
            ranked = sorted(near, key=lambda s: (s not in mine, abs(c["at"] - near[s])))
            cands = list(dict.fromkeys([*mine, *ranked]))[: max(MAX_CANDIDATES, len(mine))]
            out.append(
                {
                    "id": f"{repo.split('/')[-1]}@{c['sha'][:10]}",
                    "repo": repo,
                    "sha": c["sha"],
                    "split": split_of(c["sha"]),
                    "candidates": cands,
                    "linked": mine,
                    "agents": sorted({agent.get(s, "?") for s in cands}),
                }
            )
    return out


def sample(pop: list[dict], split: str, limit: int, seed: int = h.SEED) -> list[dict]:
    """About limit/3 commits per agent whose chats touched them, without repeats."""
    rng = random.Random(seed)
    pool = [c for c in pop if split == "all" or c["split"] == split]
    picked: dict[str, dict] = {}
    for agent in AGENTS:
        mine = [c for c in pool if agent in c["agents"] and c["id"] not in picked]
        rng.shuffle(mine)
        for c in mine[: limit // len(AGENTS)]:
            c["tags"] = [agent]
            picked[c["id"]] = c
    return list(picked.values())


def _commit_text(gitdir: str | None, sha: str, focus: set[str]) -> str:
    if not gitdir:
        return f"commit {sha} (no clone of this repo is on disk any more)"
    show = ("show", "--no-color", "--no-ext-diff", "--no-textconv")
    head = commits.git(gitdir, *show, "-s", "--format=commit %H%nDate: %ci%nAuthor: %an%n%n%B", sha, gitdir=True)
    stat = commits.git(gitdir, *show, "--stat=200", "--format=", sha, gitdir=True) or ""
    diff = commits.git(gitdir, *show, "-U2", "--format=", sha, gitdir=True) or ""
    parts = diff.split("\ndiff --git ")
    parts = [parts[0], *("diff --git " + p for p in parts[1:])] if parts else []
    first = [p for p in parts if any(f" b/{f}\n" in p.split("\n", 1)[0] + "\n" for f in focus)]
    rest = [p for p in parts if p not in first]
    body = ""
    for p in first + rest:
        if len(body) + len(p) > DIFF_CHARS:
            body += p[: max(0, DIFF_CHARS - len(body))] + "\n[diff cut]\n"
            break
        body += p
    return f"{head or ''}\n{stat}\n{body}"


def _chat_text(con, sid: str, files: list[str], at: float) -> str:
    r = con.execute("select agent, title, source, started_at, updated_at from sessions where id=?", (sid,)).fetchone()
    if not r:
        return "(chat not in the index)"
    head = (
        f"Chat {sid} ({r['agent']}), titled {r['title']!r}, from {store.fmt_time(r['started_at'])} to "
        f"{store.fmt_time(r['updated_at'])}. The commit was made {store.fmt_time(at)}.\n"
    )
    reader = commits.READERS.get(r["agent"])
    if not reader or not r["source"] or not compressed.resolve(r["source"]):
        return head + "(transcript no longer on disk)"
    work = reader(r["source"])
    out, last_ask = [head], None
    for e in work.edits:
        rel = next((f for f in files if e.path == f or e.path.endswith("/" + f)), None)
        if not rel:
            continue
        if e.ask and e.ask != last_ask:
            out.append(f'\nThe person asked: "{e.ask[:400]}"')
            last_ask = e.ask
        when = store.fmt_time(e.at) or "time unknown"
        added = "\n".join("+" + x for x in e.added[:40]) + ("\n+…" if len(e.added) > 40 else "")
        removed = "\n".join("-" + x for x in e.removed[:10]) + ("\n-…" if len(e.removed) > 10 else "")
        kind = "wrote the whole file" if e.whole else "edited"
        out.append(f"\n[{when}] {kind} {rel}\n{removed}\n{added}".rstrip())
    for ev_at, kind, text, detail in work.events:
        if kind == "commit" and ev_at and abs(ev_at - at) < 86400:
            out.append(f"\n[{store.fmt_time(ev_at)}] ran: {text[:600]}\noutput: {detail[:600]}")
    if len(out) == 1:
        out.append("(no edits to these files and no commit commands near the commit)")
    text = "\n".join(out)
    return text if len(text) <= CHAT_CHARS else text[:CHAT_CHARS] + "\n[chat evidence cut]"


def _judge_case(con_path: str, grader, case: dict) -> tuple[dict, dict]:
    con = store.connect(con_path)
    row = con.execute(
        "select gitdir, at from link_commits where repo=? and sha=?", (case["repo"], case["sha"])
    ).fetchone()
    files = [
        r[0]
        for r in con.execute("select path from link_commit_files where repo=? and sha=?", (case["repo"], case["sha"]))
    ]
    pairs, transcript = [], {"commit": None, "pairs": []}
    agent = dict(con.execute("select id, agent from sessions"))
    for sid in case["candidates"]:
        chat_files = [
            r[0]
            for r in con.execute("select path from link_edits where session_id=? and repo=?", (sid, case["repo"]))
            if r[0] in files
        ]
        commit_text = _commit_text(commits.live_gitdir(con, case["repo"], row["gitdir"]), case["sha"], set(chat_files))
        transcript["commit"] = transcript["commit"] or commit_text
        chat_text = _chat_text(con, sid, files, row["at"])
        prompt = f"COMMIT\n{commit_text}\n\nCHAT\n{chat_text}\n\nDid this chat produce this commit?"
        try:
            verdict = grader.json("links-judge", JUDGE_SYSTEM, prompt, SCHEMA)
            status = h.OK
        except Exception as e:
            verdict, status = {"verdict": "error", "reason": repr(e)[:300]}, "judge_error"
        pairs.append(
            {
                "chat": sid,
                "agent": agent.get(sid),
                "linked": case["linked"].get(sid),
                "verdict": verdict["verdict"],
                "status": status,
            }
        )
        transcript["pairs"].append({"chat": sid, "linked": case["linked"].get(sid), "evidence": chat_text, **verdict})
    con.close()
    return {"pairs": pairs}, transcript


def score(rows: list[dict], agent: str | None = None, how: str | None = None) -> dict:
    prec, rec = [], []
    for r in rows:
        ps = [p for p in r["pairs"] if p["verdict"] in ("yes", "no") and (not agent or p["agent"] == agent)]
        linked = [p for p in ps if p["linked"] and (not how or p["linked"] == how)]
        yes = [p for p in ps if p["verdict"] == "yes"]
        prec.append((sum(p["verdict"] == "yes" for p in linked), len(linked)))
        rec.append((sum(bool(p["linked"]) for p in yes), len(yes)))
    return {"precision": h.ratio_ci(prec), "recall": h.ratio_ci(rec)}


EPISODE_SYSTEM = """You check examples for a regression gym built from a developer's own coding-agent chats.

Each example claims: while an agent worked on a task, something failed (a check or command
exited non-zero, the person said the work was wrong or still broken, or a commit was
reverted), and then a commit fixed it. You get the request, the failure and the commit.

Answer "yes" if the failure is a real problem with the code under work and this commit
plausibly fixes it, so "start from the parent commit, give the request, see whether the fix
is reproduced" is a fair task. Answer "no" if the failure has nothing to do with the code
(a shell typo, a missing tool, a network or permission problem, a question rather than a
complaint), or this commit does not address it. Answer "unsure" only when the evidence
cannot decide it."""


def episode_cases(con, limit: int, seed: int = h.SEED) -> list[dict]:
    """A sample of failure-then-fix episodes, spread over the kinds of failure."""
    rng = random.Random(seed)
    found = commits.episodes(con)
    kinds = sorted({e["failure"]["kind"] for e in found})  # the failure closest before the fix
    picked = []
    for kind in kinds:
        mine = [e for e in found if e["failure"]["kind"] == kind]
        rng.shuffle(mine)
        picked += mine[: max(1, limit // len(kinds))]
    for e in picked:
        e.update(id=f"{e['repo'].split('/')[-1]}@{e['fix_commit'][:10]}", split="all", tags=[e["failure"]["kind"]])
    return picked


def run_episodes(a, con) -> None:
    cases = episode_cases(con, a.limit or 30)
    judge = a.judge or h.JUDGE
    run = h.Run("links-episodes", "all", {"cases": len(cases), "judge": judge, "note": a.note or ""}, a.who, a.runs_dir)
    grader = h.judge_client(judge)

    gitdirs = {  # looked up here: the judge runs in threads, and a connection stays in its own
        e["fix_commit"]: commits.live_gitdir(
            con,
            e["repo"],
            con.execute(
                "select gitdir from link_commits where repo=? and sha=?", (e["repo"], e["fix_commit"])
            ).fetchone()[0],
        )
        for e in cases
    }

    def one(e):
        f = e["failure"]
        shown = "\n\n".join(
            f"[{store.fmt_time(x['at'])}] {x['kind']}: {x['text']}"
            + (f"\noutput: {x['detail']}" if x.get("detail") else "")
            for x in e["failures"][-3:]
        )
        prompt = (
            f"REQUEST\n{e['prompt']}\n\nFAILURE(S), latest last\n{shown[-4000:]}\n\n"
            f"COMMIT\n{_commit_text(gitdirs[e['fix_commit']], e['fix_commit'], set(e['files']))}\n\n"
            "Is this a fair failure-then-fix example?"
        )
        try:
            v, status = grader.json("links-episode-judge", EPISODE_SYSTEM, prompt, SCHEMA), h.OK
        except Exception as err:
            v, status = {"verdict": "error", "reason": repr(err)[:300]}, "judge_error"
        row = {"status": status, "verdict": v["verdict"], "kind": f["kind"], "same_chat": e["same_chat"]}
        return run.record(e, row, {"prompt": prompt, "judge": v})

    with ThreadPoolExecutor(max(1, min(a.workers, 4))) as pool:
        rows = list(pool.map(one, cases))
    ok = [r for r in rows if r["status"] == h.OK and r["verdict"] in ("yes", "no")]
    summary = {
        "precision": h.ci([r["verdict"] == "yes" for r in ok]),
        "by_kind": {
            k: h.ci([r["verdict"] == "yes" for r in ok if r["kind"] == k]) for k in sorted({r["kind"] for r in ok})
        },
        "episodes_found": len(found := commits.episodes(con)),
        "failures_behind_them": sum(e["failure_count"] for e in found),
        "unsure": sum(r["verdict"] == "unsure" for r in rows),
        "plumbing": h.plumbing(rows),
    }
    p = summary["precision"]
    run.finish(
        summary,
        f"episode precision={p['mean']} [{p['lo']}, {p['hi']}] n={p['n']}",
        [("verdict", "verdict"), ("kind", "kind")],
    )


def from_args(a) -> None:
    con = store.connect()
    if not commits.enabled():
        raise SystemExit("commit links are off ([links] enabled = false in config.toml): nothing to evaluate")
    if getattr(a, "episodes", False):
        commits.update(con)
        run_episodes(a, con)
        return
    started = time.time()
    built = commits.update(con)
    build_s = time.time() - started
    pop = population(con)
    cases = sample(pop, a.split, a.limit or 60)
    judge = a.judge or h.JUDGE
    run = h.Run(
        "links",
        a.split,
        {"cases": len(cases), "judge": judge, "population": len(pop), "note": a.note or ""},
        a.who,
        a.runs_dir,
    )
    grader = h.judge_client(judge)
    db_path = paths.DB

    def one(case):
        result, transcript = _judge_case(db_path, grader, case)
        bad = [p for p in result["pairs"] if p["status"] != h.OK]
        row = {
            "status": h.OK if not bad else "judge_error",
            "pairs": result["pairs"],
            "linked": len(case["linked"]),
            "yes": sum(p["verdict"] == "yes" for p in result["pairs"]),
            "correct_links": sum(p["verdict"] == "yes" and bool(p["linked"]) for p in result["pairs"]),
        }
        return run.record(case, row, transcript)

    with ThreadPoolExecutor(max(1, min(a.workers, 4))) as pool:
        rows = list(pool.map(one, cases))
    ok = [r for r in rows if r["status"] == h.OK]
    summary = {
        **score(ok),
        "by_agent": {ag: score(ok, agent=ag) for ag in AGENTS},
        "by_how": {how: score(ok, how=how)["precision"] for how in ("ran", "wrote")},
        "pairs_judged": sum(len(r["pairs"]) for r in ok),
        "unsure": sum(p["verdict"] == "unsure" for r in ok for p in r["pairs"]),
        "build": {**built, "seconds": round(build_s, 1), **commits.stats(con)},
        "plumbing": h.plumbing(rows),
    }
    p, rc = summary["precision"], summary["recall"]
    headline = (
        f"precision={p['mean']} [{p['lo']}, {p['hi']}] recall={rc['mean']} [{rc['lo']}, {rc['hi']}] "
        f"commits={len(ok)} pairs={summary['pairs_judged']}"
    )
    run.finish(summary, headline, [("linked", "linked"), ("judged yes", "yes"), ("correct", "correct_links")])
