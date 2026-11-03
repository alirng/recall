"""GitHub Copilot Chat inside VS Code, Code Insiders and VSCodium (`roots.OTHERS['vscode']` lists each editor's
User folder). Positron Assistant shares the format but is not read.

Three stores, all per workspace, with a `workspace.json` beside the workspace folder that names it
(`{"folder": "file:///..."}`):
  `workspaceStorage/<hash>/chatSessions/<id>.jsonl`  a mutation log: line 1 (`kind` 0) is the whole session,
      later lines set (1), push onto (2) or delete (3) a value at the path `k`. It is replayed from the start,
      because VS Code rewrites the file when it compacts it. Before VS Code 1.109 the file was one `.json`
      object (the same state); a `.json` beside a `.jsonl` of the same name is the stale copy and is skipped.
  `globalStorage/emptyWindowChatSessions/<id>.jsonl`  chats of a window with no folder open.
  `workspaceStorage/<hash>/GitHub.copilot-chat/transcripts/<id>.jsonl`  the extension's own event log
      (`user.message`, `assistant.message`, `tool.execution_*`), the only store a newer extension writes.
      A transcript whose id also has a `chatSessions` file is skipped, and one with no turn of a person's in it
      is a headless agent run.

A person's turn is `message.text`; the reply is the `value` of its markdown parts (thinking, progress text,
edit parts and the compaction summary are not text anyone typed or the agent said). A terminal tool call is
a failed call when its recorded exit code is not 0.

Format knowledge: deja-vu docs/registry/copilot-chat.md and internal/sources/copilot_chat.go,
copilot_agent.go (MIT); agentsview internal/parser/vscode_copilot.go (MIT).
"""

import contextlib
import glob
import json
import os
import re
from urllib.parse import unquote, urlparse

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: VS Code Copilot Chat"

KEY = "vscode"
# Agent mode writes an edit as an empty code fence around parts that carry no text.
_EMPTY_FENCE = re.compile(r"(?m)^[ \t]*```[\w+-]*[ \t]*\n\s*```[ \t]*$\n?")
_DRIVE = re.compile(r"^/[A-Za-z]:")


def _stores() -> list[tuple[str, str]]:
    """(kind, path) of each chat file: "log" is a chatSessions file, "events" an extension transcript."""
    found: list[tuple[str, str]] = []
    for user in roots.other_dirs(KEY):
        for base in (user, *glob.glob(os.path.join(glob.escape(user), "profiles", "*"))):
            b = glob.escape(base)
            for pattern in (
                os.path.join(b, "workspaceStorage", "*", "chatSessions", "*.json*"),
                os.path.join(b, "globalStorage", "emptyWindowChatSessions", "*.json*"),
            ):
                found += [("log", p) for p in glob.glob(pattern) if p.endswith((".json", ".jsonl"))]
            pattern = os.path.join(b, "workspaceStorage", "*", "GitHub.copilot-chat", "transcripts", "*.jsonl")
            found += [("events", p) for p in glob.glob(pattern)]
    logs = {p for kind, p in found if kind == "log"}
    logged = {os.path.splitext(os.path.basename(p))[0] for p in logs}
    return sorted(
        (kind, p)
        for kind, p in found
        if not (kind == "log" and p.endswith(".json") and p + "l" in logs)
        and not (kind == "events" and os.path.splitext(os.path.basename(p))[0] in logged)
    )


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_stores())


def _workspace_file(kind: str, path: str) -> str:
    folder = os.path.dirname(os.path.dirname(path))  # <hash>, or <hash>/GitHub.copilot-chat for a transcript
    if kind == "events":
        folder = os.path.dirname(folder)
    return os.path.join(folder, "workspace.json")


def items():
    for kind, path in _stores():
        try:
            st = os.stat(path)
        except OSError:
            continue
        beside = 0  # the project comes from a file beside the chat, which can change on its own
        with contextlib.suppress(OSError):
            beside = os.stat(_workspace_file(kind, path)).st_mtime_ns
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}:{beside}", lambda k=kind, p=path: load(k, p))


def _folder(uri) -> str | None:
    """The local folder a `file://` URI names; a remote workspace has none to give."""
    if not isinstance(uri, str):
        return None
    u = urlparse(uri)
    if u.scheme != "file" or u.netloc not in ("", "localhost"):
        return None
    path = unquote(u.path)
    path = path[1:] if _DRIVE.match(path) else path
    return path.rstrip("/") or None


def _project(kind: str, path: str) -> str | None:
    try:
        with open(_workspace_file(kind, path), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return _folder(d.get("folder")) if isinstance(d, dict) else None


def load(kind: str, path: str) -> Session | None:
    s = Session(KEY, os.path.splitext(os.path.basename(path))[0], path)
    s.root = KEY
    s.project = _project(kind, path)
    messages: list[Message] = []
    calls = Calls()
    if kind == "events":
        _events(path, s, messages, calls)
        if not any(m.role == "user" for m in messages):  # an agent run that nobody typed
            s.origin = "headless"
    else:
        _requests(_replay(path), s, messages, calls)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


# --- chatSessions: the replayed state ----------------------------------------------------------------


def _replay(path: str) -> dict:
    """The session as VS Code holds it. A `.json` file is the state itself, and one being rewritten right now
    does not parse: the indexer tries it again later."""
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    state = None
    for op in read_jsonl(path):  # a half-written last line is dropped by read_jsonl
        kind = op.get("kind")
        if kind == 0:
            state = op.get("v")
        elif state is not None and isinstance(op.get("k"), list) and op["k"]:
            try:
                _apply(state, kind, op["k"], op.get("v"), op.get("i"))
            except (KeyError, IndexError, TypeError):  # a change to something the log never had: skip it
                continue
    return state if isinstance(state, dict) else {}


def _apply(state, kind, k: list, v, at) -> None:
    parent = state
    for part in k[:-1]:
        parent = parent[part]
    key = k[-1]
    if kind == 1:
        parent[key] = v
    elif kind == 3:
        del parent[key]
    elif kind == 2:
        items = parent[key] if isinstance(parent, list) or key in parent else []
        if isinstance(at, int):  # a push at an index first drops what was after it
            items = items[: max(at, 0)] + [None] * (at - len(items))
        parent[key] = [*items, *(v if isinstance(v, list) else [])]


def _requests(state: dict, s: Session, messages: list[Message], calls: Calls) -> None:
    s.native_id = state.get("sessionId") or s.native_id
    s.title = _title(state)
    s.started_at = iso_ts(state.get("creationDate"))
    s.updated_at = iso_ts(state.get("lastMessageDate"))
    if not s.project:  # a chat in a window with no folder open records where it ran
        s.project = _folder(state.get("workingDirectory"))
    for req in state.get("requests") or []:
        if not isinstance(req, dict):
            continue
        ts = iso_ts(req.get("timestamp"))
        done = iso_ts(req.get("responseTimestamp")) or ts
        s.started_at = s.started_at or ts
        s.updated_at = max(s.updated_at or 0, done or 0) or None
        if not req.get("isSystemInitiated") and not str(req.get("confirmation") or "").strip():
            message = req.get("message")
            text = message.get("text") if isinstance(message, dict) else message
            if isinstance(text, str):
                append(messages, "user", clean_user_text(text), ts)
        reply, tools = _response(req.get("response"))
        append(messages, "assistant", clean_assistant_text(_EMPTY_FENCE.sub("", reply)), done)
        for call_id, name, args, code, output in tools:
            failure = (signature(output) or f"exit code {code}") if code else None
            calls.add(*position(messages), done, name, args, call_id, failure)


def _title(state: dict) -> str | None:
    for key in ("customTitle", "computedTitle"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().split("\n")[0][:120]
    return None


def _response(parts) -> tuple[str, list]:
    """The reply's words, and its terminal calls as (id, tool, {command}, exit code, output)."""
    words: list[str] = []
    tools: list = []
    seen: set[str] = set()
    for part in parts if isinstance(parts, list) else [parts]:
        if isinstance(part, str):
            words.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        if kind is None and isinstance(part.get("value"), str):
            words.append(part["value"])
        elif kind == "markdownVuln" and isinstance(part.get("content"), dict):
            words.append(str(part["content"].get("value") or ""))
        elif kind == "inlineReference":  # the file or symbol VS Code draws as a name in the sentence
            ref = part.get("inlineReference") if isinstance(part.get("inlineReference"), dict) else {}
            name = part.get("name") or ref.get("name") or os.path.basename(str(ref.get("path") or ""))
            words.append(str(name or ""))
        elif kind == "toolInvocationSerialized":
            call = _terminal(part)
            if call and call[0] not in seen:  # one call can be written twice, once hidden
                seen.add(call[0])
                tools.append(call)
    return "".join(words), tools


def _terminal(part: dict):
    data = part.get("toolSpecificData")
    if not isinstance(data, dict):
        return None
    line = data.get("commandLine") if isinstance(data.get("commandLine"), dict) else {}
    command = next(
        (
            c.strip()
            for c in (line.get("toolEdited"), line.get("userEdited"), line.get("original"), data.get("command"))
            if isinstance(c, str) and c.strip()
        ),
        None,
    )
    state = data.get("terminalCommandState")
    code = state.get("exitCode") if isinstance(state, dict) else None
    if not command or not isinstance(code, int):
        return None
    out = data.get("terminalCommandOutput")
    return (
        str(part.get("toolCallId") or ""),
        str(part.get("toolId") or "run_in_terminal"),
        {"command": command},
        code,
        out.get("text") if isinstance(out, dict) and isinstance(out.get("text"), str) else "",
    )


# --- transcripts: the extension's event log ----------------------------------------------------------


def _events(path: str, s: Session, messages: list[Message], calls: Calls) -> None:
    running: dict[str, tuple[str, object]] = {}
    for e in read_jsonl(path):
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        kind, ts = e.get("type"), iso_ts(e.get("timestamp"))
        if kind == "session.start":
            s.native_id = data.get("sessionId") or s.native_id
            ts = iso_ts(data.get("startTime")) or ts
        elif kind == "user.message" and isinstance(data.get("content"), str):
            append(messages, "user", clean_user_text(data["content"]), ts)
        elif kind == "assistant.message" and isinstance(data.get("content"), str):
            append(messages, "assistant", clean_assistant_text(data["content"]), ts)
        elif kind == "tool.execution_start":
            running[str(data.get("toolCallId"))] = (data.get("toolName") or "", data.get("arguments"))
        elif kind == "tool.execution_complete" and data.get("success") is False:
            name, args = running.pop(str(data.get("toolCallId")), ("", None))
            detail = data.get("error") or data.get("result")
            failure = signature(detail if isinstance(detail, str) else json.dumps(detail or "")) or "error"
            calls.add(*position(messages), ts, name, args, data.get("toolCallId"), failure)
        if ts is not None:
            s.started_at = min(s.started_at or ts, ts)
            s.updated_at = max(s.updated_at or ts, ts)


AGENTS = {KEY: ("VS Code Copilot Chat", detect, items)}
