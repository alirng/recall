"""What your agents are doing now: recent sessions across Claude Code, Codex and Cursor, the
branch and files each one is on, and the files two sessions in the same repo both edited.

Reads each agent's history straight from disk (read-only) instead of the index, so it is
current to the second. Each transcript's summary is cached by its size and mtime, so a
refresh only re-reads the chats that moved. Edits are taken from the agents' own tool calls,
so a file changed through a shell command does not show up.
"""

import contextlib
import glob
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from itertools import combinations

from . import paths
from .chats.sources import claude, codex, cursor, read_jsonl

LIVE_SECONDS = 15 * 60  # a session touched this recently counts as live
CACHE = os.path.join(paths.HOME, "cache", "status.json")
WARNED = os.path.join(paths.HOME, "cache", "edit-warnings.json")
# Every agent's edit hook adds a line here, so agents whose history recall cannot read yet
# still show up, and an edit counts before the agent's own transcript is written.
EDITS = os.path.join(paths.HOME, "cache", "edits.jsonl")
AGENTS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "opencode": "OpenCode",
}

# Tool calls that change a file, and the input field naming it, per agent.
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "StrReplace", "Delete", "EditNotebook"}
_PATH_KEYS = ("file_path", "notebook_path", "path", "target_file")
# Codex edits through apply_patch; the patch names each file it touches.
_PATCH_FILE = re.compile(
    r"\*\*\* (?:Add|Update|Delete) File: (.+?)(?=\\n|\n|\"|$)|\*\*\* Move to: (.+?)(?=\\n|\n|\"|$)"
)


@dataclass
class Live:
    id: str
    agent: str
    title: str
    folder: str | None
    branch: str | None
    updated_at: float
    ask: str = ""  # the person's latest request in the chat
    edits: list[str] = field(default_factory=list)  # absolute paths, least recently edited first
    checkout: str | None = None  # the worktree it last edited in, else the one it runs in
    touched: list[tuple[str, str]] = field(default_factory=list)  # (checkout, repo-relative path)


@dataclass
class Checkout:
    top: str
    repo: str  # git common dir: every worktree of one repo shares it
    branch: str | None
    base: str | None  # where it forked from the default branch
    open: set[str]  # uncommitted, or changed on this branch and not yet in the default branch
    dirty: int
    snapshot: str | None = None  # a commit of the checkout as it stands, uncommitted edits included


# --- reading the agents' history ------------------------------------------------


def _tool_edits(path: str) -> dict[str, None]:
    """Files named by edit tool calls in a Claude Code or Cursor transcript, in the order
    they were last edited."""
    out: dict[str, None] = {}
    for d in read_jsonl(path):
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _EDIT_TOOLS:
                args = b.get("input") or {}
                target = next((args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)), None)
                if target:
                    out.pop(target, None)
                    out[target] = None
    return out


def _codex_edits(path: str, cwd: str | None) -> dict[str, None]:
    out: dict[str, None] = {}
    for d in read_jsonl(path):
        payload = d.get("payload") or {}
        if d.get("type") != "response_item" or payload.get("type") not in ("custom_tool_call", "function_call"):
            continue
        text = str(payload.get("input") or payload.get("arguments") or "")
        for m in _PATCH_FILE.finditer(text):
            target = (m.group(1) or m.group(2)).strip()
            target = target if os.path.isabs(target) or not cwd else os.path.join(cwd, target)
            out.pop(target, None)
            out[target] = None
    return out


def _line(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _read(agent: str, path: str, catalog) -> dict | None:
    """One chat, boiled down to what status needs. None for headless and empty chats."""
    if agent == "claude":
        s = claude.load(path)
    elif agent == "codex":
        s = codex.load(path, catalog())
    else:
        s = cursor.load_transcript(path)
    if not s or s.origin != "interactive" or not s.messages:
        return None
    # What the person typed; `!` shell commands and other tool-tagged turns are not requests.
    asks = [m.text for m in s.messages if m.role == "user" and m.text.strip() and not m.text.lstrip().startswith("<")]
    edits = _codex_edits(path, s.project) if agent == "codex" else _tool_edits(path)
    return {
        "id": s.id,
        "agent": s.agent,
        "title": _line(s.title or (asks[0] if asks else "(untitled)"), 80),
        "folder": s.project,
        "branch": s.branch,
        "updated_at": s.updated_at or 0,
        "ask": _line(asks[-1], 200) if asks else "",
        "edits": list(edits),
    }


def _sessions(since: float) -> list[Live]:
    try:
        with open(CACHE) as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        cache = {}
    catalog = None

    def codex_catalog():
        nonlocal catalog
        catalog = catalog if catalog is not None else codex._catalog()
        return catalog

    found, fresh, changed = [], {}, False
    patterns = {
        "claude": os.path.join(claude.ROOT, "*", "*.jsonl"),
        "codex": os.path.join(codex.ROOT, "sessions", "**", "*.jsonl"),
        "cursor": os.path.join(cursor.PROJECTS, "*", "agent-transcripts", "*", "*.jsonl"),
    }
    for agent, pattern in patterns.items():
        for path in glob.glob(pattern, recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            fp = f"{st.st_mtime_ns}:{st.st_size}"
            hit = cache.get(path)
            if hit and hit["fp"] == fp:
                rec = hit["rec"]
            else:
                rec, changed = _read(agent, path, codex_catalog), True
            fresh[path] = {"fp": fp, "rec": rec}
            if rec:
                live = Live(**{k: v for k, v in rec.items() if k != "edits"})
                live.updated_at = max(live.updated_at, st.st_mtime)
                live.edits = rec["edits"]
                found.append(live)
    if changed or len(fresh) != len(cache):
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            with open(CACHE + ".tmp", "w") as f:
                json.dump(fresh, f)
            os.replace(CACHE + ".tmp", CACHE)

    by_id = {s.id: s for s in found}
    for sid, e in _logged(since).items():
        s = by_id.get(sid)
        if not s:
            s = Live(sid, e["agent"], f"{AGENTS.get(e['agent'], e['agent'])} chat", e["cwd"], None, e["at"])
            found.append(s)
        s.updated_at = max(s.updated_at, e["at"])
        s.edits += [f for f in e["files"] if f not in s.edits]
    return found


def _log(agent: str, session: str, path: str, cwd: str | None) -> None:
    line = json.dumps({"at": time.time(), "agent": agent, "session": session, "file": path, "cwd": cwd})
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(EDITS), exist_ok=True)
        if os.path.exists(EDITS) and os.path.getsize(EDITS) > 512_000:  # keep two days
            keep = [x for x in _log_lines() if x["at"] > time.time() - 2 * 86400]
            with open(EDITS, "w") as f:
                f.writelines(json.dumps(x) + "\n" for x in keep)
        with open(EDITS, "a") as f:
            f.write(line + "\n")


def _log_lines() -> list[dict]:
    out = []
    with contextlib.suppress(OSError), open(EDITS) as f:
        for line in f:
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def _logged(since: float) -> dict[str, dict]:
    """Chats seen by the edit hooks since `since`: agent, folder, files in edit order."""
    out: dict[str, dict] = {}
    for x in _log_lines():
        if x.get("at", 0) < since:
            continue
        e = out.setdefault(f"{x['agent']}:{x['session']}", {"agent": x["agent"], "cwd": x.get("cwd"), "files": []})
        e["at"] = x["at"]
        if x["file"] in e["files"]:
            e["files"].remove(x["file"])
        e["files"].append(x["file"])
    return out


# --- git --------------------------------------------------------------------------


def _git(folder: str, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


def _top(path: str, cache: dict[str, str | None]) -> str | None:
    """The git checkout a path sits in, found by walking up to a .git entry."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    seen = []
    while d not in cache:
        seen.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            cache[d] = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            cache[d] = None
            break
        d = parent
    for s in seen:
        cache[s] = cache[d]
    return cache[d]


def _repo_dir(top: str) -> str | None:
    """A checkout's git common dir, read from its .git entry without running git."""
    g = os.path.join(top, ".git")
    if os.path.isdir(g):
        return os.path.realpath(g)
    try:
        with open(g) as f:
            gitdir = os.path.join(top, f.read().split(":", 1)[1].strip())
    except (OSError, IndexError):
        return None
    try:
        with open(os.path.join(gitdir, "commondir")) as f:
            return os.path.realpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        return os.path.realpath(gitdir)  # a submodule's git dir is its own


def _checkout(top: str) -> Checkout | None:
    info = _git(top, "rev-parse", "--git-common-dir", "--abbrev-ref", "HEAD")
    if not info or len(info) < 2:
        return None
    porcelain = _git(top, "status", "--porcelain", "--untracked-files=all") or []
    open_ = {line[3:].split(" -> ")[-1].strip('"') for line in porcelain}
    default = (_git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ["main"])[0]
    base = (_git(top, "merge-base", default, "HEAD") or [None])[0]
    if base:
        open_.update(_git(top, "diff", "--name-only", base, "HEAD") or [])
    # A squash-merged or rebased branch keeps its own commits, so only what still differs
    # from the default branch's tip is open. New untracked files always are.
    differs = _git(top, "diff", "--name-only", default)
    if differs is not None:
        untracked = {line[3:] for line in porcelain if line.startswith("??")}
        open_ = (open_ & set(differs)) | untracked
    branch = info[1] if info[1] != "HEAD" else None
    return Checkout(top, os.path.realpath(os.path.join(top, info[0])), branch, base, open_, len(porcelain))


def _conflicts(a: Checkout, b: Checkout) -> set[str]:
    """Files that would conflict if two checkouts were merged as they stand now. `git stash
    create` only writes a commit object: the working tree, index and stash list are untouched."""
    for c in (a, b):
        if c.snapshot is None:
            c.snapshot = ((_git(c.top, "stash", "create") or []) + (_git(c.top, "rev-parse", "HEAD") or [""]))[0]
    try:
        r = subprocess.run(
            ["git", "-C", a.top, "merge-tree", "--write-tree", "--name-only", "--no-messages", a.snapshot, b.snapshot],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(r.stdout.splitlines()[1:]) if r.returncode == 1 else set()


def _place(sessions: list[Live]) -> dict[str, Checkout]:
    """Resolve every edit to its own checkout (a session can edit in a worktree it never
    started in) and place each session where it last edited."""
    tops: dict[str, str | None] = {}
    checkouts: dict[str, Checkout | None] = {}
    for s in sessions:
        last = None
        for f in s.edits:
            full = os.path.realpath(f if os.path.isabs(f) or not s.folder else os.path.join(s.folder, f))
            top = _top(full, tops)
            if not top:
                continue
            if top not in checkouts:
                checkouts[top] = _checkout(top)
            if checkouts[top]:
                s.touched.append((top, os.path.relpath(full, top)))
                last = top
        home = _top(os.path.realpath(s.folder), tops) if s.folder and os.path.isdir(s.folder) else None
        if home and home not in checkouts:
            checkouts[home] = _checkout(home)
        s.checkout = last or (home if home and checkouts[home] else None)
        if s.checkout:
            s.branch = checkouts[s.checkout].branch or s.branch
    return {k: v for k, v in checkouts.items() if v}


def _repo_name(repo: str) -> str:
    """The folder the repo lives in: ~/code/harbor/.git -> harbor."""
    return os.path.basename(os.path.dirname(repo)) if os.path.basename(repo) == ".git" else os.path.basename(repo)


def _ago(seconds: float) -> str:
    s = int(seconds)
    return "just now" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


# --- rules ------------------------------------------------------------------------

# When an agent is about to edit a file another chat has open changes to, what happens
# depends on the situation (worst first) and the person's rule for it, set in config.toml
# [coordination]. `recall setup` offers the presets below.
SITUATIONS = {
    "same_copy": "you share a working copy, so either of you can overwrite the other",
    "conflict": "merging the two as they stand would conflict",
    "overlap": "the two merge cleanly for now",
}
ACTIONS = ("worktree", "ask", "tell", "off")  # strongest first
PRESETS = {
    "recommended": (
        {"same_copy": "ask", "conflict": "tell", "overlap": "off"},
        "stop and ask you before two live chats edit one working copy; mention conflicts",
    ),
    "hands-off": (
        {"same_copy": "worktree", "conflict": "tell", "overlap": "off"},
        "move the agent to its own worktree instead of asking",
    ),
    "tell": (
        {"same_copy": "tell", "conflict": "tell", "overlap": "tell"},
        "agents mention every overlap and never stop",
    ),
    "off": ({"same_copy": "off", "conflict": "off", "overlap": "off"}, "no checks on edits"),
}


def rules() -> dict[str, str]:
    from . import config

    got = config.load()["coordination"]
    return {k: got[k] if got.get(k) in ACTIONS else PRESETS["recommended"][0][k] for k in SITUATIONS}


def _about(other: Live, c: Checkout, situation: str, files: list[str]) -> str:
    """One line on another chat's open changes to files this agent is touching."""
    where = "this same working copy" if situation == "same_copy" else f"branch {c.branch or 'detached'}"
    who = AGENTS.get(other.agent, other.agent)
    line = (
        f'"{other.title}" ({who}, {where}, active {_ago(time.time() - other.updated_at)}) also has open '
        f"changes to {', '.join(files)}: {SITUATIONS[situation]}."
    )
    return line + (f' It was last asked: "{_line(other.ask, 140)}"' if other.ask else "")


def _instruction(action: str, file: str, repo: str, default: str) -> str:
    if action == "worktree":
        return (
            f"The user's rule here is to move to a worktree: make this change in a new git worktree on its own "
            f"branch off {default} (e.g. `git worktree add ../{repo}-<topic> -b <topic> {default}`), and tell the "
            "user you moved."
        )
    if action == "ask":
        return f"The user's rule here is to ask first: stop, tell the user, and edit {file} only once they say so."
    return "Mention this to the user in your reply, then carry on."


def status(since: float) -> dict:
    now = time.time()
    sessions = sorted(_sessions(since), key=lambda s: -s.updated_at)
    checkouts = _place(sessions)

    # A file that two sessions in one repo both edited and that is still open where each
    # edited it; work already in the default branch drops out. Its level:
    #   act    the checkouts would conflict on it if merged now, or two live sessions are
    #          editing it in the same working tree
    #   watch  anything else: it merges clean, or one of the sessions has gone quiet
    touched: dict[tuple[str, str], dict[str, Live]] = {}
    for s in sessions:
        for top, f in s.touched:
            c = checkouts[top]
            if f in c.open:
                touched.setdefault((c.repo, f), {})[s.id] = s
    pairs: dict[tuple[str, str], set[str]] = {}
    overlaps = []
    for (repo, f), group in sorted(touched.items(), key=lambda kv: kv[0][1]):
        if len(group) < 2:
            continue
        side = {sid: next(t for t, g in s.touched if g == f and checkouts[t].repo == repo) for sid, s in group.items()}
        tops = sorted(set(side.values()))
        live = sum(now - s.updated_at <= LIVE_SECONDS for s in group.values()) >= 2
        conflict = None
        if len(tops) > 1:
            for a, b in combinations(tops, 2):
                if (a, b) not in pairs:
                    pairs[a, b] = _conflicts(checkouts[a], checkouts[b])
            conflict = any(f in pairs[a, b] for a, b in combinations(tops, 2))
        level = "act" if conflict or (conflict is None and live) else "watch"
        situation = "conflict" if conflict else "same_copy" if conflict is None and live else "overlap"
        overlaps.append(
            {
                "repo": _repo_name(repo),
                "file": f,
                "sessions": list(group),
                "same_checkout": len(tops) == 1,
                "branches": [checkouts[t].branch or "detached" for t in tops],
                "live": live,
                "conflict": conflict,
                "level": level,
                "sides": [
                    {
                        "session": sid,
                        "checkout": side[sid],
                        "branch": checkouts[side[sid]].branch,
                        "base": checkouts[side[sid]].base,
                    }
                    for sid in group
                ],
                # For each session, a note to paste into it about the others.
                "notes": {
                    sid: "\n".join(
                        _about(o, checkouts[side[oid]], situation, [f]) for oid, o in group.items() if oid != sid
                    )
                    + "\nTell the user before changing it further, and agree which change goes in first."
                    for sid in group
                },
            }
        )
    overlaps.sort(key=lambda o: o["level"] != "act")

    out = []
    for s in sessions:
        c = checkouts.get(s.checkout) if s.checkout else None
        out.append(
            {
                "id": s.id,
                "agent": s.agent,
                "title": s.title,
                "folder": s.folder,
                "repo": _repo_name(c.repo) if c else (os.path.basename(s.folder) if s.folder else None),
                "checkout": s.checkout,
                "branch": s.branch,
                "updated_at": s.updated_at,
                "age_seconds": int(now - s.updated_at),
                "live": now - s.updated_at <= LIVE_SECONDS,
                "files": sorted({f for _, f in s.touched}),
            }
        )
    levels = {o["level"] for o in overlaps}
    return {
        "generated_at": now,
        "since": since,
        "live_seconds": LIVE_SECONDS,
        "level": "act" if "act" in levels else "watch" if levels else "none",
        "sessions": out,
        "overlaps": overlaps,
    }


def elsewhere(path: str, since: float) -> str:
    """For agents without an edit hook: every chat with open changes in the repo at `path`,
    the files, and which of them would conflict with the checkout `path` is in."""
    top = _top(os.path.realpath(path), {})
    if not top:
        return f"{path} is not in a git repo."
    sessions = _sessions(since)
    checkouts = _place(sessions)
    here = checkouts.get(top) or _checkout(top)
    if not here:
        return f"{path} is not in a git repo."
    lines = []
    for s in sessions:
        mine = [(t, f) for t, f in s.touched if checkouts[t].repo == here.repo and f in checkouts[t].open]
        if not mine:
            continue
        clash: set[str] = set()
        for t in {t for t, _ in mine} - {top}:
            clash |= _conflicts(here, checkouts[t])
        where = "this working copy" if s.checkout == top else f"branch {s.branch or 'detached'}"
        files = ", ".join(sorted({f + (" (would conflict)" if f in clash else "") for _, f in mine}))
        active = _ago(time.time() - s.updated_at)
        lines.append(f'- "{s.title}" ({AGENTS.get(s.agent, s.agent)}, {where}, active {active}): {files}')
    if not lines:
        return "No other chat has open changes in this repo."
    return (
        "Chats with open changes in this repo (yours may be among them). Before editing a file listed here, "
        "tell the user which chat has it open.\n" + "\n".join(lines)
    )


# --- the edit hook ----------------------------------------------------------------


@dataclass
class Decision:
    action: str  # worktree | ask | tell
    message: str  # for the agent
    notice: str  # one line for the person


def check_edit(
    agent: str, session_id: str, path: str, cwd: str | None, since: float, acts=("worktree", "ask", "tell")
) -> Decision | None:
    """Called when an agent is about to edit `path`. Logs the edit, then checks other chats
    with open changes to the same file in the same repo and applies the person's rule for the
    worst situation found, if it is one of `acts` (an agent hook may only be able to refuse
    before an edit and to inform after it). Each other chat is raised once, listing every
    file the two share, and again only if things get worse."""
    path = os.path.realpath(path if os.path.isabs(path) or not cwd else os.path.join(cwd, path))
    _log(agent, session_id, path, cwd)
    tops: dict[str, str | None] = {}
    top = _top(path, tops)
    repo = _repo_dir(top) if top else None
    want = rules()
    if not repo or not any(a in acts for a in want.values()):
        return None
    rel = os.path.relpath(path, top)
    me = f"{agent}:{session_id}"
    repos: dict[str, str | None] = {}

    def in_repo(s: Live) -> dict[str, str]:
        """The chat's edits in this repo: repo-relative path -> its checkout."""
        out = {}
        for f in s.edits:
            full = os.path.realpath(f if os.path.isabs(f) or not s.folder else os.path.join(s.folder, f))
            t = _top(full, tops)
            if t and repos.setdefault(t, _repo_dir(t)) == repo:
                out[os.path.relpath(full, t)] = t
        return out

    sessions = _sessions(since)
    others = [(s, theirs) for s in sessions if s.id != me and rel in (theirs := in_repo(s))]
    if not others:
        return None
    mine = next((s for s in sessions if s.id == me), None)
    my_files = (set(in_repo(mine)) if mine else set()) | {rel}
    checkouts: dict[str, Checkout | None] = {top: _checkout(top)}
    if not checkouts[top]:
        return None
    rank = {name: i for i, name in enumerate(reversed(SITUATIONS))}  # overlap 0 … same_copy 2
    found = []
    for s, theirs in others:
        t = theirs[rel]
        if t not in checkouts:
            checkouts[t] = _checkout(t)
        c = checkouts[t]
        if not c or rel not in c.open:
            continue
        if t == top and time.time() - s.updated_at <= LIVE_SECONDS:
            situation = "same_copy"
        elif t != top and rel in _conflicts(checkouts[top], c):
            situation = "conflict"
        else:
            situation = "overlap"
        if want[situation] in acts:
            shared = sorted(f for f, ft in theirs.items() if f in my_files and ft == t and f in c.open)
            found.append((situation, s, c, shared))
    if not found:
        return None

    try:
        with open(WARNED) as f:
            warned = json.load(f)
    except (OSError, json.JSONDecodeError):
        warned = {}
    seen = warned.get(me, {}).get("chats", {})
    fresh = [x for x in found if rank[x[0]] > seen.get(x[1].id, -1)]
    if not fresh:
        return None
    now = time.time()
    warned = {k: v for k, v in warned.items() if now - v.get("at", 0) < 2 * 86400}
    warned[me] = {"at": now, "chats": {**seen, **{s.id: rank[sit] for sit, s, _, _ in fresh}}}
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(WARNED), exist_ok=True)
        with open(WARNED, "w") as f:
            json.dump(warned, f)

    action = min((want[sit] for sit, *_ in fresh), key=ACTIONS.index)
    default = (_git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ["main"])[0]
    lines = ["recall: " + _about(s, c, sit, shared) for sit, s, c, shared in fresh]
    lines.append(_instruction(action, rel, _repo_name(repo), default))
    worst, other = max(((sit, s) for sit, s, _, _ in fresh), key=lambda x: rank[x[0]])
    words = {"same_copy": " in this working copy", "conflict": " and would conflict", "overlap": ""}[worst]
    notice = f"recall: {os.path.basename(path)} is also open in “{other.title}”{words}"
    return Decision(action, "\n".join(lines), notice)
