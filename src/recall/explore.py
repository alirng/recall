"""Exploration cache: an exploration subagent in a repo recall has seen before starts from the
earlier report that best answers its question, plus what changed since, instead of from zero.

Capture. Each exploration subagent's final report is stored with its repo, the commit it was
observed at (HEAD plus the files that were dirty), the agent type, and its prompt: live from
each agent's subagent-stop hook, and for past chats from the chat index (`recall explore
backfill`), with the commit estimated from the repo's git log at the report's time. A commit
cannot be worked out again later, so this table is not derived data: a chat schema bump
never drops it (store.CHAT_TABLES does not list it).

Serve. When an exploration subagent starts in a known repo, its prompt is matched against
earlier prompts and reports for that repo. Below a calibrated score recall abstains; above
it, the subagent gets the report's findings as pointers (files, symbols, conclusions), its
commit and age, what changed since that commit (a capped diff stat), and one line asking it
to verify claims about changed files and to extend rather than redo. A generic repo overview
does not help agents (arXiv 2602.11988), so only a report that matches this prompt is served.
Agents in any harness can ask for the same through MCP: project_memory(path, task).

The hook dispatcher (integrations/hooks.py) calls on_start / on_stop; the table above them
says where each agent puts the prompt and the report. `recall eval explore` replays the
owner's past explorations (evals_explore.py).
"""

import json
import math
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field

from . import paths

SCHEMA = """
create table if not exists explorations (
  id text primary key, agent text, agent_type text, repo text, root text, folder text,
  commit_sha text, commit_source text, branch text, dirty text, created_at real,
  prompt text, report text, origin text,
  prompt_terms text, report_terms text,  -- matching words, kept so a hook need not re-read reports
  started_at real  -- when the exploration began; created_at is when its report came
);
create index if not exists explorations_repo on explorations(repo, created_at);
create virtual table if not exists explorations_fts using fts5(
  id unindexed, prompt, report, tokenize='porter unicode61'
);
-- Prompts seen at a subagent's start, for its stop when no transcript holds the prompt
-- (Claude Code without session persistence). Kept a day.
create table if not exists exploration_starts (session text, prompt text, at real);
"""

MAX_CHARS = 5000  # the whole injected block
FINDINGS_CHARS = 3200  # of which the earlier report's pointers
DIFF_FILES = 20  # changed files listed, most-changed first
MIN_REPORT = 400  # shorter final messages are not reports
# Gate: a match is served only at or above this score (0..1); see score(). Chosen on the
# earlier half of the owner's past explorations in time order (`recall eval explore`): the
# lowest score at which at least 65% of served reports already cite 30% or more of the files
# the new report cites.
THRESHOLD = 0.40
CLOSE = 0.05  # among matches this close to the best, the newest wins
GIT_TIMEOUT = 3


def is_explorer(agent_type: str | None) -> bool:
    """Claude Code's Explore, Cursor's explore and code-explorer, Codex's explorer role."""
    return bool(agent_type) and "explor" in agent_type.lower()


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or paths.DB
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# --- git ------------------------------------------------------------------------------


def _git(folder: str, *args: str, timeout: float = GIT_TIMEOUT) -> str | None:
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.quotePath=false", "-C", folder, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


@dataclass
class Checkout:
    top: str  # this checkout's root (a worktree's own folder)
    repo: str  # project key shared by every clone and worktree (chats.projects.resolve)
    commit: str | None
    branch: str | None


def checkout(folder: str | None) -> Checkout | None:
    if not folder or not os.path.isdir(folder):
        return None
    out = _git(folder, "rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD")
    if not out:
        return None
    lines = out.split("\n")
    from .chats.projects import resolve

    project = resolve(lines[0])
    if not project:
        return None
    branch = lines[2] if len(lines) > 2 and lines[2] != "HEAD" else None
    return Checkout(lines[0], project.key, lines[1] or None, branch)


def dirty_files(top: str, limit: int = 200) -> list[str]:
    out = _git(top, "status", "--porcelain", "--untracked-files=normal") or ""
    files = [line[3:].split(" -> ")[-1].strip('"') for line in out.splitlines() if len(line) > 3]
    return files[:limit]


# --- capture --------------------------------------------------------------------------

_BLOCK = re.compile(r"\s*<recall-memory>.*?</recall-memory>\s*", re.S)


def record(
    con,
    *,
    id: str,
    agent: str,
    agent_type: str,
    folder: str | None,
    prompt: str,
    report: str,
    created_at: float | None = None,
    started_at: float | None = None,
    origin: str = "hook",
    commit: str | None = None,
    commit_source: str = "head",
    dirty: list[str] | None = None,
    repo: Checkout | None = None,
) -> bool:
    """Store one exploration report. With no commit given, the folder's HEAD and dirty
    files right now are the commit it was observed at."""
    from .chats.redact import redact  # keys and tokens never reach the table, as in the chat index

    prompt = redact(_BLOCK.sub("\n", prompt or "").strip())
    report = redact((report or "").strip())
    if len(report) < MIN_REPORT or not prompt:
        return False
    co = repo or checkout(folder)
    if not co:
        return False
    if commit is None:
        commit, dirty = co.commit, dirty_files(co.top)
    con.execute("delete from explorations_fts where id = ?", (id,))
    con.execute(
        "insert or replace into explorations values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            id,
            agent,
            agent_type,
            co.repo,
            co.top,
            folder,
            commit,
            commit_source,
            co.branch,
            json.dumps(dirty or []),
            created_at or time.time(),
            prompt,
            report,
            origin,
            " ".join(_tokens(prompt)),
            " ".join(sorted(set(_tokens(report)))),
            started_at,
        ),
    )
    con.execute("insert into explorations_fts values (?,?,?)", (id, prompt, report))
    con.commit()
    return True


# --- matching -------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_HOME = os.path.expanduser("~")
# Words every exploration prompt uses, which say nothing about which part of a repo it is about.
_BOILER = frozenset(
    "thoroughness medium quick very thorough explore exploration read only readonly repo repository "
    "codebase return report find inspect look investigate focus files file paths path symbols concise "
    "actionable notes modify modifying code current existing relevant key exact line lines".split()
)


def _tokens(text: str) -> list[str]:
    """Content words, with identifiers also split into their parts (NoteStore -> note,
    store), lower-cased and crudely stemmed so plurals meet."""
    from .chats.store import STOPWORDS

    out = []
    for w in _WORD.findall((text or "").replace(_HOME, "")):
        parts = {w.lower()}
        if len(w) > 3:
            parts |= {p.lower() for p in _CAMEL.findall(w.replace("_", " "))}
        for p in parts:
            if len(p) < 3 or p in STOPWORDS or p in _BOILER:
                continue
            out.append(p[:-1] if p.endswith("s") and len(p) > 4 else p)
    return out


def _fts_query(text: str) -> str | None:
    words = list(dict.fromkeys(_tokens(text)))[:40]
    return " OR ".join(f'"{w}"' for w in words) or None


@dataclass
class Match:
    row: sqlite3.Row
    score: float
    prompt_sim: float  # tf-idf cosine of the two prompts
    coverage: float  # share of the new prompt's weighted words the earlier prompt or report holds


def score(prompt_sim: float, coverage: float) -> float:
    """How likely the earlier report answers the new question: the new prompt's similarity
    to the earlier prompt (same question), and how much of its weighted vocabulary the
    earlier report covers (the report answers it even when asked differently)."""
    return 0.5 * prompt_sim + 0.5 * coverage


def candidates(con, repo: str, prompt: str, before: float | None = None, exclude=(), limit: int = 20) -> list[Match]:
    """Earlier explorations of this repo ranked for the prompt, each scored 0..1. before and
    exclude are for replaying history: only reports written before that time."""
    q = _fts_query(prompt)
    if not q:
        return []
    where, args = "e.repo = ?", [repo]
    if before is not None:
        where += " and e.created_at < ?"
        args.append(before)
    if exclude:
        where += f" and e.id not in ({','.join('?' * len(exclude))})"
        args += list(exclude)
    rows = con.execute(
        f"""select e.* from explorations_fts f join explorations e on e.id = f.id
            where explorations_fts match ? and {where}
            order by bm25(explorations_fts, 0, 3.0, 1.0) limit ?""",
        [q, *args, limit],
    ).fetchall()
    if not rows:
        return []
    # Inverse document frequency over this repo's earlier prompts, so words every prompt
    # shares (the repo's name, its main feature) count for little.
    prompts = [r[0].split() for r in con.execute(f"select prompt_terms from explorations e where {where}", args)]
    df: Counter = Counter()
    for p in prompts:
        df.update(set(p))
    n = len(prompts)

    def idf(w: str) -> float:
        return math.log((n + 1) / (df.get(w, 0) + 0.5))

    query = Counter(_tokens(prompt))
    qvec = {w: c * idf(w) for w, c in query.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    qmass = sum(idf(w) for w in query) or 1.0
    out = []
    for r in rows:
        theirs = Counter(r["prompt_terms"].split())
        tvec = {w: c * idf(w) for w, c in theirs.items()}
        tnorm = math.sqrt(sum(v * v for v in tvec.values())) or 1.0
        sim = sum(qvec[w] * tvec.get(w, 0) for w in qvec) / (qnorm * tnorm)
        covered = set(r["report_terms"].split()) | set(theirs)
        cov = sum(idf(w) for w in query if w in covered) / qmass
        out.append(Match(r, score(sim, cov), sim, cov))
    out.sort(key=lambda m: m.score, reverse=True)
    return out


def best(matches: list[Match], threshold: float = THRESHOLD) -> Match | None:
    """The match to serve, or None to abstain. Among matches close to the best, the newest:
    it is likelier to hold at today's commit."""
    if not matches or matches[0].score < threshold:
        return None
    top = matches[0].score
    close = [m for m in matches if m.score >= max(threshold, top - CLOSE)]
    return max(close, key=lambda m: m.row["created_at"] or 0)


# --- what the subagent gets -----------------------------------------------------------

_PATH = re.compile(r"(?<![\w@/~])((?:~?/)?(?:[\w.@-]+/)*[\w.@-]+\.(?:[a-zA-Z]{1,5}))(?::\d+(?:-\d+)?)?")
_EXTS = frozenset(
    "py ts tsx js jsx mjs cjs json md mdx toml yaml yml sql go rs java kt swift rb php sh css scss html vue svelte "
    "c h cc cpp hpp cs prisma graphql txt cfg ini lock env".split()
)


def cited_files(report: str) -> list[str]:
    """File paths a report names, in order, as written (absolute or relative)."""
    found = []
    for m in _PATH.finditer(report or ""):
        p = m.group(1)
        ext = p.rsplit(".", 1)[-1].lower()
        if ext in _EXTS and ("/" in p or len(p) > 6) and not p.startswith("http"):
            found.append(p)
    return list(dict.fromkeys(found))


def _relative(path: str, roots: list[str]) -> str:
    for root in roots:
        if root and path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")) + 1 :]
    return path


def findings(report: str, roots: list[str], budget: int = FINDINGS_CHARS) -> str:
    """The report as pointers: headings, and lines that name a file, a `symbol` or a
    conclusion, with paths made relative to the repo. Prose that narrates the search,
    and code blocks, are dropped."""
    text = report
    # Agents narrate before the report ("Let me look at…"); start at the first heading.
    first = re.search(r"^(#{1,4} |\*\*[^*\n]{3,80}\*\*\s*$)", text, re.M)
    if first and first.start() < len(text) // 2:
        text = text[first.start() :]
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for root in roots:
        if root:
            text = text.replace(root.rstrip("/") + "/", "")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= set("-|: =*"):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|") if c.strip()]
            line = "- " + " — ".join(cells)
        keep = (
            line.startswith("#")
            or "`" in line
            or _PATH.search(line)
            or re.match(r"^(\*\*)?(bottom line|summary|conclusion|answer|tl;?dr|key|recommend)", line, re.I)
        )
        if not keep:
            continue
        if len(line) > 260:
            line = line[:257].rstrip() + "…"
        lines.append(line)
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            out.append("- … (more in the full report)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _ago(seconds: float) -> str:
    days = seconds / 86400
    if days < 1:
        return f"{max(1, round(seconds / 3600))} h ago"
    return f"{round(days)} day{'s' if round(days) != 1 else ''} ago"


@dataclass
class Changes:
    known: bool  # the report's commit exists in this checkout
    commits: int = 0
    stat: list[str] = field(default_factory=list)  # one line per file, most-changed first, capped
    files: set[str] = field(default_factory=set)  # every changed path, repo-relative
    summary: str = ""  # the --stat summary line


def _git_all(folder: str, *commands: tuple[str, ...]) -> list[str | None]:
    """Several git commands at once: a hook pays for the slowest, not the sum."""
    procs = []
    for args in commands:
        try:
            procs.append(
                subprocess.Popen(
                    ["git", "--no-optional-locks", "-c", "core.quotePath=false", "-C", folder, *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            )
        except OSError:
            procs.append(None)
    out = []
    for p in procs:
        try:
            stdout, _ = p.communicate(timeout=GIT_TIMEOUT) if p else (None, None)
            out.append(stdout if p and p.returncode == 0 else None)
        except subprocess.TimeoutExpired:
            p.kill()
            out.append(None)
    return out


def changes_since(top: str, sha: str | None, max_files: int = DIFF_FILES, to: str | None = None) -> Changes:
    """What changed in this checkout since sha: commits on top of it and the diff from it to
    the working tree (so uncommitted edits count too), or to commit `to` when replaying."""
    if not sha:
        return Changes(False)
    diff = ("diff", "--numstat", "--no-renames", sha, *([to] if to else []))
    count, numstat = _git_all(top, ("rev-list", "--count", f"{sha}..{to or 'HEAD'}"), diff)
    if count is None or numstat is None:  # the commit is not in this checkout
        return Changes(False)
    rows = []
    for line in numstat.splitlines():
        added, deleted, path = [*line.split("\t", 2), "", "", ""][:3]
        n_add, n_del = int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0
        rows.append((n_add + n_del, path, n_add, n_del))
    files = {r[1] for r in rows}
    width = min(80, max((len(r[1]) for r in rows), default=0))
    stat = [f" {r[1][-width:]:<{width}} | +{r[2]} -{r[3]}" for r in sorted(rows, reverse=True)[:max_files]]
    adds, dels = sum(r[2] for r in rows), sum(r[3] for r in rows)
    summary = f"{len(rows)} files changed, +{adds} -{dels}" if rows else ""
    return Changes(True, int(count.strip() or 0), stat, files, summary)


INSTRUCTION = (
    "Treat it as a map, not as truth: verify any claim that touches the changed files, extend it "
    "rather than redo it, and say in your report what you confirmed and what is new."
)


def block(row, here: Checkout, now: float | None = None, to: str | None = None) -> str:
    """The block an exploration subagent gets: the earlier question, the report as pointers,
    its commit and age, what changed since, and how to use it."""
    now = now or time.time()
    roots = [here.top, row["root"], row["folder"]]
    when = time.strftime("%Y-%m-%d", time.localtime(row["created_at"]))
    sha = row["commit_sha"]
    estimated = " (estimated from the time)" if row["commit_source"] != "head" else ""
    head = (
        f"recall: an earlier exploration of this repo ({when}, {_ago(now - row['created_at'])}, "
        f"commit {sha[:9] if sha else 'unknown'}{estimated}"
        + (f", branch {row['branch']}" if row["branch"] else "")
        + ") answered a similar question:\n> "
        + " ".join(_BLOCK.sub(" ", row["prompt"]).split())[:300]
    )
    ch = changes_since(here.top, sha, to=to)
    cited = {_relative(p, roots) for p in cited_files(row["report"])}
    dirty_then = set(json.loads(row["dirty"] or "[]"))
    if ch.known:
        touched = sorted(p for p in cited if p in ch.files or any(f.endswith("/" + p) for f in ch.files))
        touched = [p for p in touched if not any(q != p and q.endswith("/" + p) for q in touched)]
        changed = ch.summary or "no file changes"
        target = to[:9] if to else "the working tree"
        lines = [f"Since then: {ch.commits} commits; {changed} (diff from {sha[:9]} to {target})."]
        lines += ch.stat
        if len(ch.files) > len(ch.stat):
            lines.append(f"  … {len(ch.files) - len(ch.stat)} more files")
        if touched:
            lines.append("Files the report cites that changed since: " + ", ".join(touched[:15]))
    else:
        lines = ["Its commit is not in this checkout, so recall cannot tell what changed: verify before relying on it."]
    if dirty_then & cited:
        lines.append("Uncommitted when it was written: " + ", ".join(sorted(dirty_then & cited)[:10]))
    tail = "\n".join(lines)
    budget = MAX_CHARS - len(head) - len(tail) - len(INSTRUCTION) - 120
    body = findings(row["report"], roots, min(FINDINGS_CHARS, max(800, budget)))
    text = f"{head}\n\nWhat it found (pointers):\n{body}\n\n{tail}\n\n{INSTRUCTION}"
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 40].rsplit("\n", 1)[0] + "\n…"
    return f"<recall-memory>\n{text}\n</recall-memory>"


def brief(folder: str | None, prompt: str, con=None, threshold: float = THRESHOLD) -> tuple[str | None, dict]:
    """The block for an exploration starting in folder with this prompt, or None when recall
    has nothing close enough. Also returns what it decided, for the usage log."""
    here = checkout(folder)
    if not here or not prompt.strip():
        return None, {"reason": "not a git repo" if not here else "no prompt"}
    con = con or connect()
    found = candidates(con, here.repo, prompt)
    pick = best(found, threshold)
    info = {"repo": here.repo, "candidates": len(found), "top": round(found[0].score, 3) if found else None}
    if not pick:
        return None, info | {"reason": "below threshold" if found else "no earlier exploration"}
    return block(pick.row, here), info | {"served": pick.row["id"], "score": round(pick.score, 3)}


# --- hook handlers ----------------------------------------------------------------------
#
# The hook dispatcher (integrations/hooks.py) calls these for its subagent_start and
# subagent_stop kinds. Where each agent puts what they need (October 2026):
#
#   claude  Start: PreToolUse on Agent|Task, whose tool_input holds prompt and subagent_type;
#           the block goes back appended to the prompt through updatedInput (tested live on
#           2.1.287; SubagentStart reaches the subagent too but carries no prompt). Stop:
#           SubagentStop with agent_type, last_assistant_message, agent_transcript_path.
#   codex   Start: SubagentStart with agent_type (the role; "default" unless the model chose
#           one) and, by our reading of the source, the child's rollout as transcript_path,
#           but no prompt: it is read from that rollout, and without it recall abstains.
#           Stop: SubagentStop after every child turn, with last_assistant_message and
#           agent_transcript_path. (Docs and source; not run live.)
#   cursor  Start: subagentStart drops additional_context, so preToolUse on the subagent tool
#           (Task, Subagent or mcp_task, by model), whose tool_input holds prompt and
#           subagent_type; updated_input.prompt carries the block, merged into the call with no
#           permission field. Stop: subagentStop with subagent_type, task, summary and
#           agent_transcript_path. (Docs and the 3.23 app code; not run live.)


def on_start(e):
    """subagent_start: the earlier report that answers this exploration, if any."""
    if e.observe or not is_explorer(e.subagent):
        return None
    prompt = e.prompt
    if not prompt and e.agent == "codex":
        prompt = first_prompt((e.raw or {}).get("transcript_path") or "")  # the child's rollout
    if not prompt:
        return None
    if e.session:
        con = connect()
        con.execute("delete from exploration_starts where at < ?", (time.time() - 86400,))
        con.execute("insert into exploration_starts values (?,?,?)", (e.session, prompt, time.time()))
        con.commit()
    block, info = brief(e.cwd, prompt)
    _log("explore_start", e, info)
    if not block:
        return None
    from .integrations.hooks import Reply

    return Reply(context=block)


def on_stop(e) -> None:
    """subagent_stop: store the exploration's report with the commit it was observed at."""
    if not is_explorer(e.subagent):
        return None
    raw = e.raw or {}
    path = raw.get("agent_transcript_path")
    report = (handback(path) if path else None) or e.report or (last_reply(path) if path else None)
    prompt = (e.prompt if e.agent == "cursor" else None) or (first_prompt(path) if path else None)
    con = connect()
    started = con.execute(
        "select rowid, prompt from exploration_starts where session = ? and at > ? order by at",
        (e.session, time.time() - 7200),
    ).fetchall()
    if prompt:  # this start is accounted for
        started = [r for r in started if _BLOCK.sub("\n", r[1]).strip() == _BLOCK.sub("\n", prompt).strip()][:1]
    elif len(started) == 1:  # no transcript: the one exploration this session started and has not ended
        prompt = started[0][1]
    else:
        started = []
    con.executemany("delete from exploration_starts where rowid = ?", [(r[0],) for r in started])
    con.commit()
    sid = raw.get("agent_id") or raw.get("subagent_id") or (os.path.basename(path).split(".")[0] if path else None)
    ok = bool(sid and prompt and report) and record(
        con, id=f"{e.agent}:{sid}", agent=e.agent, agent_type=e.subagent, folder=e.cwd, prompt=prompt, report=report
    )
    _log("explore_stop", e, {"recorded": ok, "chars": len(report or "")})
    return None


def _log(tool: str, e, info: dict) -> None:
    from . import usage

    usage.record("hook", tool, {"agent_type": e.subagent, "cwd": e.cwd}, None, client=e.agent, **info)


# --- reading a subagent's own transcript ----------------------------------------------


def _jsonl(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")
        ).strip()
    return ""


def _turns(path: str):
    """(role, text) for each message in a Claude Code, Codex or Cursor transcript."""
    for d in _jsonl(path):
        if d.get("type") == "response_item":  # Codex
            p = d.get("payload") or {}
            if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                yield p["role"], _text(p.get("content"))
            continue
        role = d.get("type") if d.get("type") in ("user", "assistant") else d.get("role")
        if role in ("user", "assistant"):
            yield role, _text((d.get("message") or {}).get("content"))


def first_prompt(path: str) -> str | None:
    """The task the subagent was given: its first user turn once harness context is removed."""
    from .chats.transcript import clean_user_text

    return next((c for role, t in _turns(path) if role == "user" and (c := clean_user_text(t))), None)


def last_reply(path: str) -> str | None:
    last = None
    for role, t in _turns(path):
        if role == "assistant" and t.strip():
            last = t
    return last


def handback(path: str) -> str | None:
    """Claude Code (from 2.1.271, per its docs) may deliver a subagent's report through a
    SubagentHandback tool call rather than its last message."""
    found = None
    for d in _jsonl(path):
        content = (d.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                found = (b.get("input") or {}).get("message") or found
    return found


# --- history --------------------------------------------------------------------------


@dataclass
class Past:
    id: str
    agent: str
    agent_type: str
    folder: str | None
    created_at: float | None  # when the report came
    prompt: str
    report: str
    started_at: float | None = None


def past_explorations(index) -> list[Past]:
    """Exploration subagents in the agents' own history: Cursor's (rows in the chat index,
    told apart by the subagentInfo Cursor records on each chat), Codex's explorer role, and
    Claude Code's Explore (its subagent transcripts, which the chat index does not hold)."""
    return [*_past_cursor(index), *_past_codex(index), *_past_claude()]


def _past_cursor(index) -> list[Past]:
    import glob

    from .chats.sources import cursor

    db = cursor._db()
    if db is None:
        return []
    rows = db.execute(
        """select substr(key, 14), json_extract(value, '$.subagentInfo.subagentTypeName'),
                  json_extract(value, '$.subagentInfo.parentComposerId'),
                  json_extract(value, '$.workspaceIdentifier.uri.fsPath'),
                  coalesce(nullif(json_extract(value, '$.lastUpdatedAt'), 0), json_extract(value, '$.createdAt')),
                  json_extract(value, '$.createdAt')
           from cursorDiskKV where key >= 'composerData:' and key < 'composerData;' and json_valid(value)
           and json_extract(value, '$.subagentInfo') is not null"""
    ).fetchall()
    rows = [r for r in rows if is_explorer(r[1])]
    folders = {}
    for parent in {r[2] for r in rows if r[2]}:
        got = db.execute(
            "select json_extract(value, '$.workspaceIdentifier.uri.fsPath') from cursorDiskKV where key = ?",
            (f"composerData:{parent}",),
        ).fetchone()
        folders[parent] = got[0] if got else None
    projects = dict(index.execute("select native_id, project from sessions where agent = 'cursor'"))
    transcripts = {
        os.path.basename(p)[:-6]: p
        for p in glob.glob(os.path.join(cursor.PROJECTS, "*", "agent-transcripts", "*", "subagents", "*.jsonl"))
    }
    out = []
    for cid, kind, parent, ws, ts, start in rows:
        folder = ws or folders.get(parent) or projects.get(cid) or projects.get(parent)
        if cid in transcripts:
            prompt, report = first_prompt(transcripts[cid]), last_reply(transcripts[cid])
        else:
            prompt, report = _from_index(index, f"cursor:{cid}")
        if prompt and report:
            seconds = (ts or 0) / 1000 or None
            out.append(
                Past(f"cursor:{cid}", "cursor", kind, folder, seconds, prompt, report, (start or 0) / 1000 or None)
            )
    return out


def _from_index(index, sid: str) -> tuple[str | None, str | None]:
    rows = index.execute("select role, text from messages where session_id = ? order by seq", (sid,)).fetchall()
    prompt = next((r[1] for r in rows if r[0] == "user"), None)
    report = next((r[1] for r in reversed(rows) if r[0] == "assistant"), None)
    return prompt, report


def _past_codex(index) -> list[Past]:
    out = []
    for sid, source, folder, ts, start in index.execute(
        "select id, source, project, updated_at, started_at from sessions where agent = 'codex' and origin = 'subagent'"
    ):
        try:
            with open(source) as f:
                meta = (json.loads(f.readline()).get("payload")) or {}
        except (OSError, json.JSONDecodeError):
            continue
        role = meta.get("agent_role") or ""
        if is_explorer(role) and (prompt := first_prompt(source)) and (report := last_reply(source)):
            out.append(Past(sid, "codex", role, folder or meta.get("cwd"), ts, prompt, report, start))
    return out


def _past_claude() -> list[Past]:
    import glob

    out = []
    root = os.path.expanduser("~/.claude/projects")
    for meta_path in glob.glob(os.path.join(root, "*", "*", "subagents", "agent-*.meta.json")):
        try:
            with open(meta_path) as f:
                kind = json.load(f).get("agentType") or ""
        except (OSError, json.JSONDecodeError):
            continue
        path = meta_path[: -len(".meta.json")] + ".jsonl"
        if not is_explorer(kind) or not os.path.exists(path):
            continue
        folder = ts = start = None
        for d in _jsonl(path):
            folder = folder or d.get("cwd")
            if d.get("timestamp"):
                ts, start = d["timestamp"], start or d["timestamp"]
        prompt, report = first_prompt(path), last_reply(path)
        if prompt and report:
            from .chats.sources import iso_ts

            name = os.path.basename(path)[: -len(".jsonl")]
            out.append(Past(f"claude:{name}", "claude", kind, folder, iso_ts(ts), prompt, report, iso_ts(start)))
    return out


def _mainline(top: str) -> list[tuple[float, str]]:
    """(commit time, sha) along the default branch's first-parent line, oldest first."""
    ref = (_git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    for name in (ref, "main", "master", "HEAD"):
        if name and (out := _git(top, "log", "--first-parent", "--format=%ct %H", name, timeout=30)):
            return sorted((float(t), h) for t, h in (line.split() for line in out.splitlines()))
    return []


def backfill(con, index, log=None) -> dict:
    """Store past explorations, each at the default branch's commit at the report's time
    (estimated: the history does not say which commit the agent saw)."""
    import bisect

    from .chats.projects import resolve

    have = {r[0] for r in con.execute("select id from explorations")}
    found = [p for p in past_explorations(index) if p.id not in have]
    repos: dict[str, Checkout | None] = {}
    by_name: dict[str, Checkout] = {}

    def repo_for(folder: str | None) -> Checkout | None:
        if not folder:
            return None
        if folder not in repos:
            project = resolve(folder)
            co = None
            if project and project.root and os.path.isdir(project.root):
                co = Checkout(project.root, project.key, None, None)
                by_name.setdefault(os.path.basename(project.root), co)
            repos[folder] = co
        return repos[folder]

    for p in found:
        repo_for(p.folder)
    lines: dict[str, list] = {}
    stored = skipped = 0
    for p in sorted(found, key=lambda p: p.created_at or 0):
        co = repo_for(p.folder)
        if co is None and p.folder:
            # A deleted worktree, e.g. ~/.cursor/worktrees/<repo>/<id>: the repo of that name.
            m = re.search(r"/worktrees/([^/]+)/", p.folder + "/")
            co = by_name.get(m.group(1)) if m else None
        if co is None or not p.created_at:
            skipped += 1
            continue
        if co.top not in lines:
            lines[co.top] = _mainline(co.top)
        line = lines[co.top]
        i = bisect.bisect_right([t for t, _ in line], p.created_at) - 1
        sha = line[i][1] if i >= 0 else None
        ok = sha is not None and record(
            con,
            id=p.id,
            agent=p.agent,
            agent_type=p.agent_type,
            folder=p.folder,
            prompt=p.prompt,
            report=p.report,
            created_at=p.created_at,
            started_at=p.started_at,
            origin="history",
            commit=sha,
            commit_source="estimated",
            dirty=[],
            repo=co,
        )
        stored += ok
        skipped += not ok
    if log:
        log(f"{len(found)} past explorations found, {stored} stored, {skipped} skipped")
    return {"found": len(found), "stored": stored, "skipped": skipped, "already": len(have)}
