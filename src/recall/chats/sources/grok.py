"""Grok Build (the `grok` CLI) history: one folder per session at
`<home>/sessions/<percent-encoded folder>/<session id>/`, holding

  summary.json       the title, the folder it ran in, times and, for a subagent, its kind
  chat_history.jsonl the conversation as the model sees it: `user`, `assistant` (with `tool_calls`, their
                     `arguments` a JSON string), `tool_result`, `reasoning`, `system`, `backend_tool_call`
  updates.jsonl      the same run as an ACP update stream: `user_message_chunk` and `agent_message_chunk`
                     pieces, `tool_call` and `tool_call_update` (the status of a call)
  events.jsonl       one `tool_completed` per call, with its outcome

The text is read from chat_history.jsonl, where the host's own user turns carry a `synthetic_reason`, and from
updates.jsonl when a session has no chat history. A failed call is one whose outcome in events.jsonl is
`error` (or, from the stream alone, whose update says `failed`). `grok.db` belongs to grok-dev, another CLI
that shares ~/.grok, and is not read.

Format knowledge: SpecStory's GROK-FORMAT.md (Apache-2.0) and agentsview's grok parser and golden sessions
(MIT), cross-checked with deja-vu docs/registry/grok.md (MIT).
"""

import contextlib
import glob
import json
import os
import re
from urllib.parse import unquote

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

CHAT_SCHEMA = "2026-11-03 new source: Grok Build"

KEY = "grok"
FILES = ("chat_history.jsonl", "updates.jsonl", "events.jsonl", "summary.json")
_MCP_SERVERS = re.compile(r"<mcp_servers>.*?</mcp_servers>", re.S)


def _sessions() -> list[str]:
    out: dict[str, None] = {}
    for folder in roots.other_dirs(KEY):
        for name in ("chat_history.jsonl", "updates.jsonl"):
            for path in sorted(glob.glob(os.path.join(glob.escape(folder), "sessions", "*", "*", name))):
                out[os.path.dirname(path)] = None
    return list(out)


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_sessions())


def _transcript(folder: str) -> str:
    updates = os.path.join(folder, "updates.jsonl")
    return updates if os.path.exists(updates) else os.path.join(folder, "chat_history.jsonl")


def items():
    for folder in _sessions():
        stats = []
        for name in FILES:
            try:
                st = os.stat(os.path.join(folder, name))
                stats.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                stats.append("")
        yield Item(_transcript(folder), "|".join(stats), lambda p=folder: load(p))


def _json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _folder(session: str, summary: dict) -> str | None:
    """The folder the session ran in: its summary says, then the `.cwd` file beside the sessions of that
    folder, then the group folder's own name when that is an absolute path."""
    cwd = _d(summary.get("info")).get("cwd") or summary.get("cwd")
    if not cwd:
        with contextlib.suppress(OSError), open(os.path.join(os.path.dirname(session), ".cwd"), encoding="utf-8") as f:
            cwd = f.read().strip()
    if not cwd:
        decoded = unquote(os.path.basename(os.path.dirname(session)))
        cwd = decoded if decoded.startswith("/") else None
    return cwd or None


def load(session: str) -> Session | None:
    summary = _json(os.path.join(session, "summary.json"))
    s = Session(KEY, os.path.basename(session), _transcript(session))
    s.root = KEY
    s.project = _folder(session, summary)
    title = summary.get("generated_title") or summary.get("session_summary")
    s.title = title.strip() if isinstance(title, str) and title.strip() else None
    s.started_at, s.updated_at = iso_ts(summary.get("created_at")), iso_ts(summary.get("updated_at"))
    kind = summary.get("session_kind")
    if kind == "headless":
        s.origin = "headless"
    elif kind in ("subagent", "subagent_fork"):
        s.origin = "subagent"
        if summary.get("parent_session_id"):
            s.parent = f"{KEY}:{summary['parent_session_id']}"

    messages: list[Message] = []
    calls = Calls()
    history = os.path.join(session, "chat_history.jsonl")
    if os.path.exists(history):
        _history(history, os.path.join(session, "events.jsonl"), messages, calls)
    else:
        _stream(
            os.path.join(session, "updates.jsonl"), s.native_id, bool(summary.get("parent_session_id")), messages, calls
        )
    stamps = [m.ts for m in messages if m.ts]
    s.started_at = s.started_at or min(stamps, default=None)
    s.updated_at = s.updated_at or max(stamps, default=None)
    if not s.updated_at:  # no times in the summary, as when a session's folder has no summary yet
        with contextlib.suppress(OSError):
            s.updated_at = os.stat(s.source).st_mtime
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _outcomes(path: str) -> dict[str, str]:
    """tool call id -> outcome ("success", "error"), from events.jsonl."""
    out: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for d in read_jsonl(path):
            if d.get("type") in ("tool_completed", "mcp_tool_call_completed") and d.get("tool_call_id"):
                out[d["tool_call_id"]] = str(d.get("outcome") or "")
    return out


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _arguments(raw) -> object:
    """A call's input: Grok writes it as a JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _history(path: str, events: str, messages: list[Message], calls: Calls) -> None:
    outcomes = _outcomes(events)
    pending: dict[str, tuple[str, object]] = {}
    for d in read_jsonl(path):
        kind = d.get("type") or {"tool": "tool_result"}.get(
            d.get("role"), d.get("role")
        )  # an older release wrote `role`
        if kind == "user":
            reason = d.get("synthetic_reason")
            if not reason or reason == "interjection":  # the others are the host's reminders and instructions
                text = clean_user_text(_MCP_SERVERS.sub("", text_blocks(d.get("content"))))
                append(messages, "user", text, None)
        elif kind == "assistant":
            append(messages, "assistant", clean_assistant_text(text_blocks(d.get("content"))), None)
            for c in d.get("tool_calls") or []:
                if isinstance(c, dict):
                    fn = _d(c.get("function")) or c
                    pending[c.get("id")] = (fn.get("name") or "", _arguments(fn.get("arguments")))
        elif kind == "tool_result" and d.get("tool_call_id") in pending:
            call_id = d["tool_call_id"]
            tool, args = pending.pop(call_id)
            failure = None
            if outcomes.get(call_id) == "error":
                body = d.get("content")
                failure = signature(body if isinstance(body, str) else text_blocks(body)) or "error"
            calls.add(*position(messages), None, tool, args, call_id, failure)


def _stream(path: str, own_id: str, forked: bool, messages: list[Message], calls: Calls) -> None:
    """A session with no chat history: its words and calls from the update stream. A chunk of a reply
    shares its `promptId`, and a chunk of a prompt its `promptIndex`, with the rest of that message."""
    chunk: tuple[str, object] | None = None  # (role, key) of the message being joined
    parts: list[str] = []
    chunk_ts = None
    pending: dict[str, tuple[str, object]] = {}

    def flush() -> None:
        nonlocal chunk, parts, chunk_ts
        if chunk:
            text = "".join(parts)
            append(
                messages,
                chunk[0],
                clean_user_text(text) if chunk[0] == "user" else clean_assistant_text(text),
                chunk_ts,
            )
        chunk, parts, chunk_ts = None, [], None

    for d in read_jsonl(path):
        params = _d(d.get("params"))
        update, meta = _d(params.get("update")), _d(params.get("_meta"))
        event_id = meta.get("eventId")
        if forked and isinstance(event_id, str) and not event_id.startswith(own_id + "-"):
            continue  # a --fork-session copy carries its source's records
        ts = iso_ts(meta.get("agentTimestampMs") or d.get("timestamp"))
        what = update.get("sessionUpdate")
        if what in ("user_message_chunk", "agent_message_chunk"):
            content = _d(update.get("content"))
            text = content.get("text") if content.get("type") == "text" else None
            if not isinstance(text, str) or not text:
                continue
            role = "user" if what == "user_message_chunk" else "assistant"
            key = (role, _d(update.get("_meta")).get("promptIndex") if role == "user" else meta.get("promptId"))
            if chunk != key or key[1] is None:
                flush()
                chunk, chunk_ts = key, ts
            parts.append(text)
        elif what == "tool_call" and update.get("toolCallId"):
            flush()
            tool = _d(_d(update.get("_meta")).get("x.ai/tool")).get("name") or update.get("title") or ""
            pending[update["toolCallId"]] = (tool, update.get("rawInput"))
        elif (
            what == "tool_call_update"
            and update.get("status") in ("completed", "failed")
            and update.get("toolCallId") in pending
        ):
            call_id = update["toolCallId"]
            tool, args = pending.pop(call_id)
            failure = None
            if update["status"] == "failed":
                failure = signature(_tool_text(update.get("content"))) or "failed"
            calls.add(*position(messages), ts, tool, args, call_id, failure)
    flush()


def _tool_text(content) -> str:
    """The text of a tool update's output: ACP wraps each block, `{"type": "content", "content": {"text": ...}}`."""
    if isinstance(content, dict):
        content = [content]
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return "\n".join(t for b in blocks if isinstance(t := b.get("text") or _d(b.get("content")).get("text"), str))


AGENTS = {KEY: ("Grok Build", detect, items)}
