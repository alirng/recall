"""What your agents are doing now: recent sessions across Claude Code, Codex and Cursor, the
branch and files each one is on, and the files two sessions in the same repo both edited.

Reads each agent's history straight from disk (read-only) instead of the index, so it is
current to the second. Each transcript's summary is cached by its size and mtime, so a
refresh only re-reads the chats that moved. Edits are taken from the agents' own tool calls,
so a file changed through a shell command does not show up.

The edit hook runs before every edit, so it looks twice. First, cheaply (_others_may_edit):
the edit log and, for each transcript that changed lately, the files its edit tool calls name,
read from where the last look stopped. Only when one of those names the file being edited does
it read those chats in full and ask git.
"""

import contextlib
import difflib
import functools
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from itertools import combinations

from . import paths

LIVE_SECONDS = 15 * 60  # a session touched this recently counts as live
CACHE = os.path.join(paths.HOME, "cache", "status.json")
CACHE_VERSION = 2  # bump when _read's record changes, so old entries are read again
WARNED = os.path.join(paths.HOME, "cache", "edit-warnings.json")
# Every agent's edit hook adds a line here, so agents whose history recall cannot read yet
# still show up, and an edit counts before the agent's own transcript is written.
EDITS = os.path.join(paths.HOME, "cache", "edits.jsonl")
# The edit hook's first look: per recent transcript, how far it has been read and the files its
# edit tool calls named. A chat that starts is listed at the next edit, from the folder its agent
# writes it to; every RELIST_SECONDS each agent's whole history is globbed again, so a chat without
# recall's hooks (Codex before they are trusted) resumed in a file older than LIST_HORIZON is seen
# by other chats' edit hooks within that time.
EDIT_INDEX = os.path.join(paths.HOME, "cache", "edit-index.json")
EDIT_INDEX_VERSION = 2  # 2: files placed as _resolve places them, with the folders new chats appear in
RELIST_SECONDS = 300
LIST_HORIZON = 86400  # a relist keeps transcripts changed this recently, or within the hook's window
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
# Codex and OpenCode edit through apply_patch; the patch names each file it touches. In a
# transcript the patch sits inside a JSON or JS string, so a line can also end at "\n".
_PATCH_FILE = re.compile(
    r"\*\*\* (?:Add|Update|Delete) File: (.+?)(?=\\n|\n|\"|$)|\*\*\* Move to: (.+?)(?=\\n|\n|\"|$)", re.M
)
# What a transcript line must hold to name an edit (_named), found in the raw bytes so that the
# edit hook's first look decodes only those lines.
_EDIT_LINE = {
    "codex": re.compile(rb"\*\*\* (?:Add|Update|Delete) File: |\*\*\* Move to: "),
    "other": re.compile(rb'"name": ?"(?:' + "|".join(sorted(_EDIT_TOOLS)).encode() + rb')"'),
}


def patch_hunks(text: str) -> tuple[list[str], list[str]]:
    """The text each hunk of a one-file apply_patch replaces and puts in its place, from its
    context (' ') and removed ('-') or added ('+') lines, so edit_lines can place it."""
    olds, news, old, new = [], [], [], []

    def close():
        if old or new:
            olds.append("".join(old))
            news.append("".join(new))
        old.clear()
        new.clear()

    for line in text.splitlines(keepends=True):
        if line.startswith(("@@", "*** ")):
            close()
        elif line[:1] in (" ", "-"):
            old.append(line[1:])
            new.extend([line[1:]] if line[0] == " " else [])
        elif line[:1] == "+":
            new.append(line[1:])
    close()
    return [o for o in olds if o.strip()], [n for o, n in zip(olds, news, strict=True) if o.strip()]


def patch_files(text: str, cwd: str | None) -> list[str]:
    """The files an apply_patch patch touches, made absolute against cwd."""
    out = []
    for m in _PATCH_FILE.finditer(text):
        f = (m.group(1) or m.group(2)).strip()
        out.append(f if os.path.isabs(f) or not cwd else os.path.join(cwd, f))
    return list(dict.fromkeys(out))


@dataclass
class Live:
    id: str
    agent: str
    title: str
    folder: str | None
    branch: str | None
    updated_at: float
    ask: str = ""  # the person's latest request in the chat
    source: str | None = None  # its transcript, which an edit hook also reports
    # path -> when it was last edited (None if the agent records no times), oldest first
    edits: dict[str, float | None] = field(default_factory=dict)
    checkout: str | None = None  # the worktree it last edited in, else the one it runs in
    # (checkout, repo-relative path, when), one per file, oldest edit first
    touched: list[tuple[str, str, float | None]] = field(default_factory=list)
    # path -> the line ranges its edits left, as the edit hooks saw them
    regions: dict[str, list[list[int]]] = field(default_factory=dict)


@dataclass
class Checkout:
    top: str
    repo: str  # git common dir: every worktree of one repo shares it
    branch: str | None
    base: str | None  # where it forked from the default branch
    open: set[str]  # uncommitted, or changed on this branch and not yet in the default branch
    dirty: int
    untracked: set[str] = field(default_factory=set)


# --- reading the agents' history ------------------------------------------------


def _edited(out: dict, path: str, ts: float | None) -> None:
    out.pop(path, None)
    out[path] = ts


def _named(agent: str, d: dict, cwd: str | None) -> list[str]:
    """The files one transcript line's edit tool calls name: a Codex apply_patch, or a Claude
    Code or Cursor edit tool."""
    if agent == "codex":
        payload = d.get("payload") or {}
        if d.get("type") != "response_item" or payload.get("type") not in ("custom_tool_call", "function_call"):
            return []
        return patch_files(str(payload.get("input") or payload.get("arguments") or ""), cwd)
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _EDIT_TOOLS:
            args = b.get("input") or {}
            target = next((args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)), None)
            if target:
                out.append(target)
    return out


def _tool_edits(path: str) -> dict[str, float | None]:
    """Files named by edit tool calls in a Claude Code or Cursor transcript, with when each
    was last edited, oldest first."""
    from .chats.sources import iso_ts, read_jsonl

    out: dict[str, float | None] = {}
    for d in read_jsonl(path):
        for target in _named("claude", d, None):
            _edited(out, target, iso_ts(d.get("timestamp")))
    return out


def _codex_edits(path: str, cwd: str | None) -> dict[str, float | None]:
    from .chats.sources import iso_ts, read_jsonl

    out: dict[str, float | None] = {}
    for d in read_jsonl(path):
        for target in _named("codex", d, cwd):
            _edited(out, target, iso_ts(d.get("timestamp")))
    return out


def _line(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _read(agent: str, path: str, catalog) -> dict | None:
    """One chat, boiled down to what status needs. None for headless and empty chats."""
    from .chats.sources import claude, codex, cursor

    if agent == "claude":
        s = claude.load(path)
    elif agent == "codex":
        s = codex.load(path, catalog())
    else:
        s = cursor.load_transcript(path, errors=False)
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
        "source": os.path.realpath(path),
        "edits": list(edits.items()),
    }


def _transcripts() -> dict[str, str]:
    """Every agent transcript on disk, path -> agent: a glob over all of each agent's history."""
    from .chats.sources import claude, codex, cursor

    patterns = {
        "claude": os.path.join(claude.ROOT, "*", "*.jsonl"),
        "codex": os.path.join(codex.ROOT, "sessions", "**", "*.jsonl"),
        "cursor": os.path.join(cursor.PROJECTS, "*", "agent-transcripts", "*", "*.jsonl"),
    }
    return {path: agent for agent, pattern in patterns.items() for path in glob.glob(pattern, recursive=True)}


def _transcript_agent(path: str, roots: dict[str, str] | None = None) -> str | None:
    """The agent whose transcript `path` is, when it is one _transcripts() would find. `roots`, as
    _roots() gives them (the edit index keeps a copy), saves loading the parsers."""
    roots = roots or _roots()
    up = [path]
    for _ in range(4):
        up.append(os.path.dirname(up[-1]))
    if not path.endswith(".jsonl"):
        return None
    if up[2] == roots["claude"]:
        return "claude"
    if path.startswith(roots["codex"] + os.sep):
        return "codex"
    if up[4] == roots["cursor"] and os.path.basename(up[2]) == "agent-transcripts":
        return "cursor"
    return None


def _write_json(path: str, data) -> None:
    """Replace a cache file whole. Each writer has its own temporary file, since hooks of several
    chats (and an agent's parallel tool calls) write at once."""
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def _cached_since(path: str, entry, since: float) -> bool:
    """Whether a status cache entry is still worth keeping: its file is there and moved within the window."""
    try:
        return int(str(entry["fp"]).split(":")[0]) / 1e9 >= since and os.path.exists(path)
    except (KeyError, TypeError, ValueError):
        return False


def _sessions(since: float, only: dict[str, str] | None = None) -> list[Live]:
    """The chats active since `since`, from every agent's transcripts and the edit log. `only`
    reads just these transcripts (path -> agent) instead of globbing every agent's history."""
    try:
        with open(CACHE) as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    cache = stored.get("chats") if stored.get("version") == CACHE_VERSION else None
    cache = cache if isinstance(cache, dict) else {}
    catalog = None

    def codex_catalog():
        nonlocal catalog
        from .chats.sources import codex

        catalog = catalog if catalog is not None else codex._catalog()
        return catalog

    def live(rec: dict | None, st) -> Live | None:
        if not rec:
            return None
        s = Live(**{k: v for k, v in rec.items() if k != "edits"})
        # The last message, not the file's mtime: apps rewrite transcripts (titles,
        # compaction) without anyone working in the chat.
        s.updated_at = s.updated_at or st.st_mtime
        s.edits = dict(rec["edits"])
        return s

    found, fresh, changed = [], {}, False
    for path, agent in (_transcripts() if only is None else only).items():
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        fp = f"{st.st_mtime_ns}:{st.st_size}"
        hit = cache.get(path)
        try:
            if not (isinstance(hit, dict) and hit.get("fp") == fp):
                raise KeyError(path)
            rec = hit["rec"]
            s = live(rec, st)
        except (KeyError, TypeError, ValueError):  # not cached, or a cached record of another shape: read it
            rec, changed = _read(agent, path, codex_catalog), True
            s = live(rec, st)
        fresh[path] = {"fp": fp, "rec": rec}
        if s:
            found.append(s)
    if only is not None:
        # The chats not read this time keep their entries, unless their file is gone or older than the window.
        kept = {p: v for p, v in cache.items() if p not in fresh and _cached_since(p, v, since)}
        changed = changed or len(kept) + len(fresh) != len(cache)
        fresh = kept | fresh
    if changed or len(fresh) != len(cache):
        _write_json(CACHE, {"version": CACHE_VERSION, "chats": fresh})

    # A hook's chat id is not always the transcript's, so the transcript path joins them.
    by_id = {s.id: s for s in found}
    by_source = {s.source: s for s in found if s.source}
    for sid, e in _logged(since).items():
        s = by_id.get(sid) or by_source.get(e["transcript"])
        if not s:
            s = Live(sid, e["agent"], f"{AGENTS.get(e['agent'], e['agent'])} chat", e["cwd"], None, e["at"])
            found.append(s)
            by_id[sid] = s
        s.updated_at = max(s.updated_at, e["at"])
        for f, at in e["files"].items():
            _edited(s.edits, f, max(at, s.edits.get(f) or 0))
        for f, spans in e["regions"].items():
            s.regions.setdefault(f, []).extend(spans)
    return found


def _log(
    agent: str, session: str, path: str, cwd: str | None, transcript: str | None, lines: list[list[int]] | None = None
) -> None:
    entry = {"at": time.time(), "agent": agent, "session": session, "file": path, "cwd": cwd}
    if transcript:
        entry["transcript"] = os.path.realpath(transcript)
    if lines:
        entry["lines"] = lines
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(EDITS), exist_ok=True)
        if os.path.exists(EDITS) and os.path.getsize(EDITS) > 512_000:
            # Keep the newest 1,000 lines from the last two days, so the file stays small.
            keep = [x for x in _log_lines() if x.get("at", 0) > time.time() - 2 * 86400][-1000:]
            tmp = f"{EDITS}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                f.writelines(json.dumps(x) + "\n" for x in keep)
            os.replace(tmp, EDITS)
        with open(EDITS, "a") as f:
            f.write(json.dumps(entry) + "\n")


def _log_lines() -> list[dict]:
    out = []
    with contextlib.suppress(OSError), open(EDITS) as f:
        for line in f:
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def _logged(since: float) -> dict[str, dict]:
    """Chats seen by the edit hooks since `since`: agent, folder, transcript, and each file
    with when it was last edited, oldest first."""
    out: dict[str, dict] = {}
    for x in _log_lines():
        if x.get("at", 0) < since or not all(k in x for k in ("agent", "session", "file")):
            continue
        e = out.setdefault(
            f"{x['agent']}:{x['session']}",
            {"agent": x["agent"], "cwd": x.get("cwd"), "transcript": x.get("transcript"), "files": {}, "regions": {}},
        )
        e["at"] = x["at"]
        _edited(e["files"], x["file"], x["at"])
        if spans := x.get("lines"):
            e["regions"].setdefault(x["file"], []).extend(spans)
    return out


def _edit_index(since: float) -> tuple[dict[str, dict], dict[str, str]]:
    """Every transcript that changed since `since`, path -> {"agent", "files"}: the files its
    edit tool calls named, brought up to date by reading only what was written since the last
    look (EDIT_INDEX). A chat that starts is found at the next look through the folder its agent
    writes it to (_watch); every RELIST_SECONDS each agent's whole history is listed again, for a
    chat resumed in a file older than LIST_HORIZON. A record of another shape is read again. Also
    returns the folders each agent's history is under (_roots)."""
    try:
        with open(EDIT_INDEX) as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict) or stored.get("version") != EDIT_INDEX_VERSION:
        stored = {}
    now = time.time()
    held = stored.get("chats") if isinstance(stored.get("chats"), dict) else {}
    chats = {p: r for p, r in held.items() if _record_ok(r)}
    changed = len(chats) != len(held)
    folders = stored.get("folders") if isinstance(stored.get("folders"), dict) else {}
    roots = stored.get("roots") if isinstance(stored.get("roots"), dict) else None
    listed_at, horizon = stored.get("listed_at"), stored.get("horizon")
    if (
        roots is None
        or not isinstance(listed_at, (int, float))
        or not isinstance(horizon, (int, float))
        or now - listed_at > RELIST_SECONDS
        or since < horizon
    ):
        roots = _roots()
        folders = {}
        _watch(roots, folders, scan=False)  # before the glob: a chat that starts during it is found next time
        horizon = min(since, now - LIST_HORIZON)
        recent = {}
        for path, agent in _transcripts().items():
            with contextlib.suppress(OSError):
                if os.stat(path).st_mtime >= horizon:
                    recent[path] = agent
        chats = {p: chats.get(p) or _blank(agent) for p, agent in recent.items()}
        stored |= {"listed_at": now, "horizon": horizon}
        changed = True
    else:
        new, moved = _watch(roots, folders, scan=True)
        for path, agent in new.items():
            chats.setdefault(path, _blank(agent))
        changed = changed or moved
    out = {}
    for path, rec in chats.items():
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        if rec["ino"] != st.st_ino or st.st_size < rec["off"]:  # replaced or cut short: read it again
            rec = chats[path] = {**_blank(rec["agent"]), "ino": st.st_ino}
        if st.st_size > rec["off"]:
            with contextlib.suppress(OSError):
                rec = chats[path] = _read_edits(path, rec)
                changed = True
        out[path] = rec
    if changed:
        _write_json(
            EDIT_INDEX,
            {**stored, "version": EDIT_INDEX_VERSION, "roots": roots, "folders": folders, "chats": chats},
        )
    return out, roots


def _blank(agent: str) -> dict:
    return {"agent": agent, "ino": None, "off": 0, "files": [], "cwd": None}


def _record_ok(r) -> bool:
    """Whether an edit index record has the shape this release writes."""
    return (
        isinstance(r, dict)
        and isinstance(r.get("agent"), str)
        and (r.get("ino") is None or isinstance(r.get("ino"), int))
        and isinstance(r.get("off"), int)
        and isinstance(r.get("files"), list)
        and all(isinstance(f, str) for f in r["files"])
        and (r.get("cwd") is None or isinstance(r.get("cwd"), str))
    )


def _roots() -> dict[str, str]:
    """The folders _transcripts() globs, as the edit index stores them, so a look needs no parser."""
    from .chats.sources import claude, codex, cursor

    return {"claude": claude.ROOT, "codex": os.path.join(codex.ROOT, "sessions"), "cursor": cursor.PROJECTS}


def _watch(roots: dict[str, str], folders: dict[str, int], scan: bool) -> tuple[dict[str, str], bool]:
    """The folders a new chat's transcript appears in, with the time each last changed (`folders`,
    updated in place): each Claude Code project's, each Cursor project's agent-transcripts, and
    Codex's folders for today and yesterday. With `scan`, the transcripts in those that changed
    since the last look are returned (path -> agent), with whether any folder changed. A stat per
    project folder, about a millisecond for a few hundred."""
    seen: dict[str, tuple[str, str]] = {}  # folder -> (agent, what it holds)
    with contextlib.suppress(OSError):
        for e in os.scandir(roots["claude"]):
            if e.is_dir():
                seen[e.path] = ("claude", "files")
    with contextlib.suppress(OSError):
        for e in os.scandir(roots["cursor"]):
            seen[os.path.join(e.path, "agent-transcripts")] = ("cursor", "chats")
    for day in (time.time(), time.time() - 86400):
        seen[os.path.join(roots["codex"], time.strftime("%Y/%m/%d", time.localtime(day)))] = ("codex", "files")
    found, moved = {}, False
    for folder, (agent, holds) in seen.items():
        try:
            mtime = os.stat(folder).st_mtime_ns
        except OSError:
            continue
        if folders.get(folder) == mtime:
            continue
        folders[folder] = mtime
        moved = True
        if scan:
            found |= _chats_in(folder, agent, holds)
    return found, moved


def _chats_in(folder: str, agent: str, holds: str) -> dict[str, str]:
    """The transcripts in one watched folder: its .jsonl files, or for Cursor's agent-transcripts the
    .jsonl in each chat's folder changed lately (a new chat's folder may not hold its file yet; the
    next relist finds it then)."""
    out, recent = {}, time.time() - LIST_HORIZON
    with contextlib.suppress(OSError):
        for e in os.scandir(folder):
            if holds == "files" and e.name.endswith(".jsonl") and e.is_file():
                out[e.path] = agent
            elif holds == "chats" and e.is_dir() and e.stat().st_mtime >= recent:
                with contextlib.suppress(OSError):
                    out |= {c.path: agent for c in os.scandir(e.path) if c.name.endswith(".jsonl") and c.is_file()}
    return out


def _read_edits(path: str, rec: dict) -> dict:
    """`rec` with the files named by the edit tool calls `path` gained since rec["off"]. Only lines
    that can hold one are decoded. A file that no longer ends a line where the last look stopped was
    rewritten, and is read again from the start. Each file is recorded as _placed has it."""
    agent, off, folder = rec["agent"], rec["off"], rec.get("cwd")
    files = dict.fromkeys(rec["files"])
    with open(path, "rb") as f:
        if off:
            f.seek(off - 1)
            if f.read(1) != b"\n":
                off, files, folder = 0, {}, None
                f.seek(0)
        data = f.read()
    end = data.rfind(b"\n") + 1  # whole lines only: the rest is still being written
    if agent == "codex" and not off:  # a patch names files relative to where Codex ran
        with contextlib.suppress(Exception):
            first = json.loads(data[: data.find(b"\n")])
            folder = (first.get("payload") or {}).get("cwd") if first.get("type") == "session_meta" else None
    marker = _EDIT_LINE["codex" if agent == "codex" else "other"]
    m = marker.search(data, 0, end)
    while m:
        start, stop = data.rfind(b"\n", 0, m.start()) + 1, data.find(b"\n", m.start(), end)
        with contextlib.suppress(Exception):  # a line _named cannot make sense of names nothing
            d = json.loads(data[start:stop])
            here = folder if agent == "codex" else d.get("cwd")  # Claude Code writes its folder on every line
            for named in _named(agent, d, None) if isinstance(d, dict) else []:
                files.update(dict.fromkeys(_placed(named, here if isinstance(here, str) else None)))
        m = marker.search(data, stop, end)
    return {**rec, "off": off + end, "files": list(files), "cwd": folder}


def _placed(path: str, folder: str | None) -> list[str]:
    """How the first look records a file an edit named: a full path as the real file it names, as
    _resolve places it. A relative one also as written, cleaned of leading "..", since the folder
    _resolve takes it from (the chat's, which Codex's catalogue may give) can differ from the one
    the transcript says."""
    if os.path.isabs(path):
        return [os.path.realpath(path)]
    parts = os.path.normpath(path).split(os.sep)
    while parts and parts[0] == "..":
        parts.pop(0)
    out = [os.path.realpath(os.path.join(folder, path))] if folder else []
    return out + ([os.sep.join(parts)] if parts and parts != ["."] else [])


def _others_may_edit(
    agent: str, session: str, repo: str, rel: str, since: float, source: str | None, tops: dict[str, str | None]
) -> tuple[dict[str, str] | None, str | None, dict[str, set[str]]]:
    """The edit hook's first look: the transcripts (path -> agent) of other chats whose edit tool
    calls, or lines in the edit log, may name `rel` (the file being edited, by its path in the
    repo), or None when no other chat can have edited it; this chat's own transcript, if seen; and
    for each of those transcripts the checkouts its edits of `rel` were in ("" for one not known).
    It errs towards too many: those chats are then read in full, and git decides. A full path
    (placed when it was recorded: _placed; the edit log's are real paths) counts when its checkout
    is this repo's and its path in it is `rel`; a relative one, from a chat whose folder is not
    known, when either path ends with the other. This chat's own transcript is the hook's, or the
    one named for its session id, as Claude Code, Cursor and Codex name them."""
    repos: dict[str, str | None] = {}

    @functools.cache
    def checkout_of(p: str) -> str | None:
        """The checkout of this repo in which `p` is `rel`; "" when it may be, None when not."""
        if not os.path.isabs(p):
            return "" if p == rel or rel.endswith(os.sep + p) or p.endswith(os.sep + rel) else None
        if not p.endswith(os.sep + rel):
            return None
        t = _top(p, tops)
        return t if t and os.path.relpath(p, t) == rel and repos.setdefault(t, _repo_dir(t)) == repo else None

    me, hit, out, logged = f"{agent}:{session}", False, {}, {}
    places: dict[str, set[str]] = {}
    for x in _log_lines():
        if x.get("at", 0) < since or not all(k in x for k in ("agent", "session", "file")):
            continue
        if f"{x['agent']}:{x['session']}" != me and (t := checkout_of(x["file"])) is not None:
            hit = True
            if x.get("transcript"):
                logged.setdefault(x["transcript"], set()).add(t)
    index, roots = _edit_index(since)
    own = None
    named = (os.sep + f"{session}.jsonl", f"-{session}.jsonl") if len(session or "") >= 8 else None
    for path, rec in index.items():
        if named and rec["agent"] == agent and path.endswith(named):
            own = path
        elif where := {t for f in rec["files"] if (t := checkout_of(f)) is not None}:
            if os.path.realpath(path) == source:
                own = path
            else:
                hit = True
                out[path] = rec["agent"]
                places[path] = where
    if logged:  # read by the path a full scan finds them at; a chat no scan finds is known by its log lines
        listed = {os.path.realpath(p): p for p in index}
        for real, where in logged.items():
            if real == source:
                continue
            if real in listed:
                out[listed[real]] = index[listed[real]]["agent"]
                places.setdefault(listed[real], set()).update(where)
            elif kind := _transcript_agent(real, roots):
                out[real] = kind
                places.setdefault(real, set()).update(where)
    return (out if hit else None), own, places


WHOLE = [1, 10**9]  # an edit that rewrites the whole file


def edit_lines(path: str, olds: list[str], news: list[str]) -> list[list[int]] | None:
    """The lines an edit is about to change, one [first, last] per replaced piece of text; the
    new text's length sets where each range ends. None when any text cannot be found."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    spans = []
    for old, new in zip(olds, news, strict=False):
        at = text.find(old) if old else -1
        if at < 0:
            return None
        first = text.count("\n", 0, at) + 1
        spans.append([first, first + max(old.rstrip("\n").count("\n"), new.rstrip("\n").count("\n"))])
    return spans or None


def content_lines(path: str, content: str) -> list[list[int]] | None:
    """The lines a whole-file write changes, as [first, last] ranges in the file as it stands,
    by comparing it with the new content. Cursor sends every edit this way, and Claude Code's
    Write does too. A new file is all new."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            old = f.read().splitlines()
    except FileNotFoundError:
        return [WHOLE]
    except OSError:
        return None
    matcher = difflib.SequenceMatcher(None, old, content.splitlines(), autojunk=False)
    return [[i1 + 1, max(i1 + 1, i2)] for tag, i1, i2, _, _ in matcher.get_opcodes() if tag != "equal"] or None


def _apart(mine: list[list[int]], theirs: list[list[int]], gap: int = 3) -> bool:
    """Whether two sets of line ranges are clear of each other by more than `gap` lines."""
    return bool(mine and theirs) and all(a[0] > b[1] + gap or b[0] > a[1] + gap for a in mine for b in theirs)


def _span(ranges: list[list[int]]) -> str:
    if any(r == WHOLE for r in ranges):
        return "the whole file"
    parts = [f"{a}" if a == b else f"{a}–{b}" for a, b in sorted({tuple(r) for r in ranges})]
    return "line" + ("s " if len(parts) > 1 or "–" in parts[0] else " ") + ", ".join(parts)


# --- git --------------------------------------------------------------------------


# Paths with non-ASCII letters come back as written, not octal-escaped, so every git listing
# names a file the same way the agents do.
_GIT = ("-c", "core.quotePath=false")
# recall only reads repos, while agents may be committing in them: no index refresh or lock
# (GIT_OPTIONAL_LOCKS), no network, no prompts, and no repo location inherited from a git hook.
# Only the variables that point git at another repo are dropped; config ones (GIT_CONFIG_*,
# safe.directory through them) stay.
_REPO_VARS = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
}
_ENV = {k: v for k, v in os.environ.items() if k not in _REPO_VARS} | {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


@functools.cache
def _git_bin() -> str:
    """The git to run. On a Mac, /usr/bin/git is a stub that asks xcrun for the developer tools'
    git on every call, which doubles what each call costs (about 15 ms against 7): the edit hook
    makes several, so it runs the one the stub would: DEVELOPER_DIR's, else xcode-select's, else
    the only developer tools installed. Anything less clear-cut keeps the stub."""
    found = shutil.which("git") or "git"
    if sys.platform != "darwin" or found != "/usr/bin/git":
        return found
    link = "/var/db/xcode_select_link"
    if os.environ.get("DEVELOPER_DIR"):
        tools = [os.environ["DEVELOPER_DIR"]]
    elif os.path.islink(link):
        tools = [os.path.realpath(link)]
    else:
        tools = [
            d
            for d in ("/Applications/Xcode.app/Contents/Developer", "/Library/Developer/CommandLineTools")
            if os.access(os.path.join(d, "usr", "bin", "git"), os.X_OK)
        ]
    real = os.path.join(tools[0], "usr", "bin", "git") if len(tools) == 1 else ""
    return real if real and os.access(real, os.X_OK) else found


def _git(folder: str, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(
            [_git_bin(), *_GIT, "-C", folder, *args], capture_output=True, text=True, timeout=5, env=_ENV
        )
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


def _together(*calls):
    """The results of these functions, run at once in threads: a git call is mostly the wait
    for its process (about 10 ms each on a Mac), so independent ones need not queue."""
    import threading

    out: list = [None] * len(calls)
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            out[i] = calls[i]()
        except BaseException as e:  # raised in the caller, as if the calls had run in turn
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return out


def _checkout(top: str) -> Checkout | None:
    # -z keeps paths with spaces or non-ASCII letters unquoted; a rename's old path follows it.
    info, out, ref = _together(
        lambda: _git(top, "rev-parse", "--git-common-dir", "--abbrev-ref", "HEAD"),
        lambda: _git(top, "status", "--porcelain", "-z", "--untracked-files=all"),
        lambda: _git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
    )
    if not info or len(info) < 2:
        return None
    tokens = "\n".join(out or []).split("\0")
    porcelain, i = [], 0
    while i < len(tokens):
        if tokens[i]:
            porcelain.append(tokens[i])
            i += 1 if set(tokens[i][:2]) & {"R", "C"} else 0
        i += 1
    open_ = {line[3:] for line in porcelain}
    default = (ref or ["main"])[0]
    found, differs = _together(
        lambda: _git(top, "merge-base", default, "HEAD"), lambda: _git(top, "diff", "--name-only", default)
    )
    base = (found or [None])[0]
    if base:
        open_.update(_git(top, "diff", "--name-only", base, "HEAD") or [])
    # A squash-merged or rebased branch keeps its own commits, so only what still differs
    # from the default branch's tip is open. New untracked files always are.
    if differs is not None:
        untracked = {line[3:] for line in porcelain if line.startswith("??")}
        open_ = (open_ & set(differs)) | untracked
    branch = info[1] if info[1] != "HEAD" else None
    untracked = {line[3:] for line in porcelain if line.startswith("??")}
    return Checkout(top, os.path.realpath(os.path.join(top, info[0])), branch, base, open_, len(porcelain), untracked)


def _committed_since(c: Checkout, rel: str, when: float | None) -> bool:
    """Whether `rel` was committed in this checkout after `when`, so a chat's edit from
    then is no longer among the open changes."""
    if not when:
        return False
    out = _git(c.top, "log", "-1", "--format=%ct", "--", rel)
    return bool(out and out[0].isdigit() and int(out[0]) >= when)


@functools.cache
def _hunks(top: str, base: str, rel: str, untracked: bool) -> tuple[tuple[float, float], ...]:
    """What a checkout's working copy changed in `rel` since `base`, as spans on a line axis
    where line k covers [k - 0.5, k + 0.5] and an insertion after line k is the point k + 0.5.
    A binary or untracked new file counts as changed throughout."""
    out = []
    for line in _git(top, "diff", "--no-textconv", "--no-ext-diff", "-U0", base, "--", rel) or []:
        if line.startswith("Binary files"):
            return ((0, math.inf),)
        if m := re.match(r"@@ -(\d+)(?:,(\d+))? ", line):
            start, count = int(m[1]), int(m[2] or 1)
            out.append((start + 0.5, start + 0.5) if count == 0 else (start - 0.5, start + count - 0.5))
    return ((0, math.inf),) if not out and untracked else tuple(out)


@functools.cache
def _merge_base(a: str, b: str) -> str | None:
    heads = [(h or [None])[0] for h in _together(*(lambda top=top: _git(top, "rev-parse", "HEAD") for top in (a, b)))]
    return (_git(a, "merge-base", *heads) or [None])[0] if None not in heads else None


def _fresh() -> None:
    """Forget cached git answers: the MCP server is long-lived and repos move."""
    _hunks.cache_clear()
    _merge_base.cache_clear()


def _conflict(a: Checkout, b: Checkout, rel: str) -> bool:
    """Whether merging two checkouts as they stand would conflict on `rel`: their changes since
    the common base touch or overlap, with no unchanged line between them, the test git's own
    merge applies. Read-only, unlike git merge-tree and stash create, which write objects into
    the repo. (A rename on one side reads as a deletion there, so it counts as a conflict.)"""
    base = _merge_base(a.top, b.top)
    if not base:
        return False
    mine, theirs = _together(
        lambda: _hunks(a.top, base, rel, rel in a.untracked), lambda: _hunks(b.top, base, rel, rel in b.untracked)
    )
    return any(x0 <= y1 and y0 <= x1 for x0, x1 in mine for y0, y1 in theirs)


def _resolve(s: Live, tops: dict[str, str | None]) -> list[tuple[str, str, float | None]]:
    """A chat's edits as (checkout, repo-relative path, when), oldest first; edits outside
    any git checkout are dropped."""
    out = []
    for f, when in s.edits.items():
        full = os.path.realpath(f if os.path.isabs(f) or not s.folder else os.path.join(s.folder, f))
        if top := _top(full, tops):
            out.append((top, os.path.relpath(full, top), when))
    return out


def _place(sessions: list[Live]) -> dict[str, Checkout]:
    """Resolve every edit to its own checkout (a session can edit in a worktree it never
    started in) and place each session where it last edited."""
    tops: dict[str, str | None] = {}
    checkouts: dict[str, Checkout | None] = {}
    for s in sessions:
        last = None
        for top, rel, when in _resolve(s, tops):
            if top not in checkouts:
                checkouts[top] = _checkout(top)
            if checkouts[top]:
                s.touched.append((top, rel, when))
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
    "apart": "you are changing different lines of one working copy, so with targeted edits you can both carry on",
    "overlap": "the two merge cleanly for now",
}
ACTIONS = ("worktree", "ask", "tell", "off")  # strongest first
PRESETS = {
    "recommended": (
        {"same_copy": "ask", "conflict": "tell", "apart": "tell", "overlap": "off"},
        "stop and ask you before two live chats edit the same lines of one working copy; mention conflicts",
    ),
    "hands-off": (
        {"same_copy": "worktree", "conflict": "tell", "apart": "tell", "overlap": "off"},
        "move the agent to its own worktree instead of asking",
    ),
    "tell": (
        {"same_copy": "tell", "conflict": "tell", "apart": "tell", "overlap": "tell"},
        "agents mention every overlap and never stop",
    ),
    "off": ({"same_copy": "off", "conflict": "off", "apart": "off", "overlap": "off"}, "no checks on edits"),
}


def rules() -> dict[str, str]:
    from . import config

    got = config.load()["coordination"]
    return {k: got[k] if got.get(k) in ACTIONS else PRESETS["recommended"][0][k] for k in SITUATIONS}


def _about(other: Live, c: Checkout, situation: str, files: list[str], same: bool) -> str:
    """One line on another chat's open changes to files this agent is touching. `same`: the
    other chat's changes are in this agent's working copy."""
    where = "this same working copy" if same else f"branch {c.branch or 'detached'}"
    risk = SITUATIONS[situation]
    if same and situation == "overlap":
        risk = "it has gone quiet, but its changes are still uncommitted here"
    who = AGENTS.get(other.agent, other.agent)
    line = (
        f'"{other.title}" ({who}, {where}, active {_ago(time.time() - other.updated_at)}) also has open '
        f"changes to {', '.join(files)}: {risk}."
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
    from . import mail

    _fresh()
    now = time.time()
    sessions = sorted(_sessions(since), key=lambda s: -s.updated_at)
    checkouts = _place(sessions)
    letters = mail.recent(since)
    # Messages name a chat by id or transcript; either finds it.
    alias = {k: s.id for s in sessions for k in (s.id, s.source) if k} | {"recall": "recall"}

    # A file that two sessions in one repo both edited and that is still open where each
    # edited it; work already in the default branch drops out. Its level:
    #   act    the checkouts would conflict on it if merged now, or two live sessions are
    #          editing it in the same working tree
    #   watch  anything else: it merges clean, or one of the sessions has gone quiet
    # (repo, file) -> {chat id: the checkout it last edited the file in}
    touched: dict[tuple[str, str], dict[str, str]] = {}
    by_id = {s.id: s for s in sessions}
    for s in sessions:
        for top, f, when in s.touched:
            c = checkouts[top]
            if f in c.open and not _committed_since(c, f, when):
                touched.setdefault((c.repo, f), {})[s.id] = top
    overlaps = []
    for (repo, f), side in sorted(touched.items(), key=lambda kv: kv[0][1]):
        if len(side) < 2:
            continue
        group = {sid: by_id[sid] for sid in side}
        tops = sorted(set(side.values()))
        live = sum(now - s.updated_at <= LIVE_SECONDS for s in group.values()) >= 2
        conflict = None
        if len(tops) > 1:
            conflict = any(_conflict(checkouts[a], checkouts[b], f) for a, b in combinations(tops, 2))
        spans = [group[sid].regions.get(os.path.join(side[sid], f), []) for sid in group]
        apart = conflict is None and live and all(_apart(a, b) for a, b in combinations(spans, 2))
        level = "act" if conflict or (conflict is None and live and not apart) else "watch"
        situation = (
            "conflict" if conflict else "apart" if apart else "same_copy" if conflict is None and live else "overlap"
        )
        overlaps.append(
            {
                "repo": _repo_name(repo),
                "file": f,
                "sessions": list(group),
                "same_checkout": len(tops) == 1,
                "branches": [checkouts[t].branch or "detached" for t in tops],
                "live": live,
                "conflict": conflict,
                "situation": situation,
                "level": level,
                "sides": [
                    {
                        "session": sid,
                        "source": group[sid].source,
                        "checkout": side[sid],
                        "branch": checkouts[side[sid]].branch,
                        "base": checkouts[side[sid]].base,
                    }
                    for sid in group
                ],
                # What the chats have said to each other, oldest first.
                "messages": [
                    {"at": m["at"], "from": sender, "to": recipient, "text": m["text"]}
                    for m in letters
                    if (sender := alias.get(m.get("from")) or alias.get(m.get("sender_source"))) in {*group, "recall"}
                    and (recipient := alias.get(m.get("to")) or alias.get(m.get("to_source"))) in group
                ],
                # For each session, a note to paste into it about the others.
                "key": f"{repo}:{f}",
                "notes": {
                    sid: "\n".join(
                        _about(o, checkouts[side[oid]], situation, [f], side[oid] == side[sid])
                        for oid, o in group.items()
                        if oid != sid
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
                "files": sorted({f for _, f, _ in s.touched}),
            }
        )
    # Overlaps the person pressed Resolve on, while every chat in them has been sent the note;
    # in the menu's full window, marks for overlaps that are gone are dropped.
    marks = mail.resolved()
    for o in overlaps:
        mark = marks.get(o["key"])
        o["sent_at"] = mark["at"] if mark and set(o["sessions"]) <= set(mark["sessions"]) else None
    if since <= now - 12 * 3600:
        mail.forget_resolved({o["key"] for o in overlaps})
    levels = {o["level"] for o in overlaps}
    return {
        "generated_at": now,
        "since": since,
        "live_seconds": LIVE_SECONDS,
        "level": "act" if "act" in levels else "watch" if levels else "none",
        "sessions": out,
        "overlaps": overlaps,
    }


# An agent's MCP server keeps the recall it started with until the agent restarts. A server is
# found by its script (`<env>/bin/recall serve`; the interpreter in front of it may be one a
# framework Python re-launched) or by `<env>/bin/python -m recall serve`, and its agent by the
# program (the command line before its first flag) of its nearest ancestor that names one.
_SCRIPT = re.compile(r"/bin/recall\s+serve(?:\s|$)")
_MODULE = re.compile(r"(.*?)/bin/python[\d.]*\s+-m\s+recall\s+serve(?:\s|$)")
_HOSTS = (
    ("cursor-agent", "cursor-agent"),
    ("cursor", "Cursor"),
    ("codex", "Codex"),
    ("claude", "Claude Code"),
    ("gemini", "Gemini CLI"),
    ("opencode", "OpenCode"),
)


def outlived() -> list[dict]:
    """recall MCP servers still running code an upgrade has since replaced: each started before recall
    was last installed into the environment it runs from. Until the agent that started one restarts,
    it answers with the old code, and an old enough one rebuilds the chat index for its own release.
    Oldest first; empty when `ps` cannot be run."""
    try:
        listing = subprocess.run(
            ["ps", "-ww", "-A", "-o", "pid=,ppid=,uid=,lstart=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs: dict[int, tuple[int, float, str]] = {}  # every process, for the walk up to an agent
    mine: set[int] = set()  # only this user's servers are theirs to restart
    for line in listing.splitlines():
        parts = line.split(maxsplit=8)  # lstart is five words: "Thu Oct 15 20:39:27 2026"
        with contextlib.suppress(ValueError, IndexError):
            started = time.mktime(time.strptime(" ".join(parts[3:8]), "%a %b %d %H:%M:%S %Y"))
            procs[int(parts[0])] = (int(parts[1]), started, parts[8])
            if int(parts[2]) == os.getuid():
                mine.add(int(parts[0]))
    found = []
    for pid in mine:
        ppid, started, args = procs[pid]
        env = _env(args) if "recall" in args else None
        installs = env and glob.glob(
            os.path.join(glob.escape(env), "lib", "python*", "site-packages", "recall_agents-*.dist-info")
        )
        try:  # an upgrade may be replacing the files right now
            newest = installs and max(installs, key=os.path.getmtime)
            installed_at = os.path.getmtime(newest) if newest else 0
        except OSError:
            continue
        version = newest and re.fullmatch(r"recall_agents-([\w.+]+)\.dist-info", os.path.basename(newest))
        if not version or started >= installed_at - 2:  # lstart is to the second
            continue
        agent, chat = _host(procs, ppid)
        found.append(
            {
                "pid": pid,
                "agent": agent,
                "chat": chat,
                "started_at": started,
                "installed": version[1],
                "installed_at": installed_at,
            }
        )
    return sorted(found, key=lambda x: x["started_at"])


def _env(args: str) -> str | None:
    """The environment a `recall serve` command line runs from, or None if it is not one."""
    if m := _SCRIPT.search(args):
        before = args[: m.start()]
        # Paths may hold spaces, so try each word that starts a path, longest first. A script reached
        # through a link (~/.local/bin/recall) runs from the environment the link points into.
        starts = [i for i, ch in enumerate(before) if ch == "/" and (i == 0 or before[i - 1] == " ")]
        script = next((p for i in starts if os.path.isfile(p := os.path.join(before[i:], "bin", "recall"))), None)
        return os.path.dirname(os.path.dirname(os.path.realpath(script))) if script else None
    m = _MODULE.match(args)
    return m[1] if m else None


def _host(procs: dict[int, tuple[int, float, str]], pid: int) -> tuple[str, str | None]:
    """The agent that started a process, and for Claude Code the chat it resumed (its id)."""
    for _ in range(6):
        if pid <= 1 or pid not in procs:
            break
        ppid, _, args = procs[pid]
        program = args.split(" -", 1)[0].lower()
        for needle, name in _HOSTS:
            if needle in program:
                resumed = re.search(r"--resume[= ]([\w-]+)", args) if needle == "claude" else None
                return name, f"claude:{resumed[1]}" if resumed else None
        pid = ppid
    return "an agent", None


def chat(chat_id: str, since: float) -> Live | None:
    """A recent chat by id."""
    return next((s for s in _sessions(since) if s.id == chat_id), None)


def elsewhere(path: str, since: float) -> str:
    """For agents without an edit hook: every chat with open changes in the repo at `path`,
    the files, and which of them would conflict with the checkout `path` is in."""
    _fresh()
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
        mine = [
            (t, f)
            for t, f, when in s.touched
            if checkouts[t].repo == here.repo and f in checkouts[t].open and not _committed_since(checkouts[t], f, when)
        ]
        if not mine:
            continue
        clash = {f for t, f in mine if t != top and _conflict(here, checkouts[t], f)}
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
    agent: str,
    session_id: str,
    path: str,
    cwd: str | None,
    since: float,
    acts=("worktree", "ask", "tell"),
    transcript: str | None = None,
    lines: list[list[int]] | None = None,
) -> Decision | None:
    """Called when an agent is about to edit `path` (`lines`: the ranges the edit changes, if
    known). Logs the edit, then checks other chats with open changes to the same file in the
    same repo and applies the person's rule for the worst situation found, if it is one of
    `acts` (an agent hook may only be able to refuse before an edit and to inform after it).
    The other chat gets a message about it too. Each other chat is raised once, listing every
    file the two share, and again only if things get worse."""
    _fresh()
    from . import mail

    path = os.path.realpath(path if os.path.isabs(path) or not cwd else os.path.join(cwd, path))
    _log(agent, session_id, path, cwd, transcript, lines)
    tops: dict[str, str | None] = {}
    top = _top(path, tops)
    repo = _repo_dir(top) if top else None
    want = rules()
    if not repo or not any(a in acts for a in want.values()):
        return None
    rel = os.path.relpath(path, top)
    me = f"{agent}:{session_id}"
    source = os.path.realpath(transcript) if transcript else None
    repos: dict[str, str | None] = {}

    def in_repo(s: Live) -> dict[str, tuple[str, float | None]]:
        """The chat's edits in this repo: repo-relative path -> (checkout, when)."""
        return {f: (t, when) for t, f, when in _resolve(s, tops) if repos.setdefault(t, _repo_dir(t)) == repo}

    def is_me(s: Live) -> bool:
        return s.id == me or bool(source and s.source == source)

    # Most edits are to a file no other chat has touched: the first look settles those without
    # reading any chat in full or running git. Otherwise only the chats it names are read.
    near, own, places = _others_may_edit(agent, session_id, repo, rel, since, source, tops)
    if near is None:
        return None
    rank = {name: i for i, name in enumerate(reversed(SITUATIONS))}  # overlap 0 … same_copy 3
    try:
        with open(WARNED) as f:
            warned = json.load(f)
    except (OSError, json.JSONDecodeError):
        warned = {}
    # Anything of another shape counts as never raised, and is rewritten with the next warning.
    warned = {k: v for k, v in warned.items() if isinstance(v, dict)} if isinstance(warned, dict) else {}
    held = warned.get(me, {}).get("chats")
    seen = {k: v for k, v in held.items() if isinstance(v, int)} if isinstance(held, dict) else {}

    def worst(t: str) -> int:
        """The worst situation a chat with changes in checkout `t` can still be raised for."""
        could = ("same_copy", "apart", "overlap") if t == top else ("conflict", "overlap")
        return max((rank[sit] for sit in could if want[sit] in acts), default=-1)

    # A chat already raised for the worst it can be is not raised again, so it needs no git, and one
    # whose id is its transcript's name (Claude Code's, Cursor's) need not even be read.
    for p, kind in list(near.items()):
        where = places.get(p) or {""}
        cid = f"{kind}:{os.path.basename(p).removesuffix('.jsonl')}"
        if kind in ("claude", "cursor") and "" not in where and seen.get(cid, -1) >= max(map(worst, where)):
            del near[p]
    sessions = _sessions(since, only=near)
    others = [(s, theirs) for s in sessions if not is_me(s) and rel in (theirs := in_repo(s))]
    others = [(s, theirs) for s, theirs in others if worst(theirs[rel][0]) > seen.get(s.id, -1)]
    if not others:
        return None
    mine = next((s for s in sessions if s.id == me), None) or next((s for s in sessions if is_me(s)), None)
    # After an edit (Cursor's postToolUse) the file already holds it; the lines this chat's
    # hook recorded before the edit stand in.
    lines = lines or (mine.regions.get(path) if mine else None)
    checkouts: dict[str, Checkout | None] = {}

    def checkout(t: str) -> Checkout | None:
        if t not in checkouts:
            checkouts[t] = _checkout(t)
        return checkouts[t]

    found = []
    for s, theirs in others:
        t, when = theirs[rel]
        _together(*(lambda x=x: checkout(x) for x in {t, top} - set(checkouts)))  # git for both at once
        c = checkout(t)
        if not c or rel not in c.open or _committed_since(c, rel, when):
            continue
        if not checkout(top):  # without this checkout's own state nothing can be judged
            return None
        spans = s.regions.get(os.path.join(t, rel), [])
        if t == top and time.time() - s.updated_at <= LIVE_SECONDS:
            situation = "apart" if lines and _apart(lines, spans) else "same_copy"
        elif t != top and _conflict(checkouts[top], c, rel):
            situation = "conflict"
        else:
            situation = "overlap"
        if want[situation] in acts:
            found.append((situation, s, c, theirs, t, spans))
    raise_ = [x for x in found if rank[x[0]] > seen.get(x[1].id, -1)]
    if not raise_:
        return None
    now = time.time()
    warned = {k: v for k, v in warned.items() if isinstance(v.get("at"), (int, float)) and now - v["at"] < 2 * 86400}
    warned[me] = {"at": now, "chats": {**seen, **{s.id: rank[sit] for sit, s, *_ in raise_}}}
    _write_json(WARNED, warned)

    # This chat's own transcript is read only now there is something to say: its title, what it
    # was last asked, and the other files it edited, which the two chats may share.
    own = own or transcript
    if own and not {own, source} & set(near) and (kind := _transcript_agent(own)):
        sessions = _sessions(since, only={**near, own: kind})
        mine = next((s for s in sessions if s.id == me), None) or next((s for s in sessions if is_me(s)), mine)
    my_files = {f for s in sessions if is_me(s) for f in in_repo(s)} | {rel}
    fresh = [
        (
            sit,
            s,
            c,
            sorted(f for f, (ft, _) in theirs.items() if f in my_files and ft == t and f in c.open),
            t == top,
            spans,
        )
        for sit, s, c, theirs, t, spans in raise_
    ]

    action = min((want[sit] for sit, *_ in fresh), key=ACTIONS.index)
    default = (_git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ["main"])[0]
    my_title = mine.title if mine else f"{AGENTS.get(agent, agent)} chat"
    mine_where = checkouts[top].branch or "detached"
    token = mail.grant(
        me,
        source,
        {s.id: {"source": s.source, "title": s.title, "file": rel, "repo": _repo_name(repo)} for _, s, *_ in fresh},
        my_title if mine else None,
    )
    out = []
    for sit, s, c, shared, same, spans in fresh:
        line = "recall: " + _about(s, c, sit, shared, same)
        if spans:
            line += f" Its edits there: {_span(spans)}."
        if lines:
            line += f" Yours: {_span(lines)}."
        out.append(
            line
            + " recall has told that chat too"
            + (
                f'; to message it, use the recall MCP server\'s message_chat tool with token="{token}" and to="{s.id}".'
                if token
                else "."
            )
        )
        # The other side hears it from recall, so the two can agree who goes first.
        about = f"{_span(lines)} of " if lines else ""
        where = "in your working copy" if same else f"on branch {mine_where}"
        note = (
            f'"{my_title}" ({AGENTS.get(agent, agent)}, {where}) is about to change {about}{rel}, which you also '
            f"have open changes to: {SITUATIONS[sit]}."
        )
        if spans:
            note += f" Your edits there: {_span(spans)}."
        if mine and mine.ask:
            note += f' It was asked: "{_line(mine.ask, 140)}"'
        note += " If you are changing the same part, agree with it who goes first."
        mail.post(s.id, me, my_title, note, to_source=s.source, file=rel, repo=_repo_name(repo), sender_source=source)
    out.append(_instruction(action, rel, _repo_name(repo), default))
    worst, other = max(((sit, s) for sit, s, *_ in fresh), key=lambda x: rank[x[0]])
    words = {
        "same_copy": " in this working copy",
        "conflict": " and would conflict",
        "apart": ", on other lines",
        "overlap": "",
    }[worst]
    notice = f"recall: {os.path.basename(path)} is also open in “{other.title}”{words}"
    return Decision(action, "\n".join(out), notice)


def on_edit(event):
    """The edit hook (hooks.py): the person's rules for each file the edit touches."""
    from . import usage
    from .integrations.hooks import Reply

    if window := event.raw.get("recall_since"):
        from .chats.store import parse_since

        since = parse_since(window)
    else:
        since = time.time() - 12 * 3600  # the default 12h, without loading the chat index's module
    found = [
        d
        for f in event.files
        if (
            d := check_edit(
                event.agent, event.session, f, event.cwd, since, event.acts, event.transcript, event.lines(f)
            )
        )
    ]
    if not found:
        return None
    action = min((d.action for d in found), key=ACTIONS.index)
    message = "\n".join(dict.fromkeys(d.message for d in found))
    # The usage log keeps the message, not the reply token in it.
    logged = re.sub(r'token="\w+"', 'token="…"', message)
    usage.record("hook", "check_edit", {"files": event.files, "action": action}, logged, client=event.agent)
    refuse = action in ("worktree", "ask")
    return Reply(context=None if refuse else message, deny=message if refuse else None, notice=found[0].notice)
