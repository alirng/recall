"""What a chat did to code: the files its agent edited and the lines it left in them, what
the person asked before each edit, the `git commit` commands it ran, and what failed along
the way (a check or command exiting non-zero, the person saying the work is wrong).

Read straight from each agent's transcript (read-only), from the same tool calls status.py
reads: Edit, MultiEdit, Write and NotebookEdit (Claude Code), StrReplace, Write and
ApplyPatch (Cursor), apply_patch and exec_command (Codex, including code mode). A file
changed only through a shell command is not seen.
"""

import contextlib
import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .redact import redact
from .sources import iso_ts, read_jsonl
from .transcript import clean_user_text


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
# A command that makes commits (commit, revert, cherry-pick) after any of git's global options:
# not `git log` in a folder named commit-x.
_COMMIT_CMD = re.compile(
    r"(?:^|[\s;&|(`])git(?:\s+(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|-c\s+\S+|--[\w-]+(?:=\S+)?))*"
    r"\s+(?:commit|revert|cherry-pick)(?=\s|$|[;&|)])"
)


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
    elif failed and w.edited and (_CHECK.search(cmd) or not _only_looking(cmd)):
        kind = "check" if _CHECK.search(cmd) else "command"
        w.events.append((at, kind, redact(cmd)[:500], redact(out)[-800:]))


# Commands that only look around. grep finding nothing, or ls on a missing file, exits
# non-zero without anything being wrong with the code.
_LOOKING = frozenset(
    "cd ls cat bat head tail grep rg ag fd find wc pwd echo printf sed awk sort uniq cut tr test [ which "
    "type command stat file du tree sleep true false diff jq nl less more open realpath dirname basename".split()
)
_GIT_LOOKING = frozenset("status log diff show branch rev-parse ls-files grep blame remote config".split())


def _only_looking(cmd: str) -> bool:
    """Whether every program a shell command runs only reads or searches."""
    for part in re.split(r"&&|\|\||[;|\n]", cmd):
        words = [x for x in part.split() if not re.match(r"^\w+=", x)]
        if not words:
            continue
        prog = os.path.basename(words[0].strip("(`"))
        if prog == "git":
            sub = next((x for x in words[1:] if not x.startswith("-") and "/" not in x), "")
            if sub not in _GIT_LOOKING:
                return False
        elif prog not in _LOOKING:
            return False
    return True


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


READERS = {"claude": read_claude, "codex": read_codex, "cursor": read_cursor}
