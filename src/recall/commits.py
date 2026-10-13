"""Which chats produced which commits, worked out on this machine alone.

For each chat recall reads the files its agent edited and the lines it left in them, from
the same tool calls status.py reads (Edit, Write, StrReplace, apply_patch). It then matches
them with commits in that repo: made after the edits, touching the same files, and adding
the same lines. When two chats wrote the same line, the one that wrote it last before the
commit gets it. A chat that ran `git commit` itself is matched by the sha git printed, or by
the commit's subject when the agent keeps no command output (Cursor).

Private by design. Links live only in recall's index. Git is only read (rev-list, log, show,
blame), with optional locks off, so nothing is written to any repo: no commit messages,
trailers, notes, refs, branches or hooks, and nothing is pushed. Every table here is derived:
it can be rebuilt from the chats (which the archive keeps) and git. A chat whose transcript
is gone keeps the edits read from it while it existed. `[links] enabled = false` in
config.toml turns linking off and drops these tables.

Projects are keyed on their git remote (chats/projects.py), so worktrees and clones of one
repo share their links. Squash merges and rebases need nothing special: the new commit adds
the same lines, so it links to the same chats.
"""

import contextlib
import glob
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .chats.projects import resolve
from .chats.redact import redact
from .chats.sources import iso_ts, read_jsonl
from .chats.transcript import clean_user_text

SCHEMA_VERSION = "1"  # bump to rebuild every table below on next run
TABLES = (
    "link_chats",
    "link_edits",
    "link_events",
    "link_checkouts",
    "link_repos",
    "link_commits",
    "link_commit_files",
    "commit_links",
)
SCHEMA = """
create table if not exists link_chats (session_id text primary key, fingerprint text, read_at real);
create table if not exists link_edits (
  session_id text, repo text, path text, first_at real, last_at real, lines blob, weak blob, n int,
  first_ask text, ask text, primary key (session_id, repo, path)
);
create index if not exists link_edits_repo on link_edits(repo, path);
create table if not exists link_events (session_id text, n int, at real, kind text, text text, detail text);
create index if not exists link_events_session on link_events(session_id, n);
create table if not exists link_checkouts (repo text, top text, primary key (repo, top));
create table if not exists link_repos (repo text primary key, dirty int, linked_at real);
create table if not exists link_commits (
  repo text, sha text, parent text, at real, authored real, author text, subject text, gitdir text,
  diffed int default 0, n int, primary key (repo, sha)
);
create index if not exists link_commits_at on link_commits(repo, at);
create table if not exists link_commit_files (
  repo text, sha text, path text, lines blob, n int, primary key (repo, sha, path)
);
create index if not exists link_commit_files_path on link_commit_files(repo, path);
create table if not exists commit_links (
  repo text, sha text, session_id text, score real, how text, lines int, commit_lines int, chat_lines int,
  files text, ask text, primary key (repo, sha, session_id)
);
create index if not exists commit_links_session on commit_links(session_id);
"""

SLACK = 10 * 60  # a commit this long before an edit's recorded time can still hold it (clocks, coarse times)
WINDOW = 30 * 86400  # a commit this long after a chat's last edit to a file can still hold it
MIN_LINES = 3  # lines a chat must have written into a commit to link by content
MIN_SHARE = 0.2  # ...and the share of the commit they make up,
MIN_KEPT = 0.3  # ...or the share of what the chat wrote to those files that the commit holds
MAX_FILE_LINES = 5000  # a file adding more lines than this in one commit is generated or vendored
# Files whose lines say nothing about who wrote the code: lockfiles and build output.
_GENERATED = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|"
    r"go\.sum|composer\.lock|Podfile\.lock|Package\.resolved)$|\.(min\.js|min\.css|map|snap|lock)$"
)
_EMPTY = "0" * 40


# --- reading what a chat did ---------------------------------------------------------


@dataclass
class Edit:
    path: str  # as the agent named it: absolute, or relative to the chat's folder
    at: float | None
    added: list[str]
    removed: list[str] = field(default_factory=list)
    whole: bool = False  # the file's whole content, whose new lines are not known (weak evidence)
    ask: str = ""  # what the person asked last before this edit


@dataclass
class Work:
    folder: str | None = None
    edits: list[Edit] = field(default_factory=list)
    events: list[tuple[float | None, str, str, str]] = field(default_factory=list)  # at, kind, text, detail
    edited: bool = False  # an edit has happened by this point in the chat


_EDIT_KEYS = ("file_path", "notebook_path", "path", "target_file", "target_notebook")
# Commands whose failure means the code under work is wrong, rather than a typo in a shell line.
_CHECK = re.compile(
    r"\b(pytest|unittest|tox|nox|jest|vitest|mocha|playwright|cypress|go (test|vet|build)|cargo (test|build|check|"
    r"clippy)|swift (test|build)|xcodebuild|gradlew?|mvn|tsc|eslint|ruff|mypy|pyright|flake8|mix test|rspec|"
    r"(npm|pnpm|yarn|bun) (run )?(test|build|lint|typecheck|check)|make( |$)|uv run|python[3]? -m|node )"
)
# A person telling the agent its work is wrong or still broken. Read from the start of a message.
_CORRECTION = re.compile(
    r"(?i)\b(doesn'?t|does not|didn'?t|did not|isn'?t|is not|not) (work|working|fixed|right|correct|show|load|"
    r"render|change|help)|\bstill (broken|failing|fails|not|doesn'?t|the same|seeing|getting|happening|there|"
    r"wrong|an? (error|issue|problem|bug))|\b(that'?s|this is|it'?s) (wrong|not right|incorrect|broken)|"
    r"\b(revert|undo|roll ?back) (that|this|it|the|your)|\byou broke\b|\b(broke|broken|crash(es|ed|ing)?)\b|"
    r"\b(error|exception|traceback)\b.{0,40}\b(when|after|on|now)\b|\bnot what i (asked|wanted|meant)\b"
)
_COMMIT_CMD = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def _lines(text) -> list[str]:
    return str(text or "").splitlines()


def _ask(text: str) -> str:
    return " ".join(redact(text).split())[:400]


def _note_ask(w: Work, at: float | None, raw: str) -> str:
    text = clean_user_text(raw)
    if not text or text.lstrip().startswith("<"):
        return ""
    if w.edited and _CORRECTION.search(text[:400]):
        w.events.append((at, "correction", _ask(text), ""))
    return _ask(text)


def _note_command(w: Work, at: float | None, cmd: str, out: str, failed: bool) -> None:
    if _COMMIT_CMD.search(cmd):
        w.events.append((at, "commit", redact(cmd)[:2000], redact(out)[:2000]))
    elif failed and w.edited:
        kind = "check" if _CHECK.search(cmd) else "command"
        w.events.append((at, kind, redact(cmd)[:500], redact(out)[-800:]))


def _patch(text: str, cwd: str | None, at: float | None, ask: str) -> list[Edit]:
    """The files an apply_patch patch changes (Codex, Cursor), with its added and removed lines."""
    out, cur = [], None
    for line in text.splitlines():
        if m := re.match(r"\*\*\* (Add|Update|Delete) File: (.+)", line):
            f = m.group(2).strip()
            f = f if os.path.isabs(f) or not cwd else os.path.join(cwd, f)
            cur = Edit(f, at, [], ask=ask)
            if m.group(1) != "Delete":
                out.append(cur)
        elif m := re.match(r"\*\*\* Move to: (.+)", line):
            if cur:
                f = m.group(1).strip()
                cur.path = f if os.path.isabs(f) or not cwd else os.path.join(cwd, f)
        elif line.startswith("*** "):
            cur = None if line.startswith("*** End") else cur
        elif cur and line.startswith("+"):
            cur.added.append(line[1:])
        elif cur and line.startswith("-"):
            cur.removed.append(line[1:])
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def read_claude(path: str) -> Work:
    w = Work()
    ask = ""
    pending: dict[str, tuple] = {}  # tool_use id -> (name, at, input)
    for f in [path, *sorted(glob.glob(os.path.join(path[:-6], "subagents", "*.jsonl")))]:
        with contextlib.suppress(OSError):
            for d in read_jsonl(f):
                ask = _claude_line(w, d, ask, pending, sub=f != path)
    return w


def _claude_line(w: Work, d: dict, ask: str, pending: dict, sub: bool) -> str:
    at = iso_ts(d.get("timestamp"))
    w.folder = w.folder or d.get("cwd")
    content = (d.get("message") or {}).get("content")
    if d.get("type") == "user":
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and not (sub or d.get("isMeta") or d.get("isSidechain")):
                ask = _note_ask(w, at, b.get("text", "")) or ask
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                name, t0, args = pending.pop(b["tool_use_id"])
                result = d.get("toolUseResult")
                if name == "Bash":
                    _note_command(
                        w, t0, str(args.get("command") or ""), _text(b.get("content")), bool(b.get("is_error"))
                    )
                elif name == "Write" and isinstance(result, dict):
                    _claude_write(w, args, t0, ask, result)
    elif d.get("type") == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use" or not isinstance(b.get("input"), dict):
                continue
            name, args = b.get("name"), b["input"]
            target = next((args[k] for k in _EDIT_KEYS if isinstance(args.get(k), str)), None)
            if name == "Edit" and target:
                w.edits.append(
                    Edit(target, at, _lines(args.get("new_string")), _lines(args.get("old_string")), ask=ask)
                )
            elif name == "MultiEdit" and target:
                for e in args.get("edits") or []:
                    if isinstance(e, dict):
                        w.edits.append(
                            Edit(target, at, _lines(e.get("new_string")), _lines(e.get("old_string")), ask=ask)
                        )
            elif name == "NotebookEdit" and target:
                w.edits.append(Edit(target, at, _lines(args.get("new_source")), ask=ask))
            elif name in ("Bash", "Write"):
                pending[b.get("id")] = (name, at, args)
            if name in ("Edit", "MultiEdit", "NotebookEdit"):
                w.edited = True
    return ask


def _claude_write(w: Work, args: dict, at: float | None, ask: str, result: dict) -> None:
    target = args.get("file_path")
    if not isinstance(target, str) or str(result.get("type")) not in ("create", "update"):
        return
    w.edited = True
    hunks = result.get("structuredPatch")
    if result.get("type") == "update" and isinstance(hunks, list) and hunks:
        lines = [x for h in hunks if isinstance(h, dict) for x in h.get("lines") or [] if isinstance(x, str)]
        added = [x[1:] for x in lines if x.startswith("+")]
        removed = [x[1:] for x in lines if x.startswith("-")]
        w.edits.append(Edit(target, at, added, removed, ask=ask))
    else:
        whole = result.get("type") == "update"  # an update with no patch: its new lines are unknown
        w.edits.append(Edit(target, at, _lines(args.get("content")), whole=whole, ask=ask))


def read_codex(path: str) -> Work:
    w = Work()
    ask = ""
    pending: dict[str, tuple] = {}  # call id -> (at, command)
    for d in read_jsonl(path):
        p = d.get("payload") or {}
        at = iso_ts(d.get("timestamp"))
        if d.get("type") == "session_meta":
            w.folder = w.folder or p.get("cwd")
            continue
        if d.get("type") != "response_item":
            continue
        kind = p.get("type")
        if kind == "message" and p.get("role") == "user":
            text = "\n".join(str(b.get("text", "")) for b in p.get("content") or [] if isinstance(b, dict))
            if not text.lstrip().startswith(("<", "# AGENTS.md")):
                ask = _note_ask(w, at, text) or ask
        elif kind in ("custom_tool_call", "function_call"):
            raw = str(p.get("input") if kind == "custom_tool_call" else p.get("arguments") or "")
            if "tools." in raw and kind == "custom_tool_call":  # code mode: a script calling tools
                calls = _js_calls(raw)
            else:
                args = {}
                if raw.lstrip().startswith("{"):
                    with contextlib.suppress(json.JSONDecodeError):
                        args = json.loads(raw)
                cmd = args.get("cmd") or args.get("command") or ""
                cmd = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
                patch = raw if "*** Begin Patch" in raw and not args else str(args.get("input") or cmd)
                calls = [("apply_patch", patch, args.get("workdir"))] if "*** Begin Patch" in patch else []
                calls += [("exec_command", cmd, args.get("workdir"))] if cmd and not calls else []
            cmds = []
            for name, text, workdir in calls:
                if name == "apply_patch":
                    edits = _patch(text, workdir or w.folder, at, ask)
                    w.edits += edits
                    w.edited = w.edited or bool(edits)
                else:
                    cmds.append(text)
            if cmds and p.get("call_id"):
                pending[p["call_id"]] = (at, "\n".join(cmds))
        elif kind in ("function_call_output", "custom_tool_call_output") and p.get("call_id") in pending:
            t0, cmd = pending.pop(p["call_id"])
            out = p.get("output")
            code = None
            if isinstance(out, list):
                out = "\n".join(str(b.get("text", "")) for b in out if isinstance(b, dict))
            elif isinstance(out, str) and out.lstrip().startswith("{"):
                with contextlib.suppress(json.JSONDecodeError, AttributeError):
                    o = json.loads(out)
                    code = (o.get("metadata") or {}).get("exit_code")
                    out = str(o.get("output") or "")
            out = str(out or "")
            codes = [int(c) for c in re.findall(r"Process exited with code (-?\d+)", out)]
            failed = bool(code) or any(codes) or out.startswith("Script failed")
            _note_command(w, t0, cmd, out, failed)
    return w


_JS_TOOL = re.compile(r"tools\.(apply_patch|exec_command)\(\s*")
_JS_KEY = re.compile(r"\b(cmd|workdir|input)\s*:\s*")


def _js_calls(src: str) -> list[tuple[str, str, str | None]]:
    """The apply_patch and exec_command calls in a Codex code-mode script, as (tool, patch or
    command, working folder), with their JavaScript string literals decoded."""
    out = []
    for m in _JS_TOOL.finditer(src):
        pos = m.end()
        if src[pos : pos + 1] in "\"'`":
            text, _ = _js_string(src, pos)
            out.append((m.group(1), text, None))
            continue
        if src[pos : pos + 1] != "{":
            continue
        fields, i = {}, pos + 1
        while (k := _JS_KEY.match(src, i)) or (i < len(src) and src[i] in " ,\n\t"):
            if not k:
                i += 1
                continue
            if src[k.end() : k.end() + 1] not in "\"'`":
                break
            fields[k.group(1)], i = _js_string(src, k.end())
        text = fields.get("cmd") or fields.get("input")
        if text:
            out.append((m.group(1), text, fields.get("workdir")))
    return out


def _js_string(src: str, start: int) -> tuple[str, int]:
    """Decode the JavaScript string literal at src[start]; returns it and the index after it."""
    q, i, buf = src[start], start + 1, []
    while i < len(src) and src[i] != q:
        if src[i] == "\\" and i + 1 < len(src):
            c = src[i + 1]
            if c == "u" and re.match(r"[0-9a-fA-F]{4}", src[i + 2 : i + 6]):
                buf.append(chr(int(src[i + 2 : i + 6], 16)))
                i += 6
                continue
            buf.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(c, c))
            i += 2
            continue
        buf.append(src[i])
        i += 1
    return "".join(buf), i + 1


# Cursor writes no time on transcript lines, but each message the person sends carries one.
_CURSOR_TS = re.compile(
    r"<timestamp>\w+, (\w{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) ([AP]M)(?: \(UTC([+-]\d{1,2})(?::?(\d{2}))?\))?"
)


def _cursor_ts(text: str) -> float | None:
    m = _CURSOR_TS.search(text)
    if not m:
        return None
    mon, day, year, hour, minute, half, off_h, off_m = m.groups()
    try:
        dt = datetime.strptime(
            f"{mon} {day} {year} {int(hour) % 12 + (12 if half == 'PM' else 0)}:{minute}", "%b %d %Y %H:%M"
        )
    except ValueError:
        return None
    if off_h is None:
        return dt.timestamp()
    sign = -1 if off_h.startswith("-") else 1
    offset = timedelta(hours=int(off_h), minutes=sign * int(off_m or 0))
    return dt.replace(tzinfo=timezone(offset)).timestamp()


def read_cursor(path: str) -> Work:
    w = Work()
    ask, at = "", None
    for d in read_jsonl(path):
        content = (d.get("message") or {}).get("content")
        if d.get("role") == "user":
            raw = _text(content)
            at = _cursor_ts(raw) or at
            ask = _note_ask(w, at, raw) or ask
            continue
        if d.get("role") != "assistant" or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, args = b.get("name"), b.get("input")
            if isinstance(args, str) and "*** Begin Patch" in args:
                edits = _patch(args, w.folder, at, ask)
                w.edits += edits
                w.edited = w.edited or bool(edits)
                continue
            if not isinstance(args, dict):
                continue
            target = next((args[k] for k in _EDIT_KEYS if isinstance(args.get(k), str)), None)
            if name == "StrReplace" and target:
                w.edits.append(
                    Edit(target, at, _lines(args.get("new_string")), _lines(args.get("old_string")), ask=ask)
                )
                w.edited = True
            elif name == "Write" and target:
                w.edits.append(Edit(target, at, _lines(args.get("contents")), whole=True, ask=ask))
                w.edited = True
            elif name == "EditNotebook" and target:
                w.edits.append(
                    Edit(target, at, _lines(args.get("new_string")), _lines(args.get("old_string")), ask=ask)
                )
                w.edited = True
            elif name == "Shell" and _COMMIT_CMD.search(str(args.get("command") or "")):
                _note_command(w, at, str(args["command"]), "", False)  # Cursor keeps no command output
    return w


# --- lines -------------------------------------------------------------------------


def _norm(line: str) -> bytes | None:
    """A line as evidence: whitespace collapsed, hashed. Short and punctuation-only lines say
    nothing about who wrote them, so they are dropped."""
    s = " ".join(line.split())
    if len(s) < 8 or sum(c.isalnum() for c in s) < 4:
        return None
    return hashlib.blake2b(s.encode(), digest_size=8).digest()


def _hashes(lines) -> set[bytes]:
    return {h for x in lines if (h := _norm(x))}


def _pack(hs: set[bytes]) -> bytes:
    return b"".join(sorted(hs))


def _unpack(blob: bytes | None) -> set[bytes]:
    return {blob[i : i + 8] for i in range(0, len(blob or b""), 8)}


# --- git, read-only -------------------------------------------------------------------

_GIT = ("-c", "core.quotePath=false", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false")
_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def _git(where: str, *args: str, stdin: str | None = None, timeout: int = 120, gitdir: bool = False) -> str | None:
    """Run a read-only git command in a checkout (or against a git dir); None if it fails."""
    loc = ("--git-dir", where) if gitdir else ("-C", where)
    try:
        r = subprocess.run(
            ["git", *_GIT, *loc, *args],
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _top(path: str, cache: dict) -> str | None:
    from .status import _top as top

    return top(path, cache)


def _gitdir(top: str) -> str | None:
    from .status import _repo_dir

    return _repo_dir(top)


def place(path: str, folder: str | None, cache: dict) -> tuple[str, str, str] | None:
    """(repo key, path in the repo, checkout) for a file an agent edited; None outside a repo
    or in a scratch folder. A file in a worktree that has since been deleted still finds its
    repo: the walk up lands in the main checkout, and the worktree's folders are dropped."""
    full = os.path.normpath(path if os.path.isabs(path) or not folder else os.path.join(folder, path))
    if os.path.exists(os.path.dirname(full)):
        full = os.path.realpath(full)
    top = _top(full, cache)
    if not top:
        return None
    rel = os.path.relpath(full, top)
    if rel.startswith(".."):
        return None
    if not os.path.exists(os.path.dirname(full)) and (m := re.match(r"(?:.*/)?worktrees/[^/]+/(.+)", rel)):
        rel = m.group(1)
    key = cache.get(("key", top))
    if key is None:
        project = resolve(top)
        key = cache[("key", top)] = project.key if project else ""
    return (key, rel, top) if key else None


# --- the tables ---------------------------------------------------------------------


def enabled() -> bool:
    return bool(config.load().get("links", {}).get("enabled", True))


def ensure(con) -> None:
    con.execute("create table if not exists meta (k text primary key, v text)")
    row = con.execute("select v from meta where k='links_schema'").fetchone()
    if not row or row[0] != SCHEMA_VERSION:
        for t in TABLES:
            con.execute(f"drop table if exists {t}")
        con.execute("insert or replace into meta values ('links_schema', ?)", (SCHEMA_VERSION,))
    con.executescript(SCHEMA)


def clear(con) -> None:
    """Forget every link, for when linking is turned off."""
    for t in TABLES:
        con.execute(f"drop table if exists {t}")
    con.execute("delete from meta where k='links_schema'")
    con.commit()


READERS = {"claude": read_claude, "codex": read_codex, "cursor": read_cursor}


def _fingerprint(source: str) -> str | None:
    try:
        st = os.stat(source)
    except OSError:
        return None
    fp = f"{st.st_mtime_ns}:{st.st_size}"
    if source.endswith(".jsonl"):  # Claude Code keeps a chat's subagents beside it
        subs = glob.glob(os.path.join(source[:-6], "subagents", "*.jsonl"))
        if subs:
            fp += f":{len(subs)}:{max(os.stat(s).st_mtime_ns for s in subs)}"
    return fp


def read_chats(con, log=None) -> set[str]:
    """Read the edits of every chat that changed since the last run. Returns the repos touched."""
    seen = dict(con.execute("select session_id, fingerprint from link_chats"))
    rows = con.execute("select id, agent, source, project, started_at, updated_at from sessions").fetchall()
    cache: dict = {}
    repos: set[str] = set()
    done = 0
    for r in rows:
        reader = READERS.get(r["agent"])
        if not reader or not r["source"] or "#" in r["source"]:
            continue  # Cursor's older chats live in its database and keep no tool calls
        fp = _fingerprint(r["source"])
        if fp is None or seen.get(r["id"]) == fp:
            continue
        try:
            work = reader(r["source"])
        except Exception:  # one unreadable chat must not stop the run
            continue
        repos |= _store_chat(con, r, work, cache)
        con.execute("insert or replace into link_chats values (?,?,?)", (r["id"], fp, time.time()))
        done += 1
        if done % 200 == 0:
            con.commit()
            if log:
                log(f"links: read {done} chats…")
    con.commit()
    return repos


def _store_chat(con, r, work: Work, cache: dict) -> set[str]:
    sid = r["id"]
    folder = work.folder or r["project"]
    old = {row[0] for row in con.execute("select distinct repo from link_edits where session_id=?", (sid,))}
    con.execute("delete from link_edits where session_id=?", (sid,))
    con.execute("delete from link_events where session_id=?", (sid,))
    files: dict[tuple[str, str], dict] = {}
    for e in work.edits:
        where = place(e.path, folder, cache)
        if not where:
            continue
        repo, rel, top = where
        con.execute("insert or ignore into link_checkouts values (?,?)", (repo, top))
        at = e.at or r["started_at"] or r["updated_at"]
        f = files.setdefault((repo, rel), {"strong": set(), "weak": set(), "first": at, "last": at, "ask": e.ask})
        f["first_ask"] = f.get("first_ask") or e.ask
        added, removed = _hashes(e.added), _hashes(e.removed)
        if e.whole:  # the file as a whole: what stays of the chat's own lines, the rest is weak
            f["strong"] &= added
            f["weak"] = added - f["strong"]
        else:
            f["strong"] = (f["strong"] - (removed - added)) | added
            f["weak"] -= removed - added
        if at:
            f["first"] = min(x for x in (f["first"], at) if x)
            f["last"] = max(x for x in (f["last"], at) if x)
        f["ask"] = e.ask or f["ask"]
    for (repo, rel), f in files.items():
        if not f["strong"] and not f["weak"]:
            continue
        con.execute(
            "insert into link_edits values (?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                repo,
                rel,
                f["first"],
                f["last"] or r["updated_at"],
                _pack(f["strong"]),
                _pack(f["weak"]),
                len(f["strong"]) + len(f["weak"]),
                f["first_ask"],
                f["ask"],
            ),
        )
    con.executemany(
        "insert into link_events values (?,?,?,?,?,?)",
        [(sid, i, at or r["updated_at"], kind, text, detail) for i, (at, kind, text, detail) in enumerate(work.events)],
    )
    repos = old | {repo for repo, _ in files}
    # A chat that ran `git commit` in its folder may have committed edits made by hand.
    if folder and any(kind == "commit" for _, kind, _, _ in work.events) and (home := place(folder, None, cache)):
        repos.add(home[0])
        con.execute("insert or ignore into link_checkouts values (?,?)", (home[0], home[2]))
    con.executemany("insert or replace into link_repos values (?, 1, null)", [(x,) for x in repos])
    return repos


# --- commits -------------------------------------------------------------------------

_FORMAT = "%x1e%H%x1f%P%x1f%ct%x1f%at%x1f%an%x1f%s"


def scan(con, repo: str) -> int:
    """Record every commit in the repo's clones since its chats began; returns how many are new."""
    row = con.execute("select min(first_at) from link_edits where repo=?", (repo,)).fetchone()
    if not row or not row[0]:
        return 0
    since = int(row[0] - 2 * 86400)
    known = {r[0] for r in con.execute("select sha from link_commits where repo=?", (repo,))}
    new = 0
    for gitdir in _gitdirs(con, repo):
        out = _git(gitdir, "rev-list", "--exclude=refs/stash", "--all", "--no-merges", f"--since={since}", gitdir=True)
        fresh = [s for s in (out or "").split() if s not in known]
        if not fresh:
            continue
        out = _git(
            gitdir,
            "log",
            "--no-walk=unsorted",
            "--stdin",
            "--name-only",
            f"--format={_FORMAT}",
            stdin="\n".join(fresh),
            gitdir=True,
        )
        for block in (out or "").split("\x1e")[1:]:
            head, _, names = block.partition("\n")
            parts = head.split("\x1f")
            if len(parts) < 6:
                continue
            sha, parents, ct, at, author, subject = parts[:6]
            con.execute(
                "insert or ignore into link_commits(repo, sha, parent, at, authored, author, subject, gitdir) "
                "values (?,?,?,?,?,?,?,?)",
                (repo, sha, (parents.split() or [None])[0], float(ct), float(at), author, subject, gitdir),
            )
            con.executemany(
                "insert or ignore into link_commit_files(repo, sha, path) values (?,?,?)",
                [(repo, sha, n) for n in names.splitlines() if n.strip()],
            )
            known.add(sha)
            new += 1
    con.commit()
    return new


def _gitdirs(con, repo: str) -> list[str]:
    """Every clone of the repo on disk that a chat worked in; worktrees share their clone's."""
    out = []
    for (top,) in con.execute("select top from link_checkouts where repo=?", (repo,)).fetchall():
        if os.path.isdir(top) and (g := _gitdir(top)) and os.path.isdir(g) and g not in out:
            out.append(g)
    return out


def _diff(con, repo: str, shas: list[str]) -> None:
    """Read the lines each commit adds, for commits not read yet."""
    by_dir: dict[str, list[str]] = {}
    for sha in shas:
        row = con.execute("select gitdir, diffed from link_commits where repo=? and sha=?", (repo, sha)).fetchone()
        if row and not row["diffed"]:
            by_dir.setdefault(row["gitdir"], []).append(sha)
    for gitdir, todo in by_dir.items():
        for i in range(0, len(todo), 200):
            batch = todo[i : i + 200]
            out = _git(
                gitdir,
                "log",
                "--no-walk=unsorted",
                "--stdin",
                "-p",
                "-U0",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--format=%x1e%H",
                stdin="\n".join(batch),
                gitdir=True,
                timeout=300,
            )
            if out is None:
                continue
            for block in out.split("\x1e")[1:]:
                sha, files = _parse_patch(block)
                total = 0
                for path, lines in files.items():
                    hs = _hashes(lines) if len(lines) <= MAX_FILE_LINES and not _GENERATED.search(path) else set()
                    total += len(hs)
                    con.execute(
                        "insert or replace into link_commit_files values (?,?,?,?,?)",
                        (repo, sha, path, _pack(hs), len(hs)),
                    )
                con.execute("update link_commits set diffed=1, n=? where repo=? and sha=?", (total, repo, sha))
    con.commit()


def _parse_patch(block: str) -> tuple[str, dict[str, list[str]]]:
    sha, _, body = block.partition("\n")
    files: dict[str, list[str]] = {}
    cur, header = None, False
    for line in body.split("\n"):
        if line.startswith("diff --git "):
            cur, header = None, True
        elif header and line.startswith("+++ "):
            name = line[4:]
            cur = None if name == "/dev/null" else name[2:] if name.startswith("b/") else name
            header = False
            if cur is not None:
                files.setdefault(cur, [])
        elif line.startswith("@@"):
            header = False
        elif cur is not None and not header and line.startswith("+"):
            files[cur].append(line[1:])
    return sha.strip(), files


# --- linking ---------------------------------------------------------------------------


@dataclass
class _Claim:
    session: str
    path: str
    first: float
    last: float
    strong: set[bytes]
    weak: set[bytes]
    ask: str


def link(con, repo: str) -> int:
    """Work out the repo's links again from its chats' edits and its commits."""
    claims: dict[str, list[_Claim]] = {}
    for r in con.execute("select * from link_edits where repo=?", (repo,)):
        claims.setdefault(r["path"], []).append(
            _Claim(
                r["session_id"],
                r["path"],
                r["first_at"],
                r["last_at"],
                _unpack(r["lines"]),
                _unpack(r["weak"]),
                r["ask"],
            )
        )
    commits = {r["sha"]: r for r in con.execute("select * from link_commits where repo=?", (repo,))}
    touching: dict[str, list[str]] = {}
    for r in con.execute("select sha, path from link_commit_files where repo=?", (repo,)):
        if r["path"] in claims and r["sha"] in commits:
            touching.setdefault(r["sha"], []).append(r["path"])

    def admits(c: _Claim, at: float) -> bool:
        return c.first - SLACK <= at <= c.last + WINDOW

    candidates = [s for s, ps in touching.items() if any(admits(c, commits[s]["at"]) for p in ps for c in claims[p])]
    _diff(con, repo, candidates)
    found: dict[tuple[str, str], dict] = {}
    for sha in candidates:
        at = commits[sha]["at"]
        rows = {
            r["path"]: r for r in con.execute("select * from link_commit_files where repo=? and sha=?", (repo, sha))
        }
        total = sum(r["n"] or 0 for r in rows.values())
        for path in touching[sha]:
            added = _unpack(rows[path]["lines"]) if path in rows else set()
            if not added:
                continue
            # The chat that wrote a line last before the commit gets it. Lines a chat only
            # wrote as part of a whole file count after every chat's own edits.
            live = sorted((c for c in claims[path] if admits(c, at)), key=lambda c: -c.last)
            taken: set[bytes] = set()
            for tier in ("strong", "weak"):
                for c in live:
                    got = (getattr(c, tier) & added) - taken
                    taken |= got
                    x = found.setdefault((sha, c.session), {"lines": 0, "chat": 0, "files": [], "ask": "", "last": 0})
                    x["total"] = total
                    x["lines"] += len(got)
                    if tier == "strong":
                        x["chat"] += len(c.strong | c.weak)
                        if c.last >= x["last"]:
                            x["last"], x["ask"] = c.last, c.ask
                    if got and path not in x["files"]:
                        x["files"].append(path)
    ran = _ran(con, repo, commits)
    rows = []
    for (sha, sid), x in found.items():
        share = x["lines"] / x["total"] if x["total"] else 0.0
        kept = x["lines"] / x["chat"] if x["chat"] else 0.0
        made = (sha, sid) in ran
        if not made and not (x["lines"] >= MIN_LINES and (share >= MIN_SHARE or kept >= MIN_KEPT)):
            continue
        score = 1.0 if made else round(min(1.0, max(share, kept)) * min(1.0, x["lines"] / 10), 3)
        how = "ran" if made else "wrote"
        rows.append((repo, sha, sid, score, how, x["lines"], x["total"], x["chat"], json.dumps(x["files"]), x["ask"]))
    for sha, sid in ran - set(found):
        rows.append((repo, sha, sid, 1.0, "ran", 0, commits[sha]["n"] or 0, 0, "[]", ""))
    con.execute("delete from commit_links where repo=?", (repo,))
    con.executemany("insert or replace into commit_links values (?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("insert or replace into link_repos values (?, 0, ?)", (repo, time.time()))
    con.commit()
    return len(rows)


def _ran(con, repo: str, commits: dict) -> set[tuple[str, str]]:
    """Commits a chat made itself: git printed the sha, or the command carried the subject."""
    out: set[tuple[str, str]] = set()
    if not commits:
        return out
    by_prefix = {sha[:7]: sha for sha in commits}
    here = {r[0] for r in con.execute("select distinct session_id from link_edits where repo=?", (repo,))}
    here |= {r[0] for r in con.execute("select id from sessions where project_key=?", (repo,))}
    subjects = [(sha, " ".join((c["subject"] or "").split()), c["at"]) for sha, c in commits.items()]
    subjects = [x for x in subjects if len(x[1]) >= 12]
    for r in con.execute("select session_id, at, text, detail from link_events where kind='commit'"):
        at = r["at"] or 0
        for tok in set(_SHA.findall(r["detail"] or "")):
            sha = by_prefix.get(tok[:7])
            # Made by this command, so not before it started: output often lists older commits too.
            if sha and sha.startswith(tok) and at - 120 <= commits[sha]["at"] <= at + 3600:
                out.add((sha, r["session_id"]))
        if r["session_id"] in here:
            cmd = " ".join((r["text"] or "").split())
            for sha, subject, when in subjects:
                if at - SLACK <= when <= at + 12 * 3600 and subject in cmd:
                    out.add((sha, r["session_id"]))
    return out


def update(con, repo: str | None = None, log=None) -> dict:
    """Bring the links up to date: read the chats that changed, record new commits, and link
    again wherever anything moved. With repo, only that repo is scanned. No model calls."""
    if not enabled():
        clear(con)
        return {"enabled": False}
    ensure(con)
    started = time.time()
    read_chats(con, log)
    repos = [repo] if repo else [r[0] for r in con.execute("select distinct repo from link_edits")]
    dirty = {r[0] for r in con.execute("select repo from link_repos where dirty != 0")}
    done = set(r[0] for r in con.execute("select repo from link_repos where linked_at is not null"))
    linked = 0
    for name in repos:
        if scan(con, name) or name in dirty or name not in done:
            linked += link(con, name)
            if log:
                log(f"links: {name}: {linked} links so far")
    return {"repos": len(repos), "links": linked, "seconds": round(time.time() - started, 1)}


def stats(con) -> dict:
    ensure(con)

    def one(sql: str) -> int:
        return con.execute(sql).fetchone()[0]

    return {
        "chats_read": one("select count(*) from link_chats"),
        "chats_with_edits": one("select count(distinct session_id) from link_edits"),
        "repos": one("select count(distinct repo) from link_edits"),
        "commits_seen": one("select count(*) from link_commits"),
        "commits_linked": one("select count(*) from (select distinct repo, sha from commit_links)"),
        "made_by_a_chat": one("select count(*) from (select distinct repo, sha from commit_links where how='ran')"),
        "links": one("select count(*) from commit_links"),
    }


# --- recall why ------------------------------------------------------------------------


def _chat(con, sid: str) -> dict:
    r = con.execute(
        "select id, agent, title, first_prompt, started_at, updated_at from sessions where id=?", (sid,)
    ).fetchone()
    if not r:
        return {"chat_id": sid}
    return {
        "chat_id": sid,
        "agent": r["agent"],
        "title": " ".join((r["title"] or r["first_prompt"] or "").split())[:120],
        "date": datetime.fromtimestamp(r["updated_at"] or r["started_at"] or 0).strftime("%Y-%m-%d"),
    }


def chats_for(con, repo: str, sha: str) -> list[dict]:
    rows = con.execute(
        "select * from commit_links where repo=? and sha=? order by how='ran' desc, score desc, lines desc", (repo, sha)
    ).fetchall()
    out = []
    for r in rows:
        c = _chat(con, r["session_id"])
        c.update(
            how=r["how"],
            lines=r["lines"],
            of=r["commit_lines"],
            score=r["score"],
            asked=r["ask"] or None,
        )
        out.append(c)
    return out


def _commit_meta(con, repo: str, sha: str, top: str) -> dict:
    r = con.execute("select * from link_commits where repo=? and sha=?", (repo, sha)).fetchone()
    if r:
        at, author, subject, parent = r["at"], r["author"], r["subject"], r["parent"]
    else:
        out = (_git(top, "show", "-s", "--format=%ct%x1f%an%x1f%s%x1f%P", sha) or "").strip().split("\x1f")
        if len(out) < 4:
            return {"sha": sha}
        at, author, subject, parent = float(out[0]), out[1], out[2], (out[3].split() or [None])[0]
    return {
        "sha": sha,
        "date": datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M"),
        "author": author,
        "subject": subject,
        "parent": parent,
    }


def why(con, target: str | None = None, commit: str | None = None, cwd: str | None = None, top_n: int = 5) -> dict:
    """The chats behind a file, a line (path:line or path:start-end) or a commit: git blame
    finds the commits, and the links name the chats. Links are brought up to date first."""
    cwd = cwd or os.getcwd()
    path, lines = target, None
    m = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", target or "")
    if m and not os.path.exists(os.path.join(cwd, os.path.expanduser(target))):
        path, lines = m.group(1), (int(m.group(2)), int(m.group(3) or m.group(2)))
    where = os.path.realpath(os.path.join(cwd, os.path.expanduser(path))) if path else os.path.realpath(cwd)
    top = (
        _git(where if os.path.isdir(where) else os.path.dirname(where), "rev-parse", "--show-toplevel") or ""
    ).strip()
    if not top:
        return {"error": f"{path or cwd} is not in a git repo"}
    project = resolve(top)
    if not project:
        return {"error": f"{top} is a scratch folder: recall links no chats there"}
    if not enabled():
        return {"error": "commit links are off: set [links] enabled = true in recall's config.toml"}
    ensure(con)
    con.execute("insert or ignore into link_checkouts values (?,?)", (project.key, top))
    update(con, project.key)
    out: dict = {"repo": project.key}
    if commit:
        sha = (_git(top, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}") or "").strip()
        if not sha:
            return {"error": f"no commit {commit!r} in {top}"}
        out["commits"] = [{**_commit_meta(con, project.key, sha, top), "chats": chats_for(con, project.key, sha)}]
        return out
    rel = os.path.relpath(where, top)
    out["file"] = rel + (f":{lines[0]}" + (f"-{lines[1]}" if lines[1] != lines[0] else "") if lines else "")
    blame = _git(top, "blame", "--porcelain", *(["-L", f"{lines[0]},{lines[1]}"] if lines else []), "--", rel)
    if blame is None:
        return {"error": f"git blame failed for {rel}: is it tracked, and is the line range inside the file?"}
    texts: dict[str, list[str]] = {}
    sha = None
    for line in blame.split("\n"):
        if re.match(r"^[0-9a-f]{40} ", line):
            sha = line.split()[0]
        elif line.startswith("\t") and sha:
            texts.setdefault(sha, []).append(line[1:])
    found = []
    for sha, said in sorted(texts.items(), key=lambda x: -len(x[1]))[:top_n]:
        if sha == _EMPTY:
            meta, chats = {"sha": None, "subject": "not committed yet"}, _writers(con, project.key, rel, said)
        else:
            meta, chats = _commit_meta(con, project.key, sha, top), chats_for(con, project.key, sha)
            if not chats and lines:  # no confident link: the chats that wrote these very lines, if any
                chats = [dict(c, how="wrote the line") for c in _writers(con, project.key, rel, said)]
        if lines and lines[1] - lines[0] < 10:  # for a few lines, the request that led to them
            for c in chats[:3]:
                c.update(_line_ask(con, c["chat_id"], rel, said) or {})
        found.append({**meta, "lines": len(said), "chats": chats})
    out["commits"] = found
    out["blamed_lines"] = sum(len(x) for x in texts.values())
    return out


def _writers(con, repo: str, rel: str, lines: list[str]) -> list[dict]:
    """Chats that wrote any of these lines into the file, latest first."""
    want = _hashes(lines)
    out = []
    for r in con.execute(
        "select session_id, lines, weak, ask from link_edits where repo=? and path=? order by last_at desc", (repo, rel)
    ):
        if want & (_unpack(r["lines"]) | _unpack(r["weak"])):
            out.append(dict(_chat(con, r["session_id"]), how="wrote", asked=r["ask"] or None))
    return out[:5]


def _line_ask(con, sid: str, rel: str, lines: list[str]) -> dict | None:
    """What the person asked right before the chat wrote these lines, read again from its
    transcript (the index keeps one request per file)."""
    r = con.execute("select agent, source from sessions where id=?", (sid,)).fetchone()
    reader = READERS.get(r["agent"]) if r else None
    if not reader or not r["source"] or not os.path.exists(r["source"]):
        return None
    want = _hashes(lines)
    with contextlib.suppress(Exception):
        for e in reversed(reader(r["source"]).edits):
            if (e.path == rel or e.path.endswith("/" + rel)) and want & _hashes(e.added) and e.ask:
                return {"asked": e.ask, "asked_at": e.at}
    return None


def why_rows(con, file: str | None, commit: str | None) -> list[dict]:
    """`why` for an agent: one row per (commit, chat), or one row with an error or a note."""
    if commit:
        base = os.path.expanduser(re.sub(r":\d+(-\d+)?$", "", file or "")) or os.getcwd()
        result = why(con, None, commit, cwd=base if os.path.isdir(base) else os.path.dirname(base))
    else:
        result = why(con, file)
    if result.get("error"):
        return [{"error": result["error"]}]
    rows = []
    for c in result.get("commits") or []:
        head = {
            "sha": c.get("sha"),
            "committed": c.get("date"),
            "subject": c.get("subject"),
            "lines_here": c.get("lines"),
        }
        for x in c.get("chats") or [{"note": "no chat linked: written by hand, or by an agent recall does not read"}]:
            row = {**head, **{k: v for k, v in x.items() if k not in ("score", "asked_at", "date")}}
            if x.get("date"):
                row["chat_date"] = x["date"]
            if row.get("asked"):
                row["asked"] = row["asked"][:200]
            rows.append(row)
    return rows


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}


def render(result: dict) -> str:
    """`recall why` as text: each commit, then the chats behind it."""
    if result.get("error"):
        return result["error"]
    repo = re.sub(r"^(git|path):", "", result["repo"])
    out = [f"{result.get('file') or 'commit'}  ({repo})"]
    for c in result.get("commits") or []:
        here = f"  ({c['lines']} line{'s' if c['lines'] != 1 else ''} here)" if result.get("file") else ""
        out.append(
            f"\n{(c.get('sha') or '')[:8] or 'working copy'}  {(c.get('date') or '')[:10]}  "
            f"{c.get('subject', '')}{here}"
        )
        chats = c.get("chats") or []
        if not chats:
            out.append(
                "  no chat linked: written by hand, through a shell command, or by an agent recall does not read"
            )
        elif c.get("sha") and not any(x.get("how") == "ran" for x in chats):
            out.append("  committed outside these chats: by hand, or by a tool recall does not read")
        for x in chats:
            agent = AGENT_NAMES.get(x.get("agent"), x.get("agent") or "?")
            out.append(f"  {agent} · {x.get('date', '?')} · {x.get('title') or '(untitled)'}")
            how = x.get("how")
            wrote = f"wrote {x['lines']} of its {x['of']} lines" if x.get("lines") and x.get("of") else ""
            what = {
                "ran": "made this commit" + (f" and {wrote}" if wrote else ""),
                "wrote": wrote or "wrote these lines",
                "wrote the line": "wrote these lines (the commit itself is not linked with confidence)",
            }.get(how, how or "")
            out.append(f"    {what} · {x['chat_id']}")
            if x.get("asked"):
                out.append(f'    asked: "{x["asked"][:160]}"')
    return "\n".join(out)


# --- failure, then fix: the start of a personal regression gym ----------------------------


FAILURES = "('check', 'command', 'correction')"


def episodes(con, repo: str | None = None, days: int = 7) -> list[dict]:
    """Chats where something failed, then a commit fixed it.

    A failure is a check or command that exited non-zero after the chat started editing, a
    message from the person saying the work is wrong or still broken, or a revert of an
    earlier commit to the same files. The fix is a commit the chat is linked to, made after
    the chat edited those files again; or the failure was in an earlier chat (within `days`)
    on the same files whose work never reached a commit. Each episode gives the repo, the
    parent commit (where to start from), the person's request, the failure and the fixing
    commit. Rough heuristics, with no check that the failure and the fix are about the same
    thing: see the precision measured in the PR that added this.
    """
    ensure(con)
    sql = "select * from commit_links where files != '[]'" + (" and repo=?" if repo else "")
    out, seen = [], set()
    for lk in con.execute(sql + " order by repo, sha", (repo,) if repo else ()).fetchall():
        if (lk["repo"], lk["sha"]) in seen:
            continue
        files = set(json.loads(lk["files"]))
        c = con.execute("select * from link_commits where repo=? and sha=?", (lk["repo"], lk["sha"])).fetchone()
        q = ",".join("?" * len(files))
        last = con.execute(
            f"select max(last_at) from link_edits where session_id=? and repo=? and path in ({q})",
            (lk["session_id"], lk["repo"], *files),
        ).fetchone()[0]
        if not c or not last:
            continue
        sid, source = lk["session_id"], lk["session_id"]
        # A failure in the fixing chat, before its last edit to the files: something followed it.
        events = con.execute(
            f"select * from link_events where session_id=? and kind in {FAILURES} and at <= ? order by n",
            (sid, min(last, c["at"])),
        ).fetchall()
        for prev in [] if events else _earlier(con, lk["repo"], files, sid, c["at"], days):
            events = con.execute(
                f"select * from link_events where session_id=? and kind in {FAILURES} order by n", (prev,)
            ).fetchall()
            if events:
                source = prev
                break
        failure = dict(events[-1]) if events else _reverted(con, lk["repo"], files, c["at"], days)
        if not failure:
            continue
        seen.add((lk["repo"], lk["sha"]))
        out.append(
            {
                "repo": lk["repo"],
                "parent": c["parent"],
                "fix_commit": c["sha"],
                "fix_subject": c["subject"],
                "fix_chat": sid,
                "files": sorted(files),
                "prompt": _prompt(con, source, lk["repo"], files) or lk["ask"],
                "failure": {
                    "chat": failure.get("session_id", source),
                    **{k: failure.get(k) for k in ("kind", "text", "detail", "at")},
                },
                "same_chat": failure.get("session_id", source) == sid,
            }
        )
    return out


def _reverted(con, repo: str, files: set[str], at: float, days: int) -> dict | None:
    """A revert of an earlier commit to these files, in the days before `at`."""
    q = ",".join("?" * len(files))
    row = con.execute(
        f"select c.sha, c.subject, c.at from link_commits c join link_commit_files f on f.repo=c.repo and f.sha=c.sha "
        f"where c.repo=? and f.path in ({q}) and c.subject like 'Revert %' and c.at < ? and c.at > ? "
        "order by c.at desc limit 1",
        (repo, *files, at, at - days * 86400),
    ).fetchone()
    return (
        {"session_id": None, "kind": "revert", "text": row["subject"], "detail": row["sha"], "at": row["at"]}
        if row
        else None
    )


def _earlier(con, repo: str, files: set[str], sid: str, at: float, days: int) -> list[str]:
    q = ",".join("?" * len(files))
    rows = con.execute(
        f"select session_id, max(last_at) last from link_edits where repo=? and path in ({q}) and session_id != ? "
        "group by session_id having last < ? and last > ? order by last desc",
        (repo, *files, sid, at, at - days * 86400),
    ).fetchall()
    linked = {r[0] for r in con.execute("select session_id from commit_links where repo=?", (repo,))}
    return [r["session_id"] for r in rows if r["session_id"] not in linked]


def _prompt(con, sid: str, repo: str, files: set[str]) -> str | None:
    q = ",".join("?" * len(files))
    row = con.execute(
        f"select first_ask from link_edits where session_id=? and repo=? and path in ({q}) and first_ask != '' "
        "order by first_at limit 1",
        (sid, repo, *files),
    ).fetchone()
    return row["first_ask"] if row else None
