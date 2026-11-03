"""Kimi Code history: one append-only JSONL wire log per agent at
`<home>/sessions/<workdir key>/<session>/agents/<agent>/wire.jsonl`, beside one `state.json` per session
(title, working folder, times). `agents/main` is the person's chat; any other agent is a subagent's.

A person's turn is a `context.append_message` record. The agent's turns are not messages: they are streamed
as `step.begin`, `content.part` (`text`; `think` is reasoning) and `step.end` loop events, and a tool call
and its result are `tool.call` and `tool.result` loop events joined by `toolCallId`. The host's own lines
(reminders, hook output, task notices) arrive as user messages too, and say so in `origin`.

Format knowledge: deja-vu docs/registry/kimi.md and internal/sources/kimi.go (MIT), cross-checked with
agentsview (MIT). The older MoonshotAI/kimi-cli (`~/.kimi`, a `context.jsonl` per session) is another
format and is not read.
"""

import glob
import json
import os
import re

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Kimi Code"

KEY = "kimi"
_HOOK_RESULT = re.compile(r"<hook_result\b.*?</hook_result>", re.S)


def _wires() -> list[str]:
    out = []
    for folder in roots.other_dirs(KEY):
        out += sorted(glob.glob(os.path.join(glob.escape(folder), "sessions", "*", "*", "agents", "*", "wire.jsonl")))
    return out


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_wires())


def _session_dir(wire: str) -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(wire)))


def items():
    for wire in _wires():
        state = os.path.join(_session_dir(wire), "state.json")
        try:
            st = os.stat(wire)
        except OSError:
            continue
        try:  # a rename or a generated title rewrites state.json and not the wire log
            sst = os.stat(state)
            extra = f"{sst.st_mtime_ns}:{sst.st_size}"
        except OSError:
            extra = ""
        yield Item(wire, f"{st.st_mtime_ns}:{st.st_size}:{extra}", lambda p=wire: load(p))


def _json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _text(content) -> str:
    """The text parts of a message's content."""
    if isinstance(content, str):
        return content
    parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"] if content else []
    return "\n\n".join(t for t in parts if isinstance(t, str) and t)


def _by_person(origin) -> bool:
    """A user message the person typed (or a slash command they ran), or one an older protocol wrote
    with no origin. Reminders, hook output and background-task notices name another origin."""
    if not isinstance(origin, dict):
        return True
    kind = origin.get("kind") or ""
    return kind.startswith("user") or kind == "" or origin.get("trigger") == "user-slash"


def _subagent_task(origin) -> bool:
    """The brief a parent agent handed a subagent, which opens its log as a message from the host."""
    return isinstance(origin, dict) and origin.get("kind") == "system_trigger" and origin.get("name") == "subagent"


def load(wire: str) -> Session | None:
    session_dir = _session_dir(wire)
    agent = os.path.basename(os.path.dirname(wire))
    session_id = os.path.basename(session_dir)
    state = _json(os.path.join(session_dir, "state.json"))
    subagent = agent != "main"
    if subagent and ((state.get("agents") or {}).get(agent) or {}).get("forkedFrom"):
        return None  # a fork opens with a copy of the main agent's context, which main's log already holds
    s = Session(KEY, f"{agent}-{session_id}" if subagent else session_id, wire)
    s.root = KEY
    s.project = state.get("workDir") or state.get("cwd") or None
    title = (state.get("title") or "").strip()
    s.title = None if not title or title == "Untitled" else title
    if subagent:  # the title and the times in state.json are the session's, which the main agent's chat carries
        s.title = None
        s.origin, s.parent = "subagent", f"{KEY}:{session_id}"
    else:
        s.started_at, s.updated_at = iso_ts(state.get("createdAt")), iso_ts(state.get("updatedAt"))

    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}  # toolCallId -> (tool, its input)
    reply: list[str] = []  # the text parts of the step being streamed
    reply_ts = first = last = None

    def flush() -> None:
        nonlocal reply, reply_ts
        append(messages, "assistant", clean_assistant_text("".join(reply).strip()), reply_ts)
        reply, reply_ts = [], None

    for d in read_jsonl(wire):
        kind, ts = d.get("type"), iso_ts(d.get("time"))
        if kind == "config.update" and not s.project:
            s.project = d.get("cwd") or None
        first, last = first or ts, ts or last
        if kind == "forked" and state.get(
            "forkedFrom"
        ):  # closes the copy of the source's records: only what follows is the fork's
            messages, calls, pending, reply = [], Calls(), {}, []
        elif kind == "context.append_message":
            msg = d.get("message")
            if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
                continue
            if msg["role"] == "user" and not (_by_person(msg.get("origin")) or _subagent_task(msg.get("origin"))):
                continue
            flush()
            text = _text(msg.get("content"))
            if msg["role"] == "user":
                append(messages, "user", clean_user_text(_HOOK_RESULT.sub("", text)), ts)
            else:
                append(messages, "assistant", clean_assistant_text(text), ts)
        elif kind == "context.append_loop_event" and isinstance(d.get("event"), dict):
            e = d["event"]
            what = e.get("type")
            if what in ("step.begin", "step.end"):
                flush()
            elif what == "content.part":
                part = e.get("part")
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    reply_ts = reply_ts or ts
                    reply.append(part["text"])
            elif what == "tool.call":
                flush()  # the words that led to the call come before it
                pending[e.get("toolCallId")] = (e.get("name") or "", e.get("args"))
            elif what == "tool.result" and e.get("toolCallId") in pending:
                tool, args = pending.pop(e["toolCallId"])
                result = e.get("result") if isinstance(e.get("result"), dict) else {}
                failure = None
                if result.get("isError"):
                    body = result.get("output")
                    failure = signature(body if isinstance(body, str) else json.dumps(body)) or "error"
                calls.add(*position(messages), ts, tool, args, e["toolCallId"], failure)
    flush()
    s.started_at, s.updated_at = s.started_at or first, s.updated_at or last
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


AGENTS = {KEY: ("Kimi Code", detect, items)}
