"""Factory Droid history: `~/.factory/sessions/<slug>/<uuid>.jsonl` (older builds: `sessions/<uuid>.jsonl`), one
event per line. The first is `session_start` (`id`, `title`, `cwd`, `owner`); then `message` events whose
`message.role` is `user` or `assistant` and whose `content` is a string or Anthropic-style blocks (`text`,
`thinking`, `tool_use`, and `tool_result` with `is_error`, in the user turn after the call), `todo_state`
and `compaction_state`. The slug is the folder with its separators turned into dashes and is lossy, so the
project is `session_start.cwd`. `<uuid>.settings.json` (model, token counts) is not read.

The format is closed-source: line 1 is confirmed by Factory's own SDK, the rest by SpecStory's parser.

Format knowledge: SpecStory's droidcli provider (Apache-2.0), franken_agent_detection's factory connector
(MIT), and the field list in docs/research/agent-storage-and-integration.md.
"""

import os

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Factory Droid"

KEY = "droid"


def _files() -> list[str]:
    out: list[str] = []
    for home in roots.other_dirs(KEY):
        base = os.path.join(home, "sessions")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if name.endswith(".jsonl"):
                out.append(path)
            elif os.path.isdir(path):
                try:
                    out += [os.path.join(path, n) for n in sorted(os.listdir(path)) if n.endswith(".jsonl")]
                except OSError:
                    continue
    return out


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_files())


def items():
    for path in _files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path: load(p))


def _first(*values) -> str | None:
    return next((v.strip() for v in values if isinstance(v, str) and v.strip()), None)


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _output(content) -> str:
    return "\n".join(b["text"] for b in _blocks(content) if isinstance(b.get("text"), str))


def load(path: str) -> Session | None:
    s = Session(KEY, os.path.basename(path).removesuffix(".jsonl"), path)
    s.root = KEY
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    try:
        for d in read_jsonl(path):
            kind, ts = d.get("type"), iso_ts(d.get("timestamp"))
            if kind == "session_start":
                nested = d.get("session") if isinstance(d.get("session"), dict) else {}
                s.native_id = _first(d.get("id"), nested.get("id")) or s.native_id
                s.title = _first(d.get("title"), d.get("sessionTitle"), nested.get("title"))
                s.project = _first(
                    d.get("cwd"), d.get("workspace_root"), nested.get("cwd"), nested.get("workspace_root")
                )
                ts = ts or iso_ts(_first(d.get("created_at"), nested.get("created_at")))
            elif kind == "message" and isinstance(d.get("message"), dict):
                _message(d["message"], ts, messages, calls, pending)
            if ts is not None:
                s.started_at = min(s.started_at or ts, ts)
                s.updated_at = max(s.updated_at or ts, ts)
    except (OSError, UnicodeError):
        return None
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _message(m: dict, ts: float | None, messages: list[Message], calls: Calls, pending: dict) -> None:
    role = m.get("role")
    for b in _blocks(m.get("content")):
        kind = b.get("type")
        if kind == "text" and isinstance(b.get("text"), str) and role in ("user", "assistant"):
            text = clean_user_text(b["text"]) if role == "user" else clean_assistant_text(b["text"])
            append(messages, role, text, ts)
        elif kind == "tool_use" and isinstance(b.get("id"), str):
            pending[b["id"]] = (b.get("name") or "", b.get("input"))
        elif kind == "tool_result":
            name, args = pending.pop(str(b.get("tool_use_id")), ("", None))
            failure = (signature(_output(b.get("content"))) or "error") if b.get("is_error") is True else None
            calls.add(*position(messages), ts, name, args, b.get("tool_use_id"), failure)


AGENTS = {KEY: ("Factory Droid", detect, items)}
