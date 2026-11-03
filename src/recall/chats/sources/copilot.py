"""GitHub Copilot CLI history: `<home>/session-state/<id>/events.jsonl`, an append-only log of
`{id, parentId, timestamp, type, data}` events, beside a `workspace.yaml` that holds the session's name
(`COPILOT_HOME` moves the home). Older builds wrote the same log as `session-state/<id>.jsonl`. Not VS Code's
Copilot Chat, and not the `history-session-state/<id>.json` files from before 0.0.342.

`session.start` names the folder (`data.context.cwd`, `branch`); `user.message` and `assistant.message` carry
the words in `data.content` (`transformedContent` is what the model saw and is not read). Tool calls are
`tool.execution_start` and `tool.execution_complete`, which says `success: false` for a call that failed (a
command that exits non-zero still succeeds as a call). A `task` sub-agent runs inside its parent's log, every
record of it stamped with `agentId`: those records are not the person's chat and are left out.
`config.json`, `mcp-oauth-config`, `mcp-secrets` and `session-store.db` are never opened.

Format knowledge: deja-vu's docs/registry/copilot.md (MIT) and agentsview's internal/parser/copilot.go (MIT),
checked against GitHub's SDK event types as docs/research/agent-storage-and-integration.md cites them.
"""

import contextlib
import os
import re

import yaml

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: GitHub Copilot CLI"

KEY = "copilot"
# Copilot's hooks add their output to a turn inside <system_reminder> (with an underscore).
_REMINDER = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)


def _sessions() -> list[tuple[str, str, str | None]]:
    """(session id, its log, its workspace.yaml) for each session, once per id. A session in both layouts
    is read from the directory."""
    out, seen = [], set()
    for home in roots.other_dirs(KEY):
        base = os.path.join(home, "session-state")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if os.path.isdir(path):
                log, workspace, sid = os.path.join(path, "events.jsonl"), os.path.join(path, "workspace.yaml"), name
            elif name.endswith(".jsonl") and name.removesuffix(".jsonl") not in names:
                log, workspace, sid = path, None, name.removesuffix(".jsonl")
            else:
                continue
            if sid not in seen and os.path.isfile(log):
                seen.add(sid)
                out.append((sid, log, workspace))
    return out


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_sessions())


def items():
    for sid, log, workspace in _sessions():
        try:
            st = os.stat(log)
            fingerprint = f"{st.st_mtime_ns}:{st.st_size}"
            if workspace:  # a rename changes workspace.yaml and not the log
                with contextlib.suppress(OSError):
                    ws = os.stat(workspace)
                    fingerprint += f":{ws.st_mtime_ns}:{ws.st_size}"
        except OSError:
            continue
        yield Item(log, fingerprint, lambda s=sid, p=log, w=workspace: load(s, p, w))


def _workspace(path: str | None) -> dict:
    if not path:
        return {}
    with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError):
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f)
        return d if isinstance(d, dict) else {}
    return {}


def _text(value, user: bool) -> str:
    if not isinstance(value, str):
        return ""
    return clean_user_text(_REMINDER.sub("", value)) if user else clean_assistant_text(value)


def load(sid: str, path: str, workspace: str | None) -> Session | None:
    meta = _workspace(workspace)
    s = Session(KEY, sid, path)
    s.root = KEY
    name = meta.get("name")
    s.title = name.strip() or None if isinstance(name, str) else None
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}  # tool call id -> (tool, arguments) of the calls in flight
    try:
        for d in read_jsonl(path):
            kind, data, ts = d.get("type"), d.get("data"), iso_ts(d.get("timestamp"))
            if not isinstance(data, dict):
                continue
            if kind == "session.start":
                _start(s, data, ts)
                continue
            if d.get("agentId"):  # a sub-agent's record
                continue
            s.started_at = s.started_at or ts
            s.updated_at = ts or s.updated_at
            if kind == "user.message":
                source = data.get("source")
                content = data.get("content")
                if not (isinstance(source, str) and source.startswith("skill-")) and not (
                    isinstance(content, str) and content.startswith("<skill-context")
                ):
                    append(messages, "user", _text(content, True), ts)
            elif kind == "assistant.message":
                append(messages, "assistant", _text(data.get("content"), False), ts)
            elif kind == "tool.execution_start" and isinstance(data.get("toolCallId"), str):
                pending[data["toolCallId"]] = (data.get("toolName") or "", data.get("arguments"))
            elif kind == "tool.execution_complete":
                name, args = pending.pop(str(data.get("toolCallId")), ("", None))
                failure = (signature(_failure(data)) or "error") if data.get("success") is False else None
                calls.add(*position(messages), ts, name, args, data.get("toolCallId"), failure)
    except (OSError, UnicodeError):
        return None
    if not s.project and isinstance(meta.get("cwd"), str):
        s.project = meta["cwd"] or None
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _start(s: Session, data: dict, ts: float | None) -> None:
    """The first session.start names the folder and the time. The id is the folder's name: a fork copies
    its source's records, whose session.start names the source."""
    s.started_at = s.started_at or iso_ts(data.get("startTime")) or ts
    context = data.get("context")
    if isinstance(context, dict):
        for field, attr in (("cwd", "project"), ("branch", "branch")):
            if isinstance(context.get(field), str) and context[field] and not getattr(s, attr):
                setattr(s, attr, context[field])


def _failure(data: dict) -> str:
    """What a failed call said: its error, else its result."""
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    if isinstance(error, str) and error.strip():
        return error
    result = data.get("result")
    return str(result.get("content") if isinstance(result, dict) else result or "")


AGENTS = {KEY: ("GitHub Copilot CLI", detect, items)}
