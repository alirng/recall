"""Continue history (continuedev, VS Code, JetBrains and the `cn` CLI): `~/.continue/sessions/<id>.json`, one
document per chat, rewritten whenever it changes, and `sessions.json` beside them, a list of `{sessionId, title,
dateCreated, workspaceDirectory}`. `CONTINUE_GLOBAL_DIR` moves the folder.

A document holds `history[]`: each item a `message` (`role`, and `content` as a string or `{type, text}` parts) and,
on an assistant item that called tools, `toolCallStates[]` (`status`, `toolCall.function`, `output[]`). Only the
`user` and `assistant` text is kept; `system` is configuration and a tool's result is under the item that asked
for it. A call whose state is `errored`, or whose output says `Command failed`, is a failed tool call. A
compaction rewrites the file to the system message and one assistant item carrying `conversationSummary`: that
item is a summary, not a turn, and is not read.

Nothing in the file has a time. `dateCreated` of the list entry is the start, the file's mtime the last update, and
turns carry none. The folder is `workspaceDirectory` (a `file://` URI in the IDE, a path in the CLI), from the
document or else the list entry. A title that is Continue's placeholder is dropped.

Format knowledge: deja-vu docs/registry/continue.md and internal/sources/continuedev.go (MIT), checked against
Continue's core/index.d.ts types as that file cites them.
"""

import contextlib
import json
import os
from urllib.parse import unquote, urlparse

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts

CHAT_SCHEMA = "2026-11-03 new source: Continue"

KEY = "continue"
_LIST = "sessions.json"
_PLACEHOLDER_TITLES = ("new session", "untitled session")


def _folders() -> list[str]:
    return [os.path.join(f, "sessions") for f in roots.other_dirs(KEY) if os.path.isdir(os.path.join(f, "sessions"))]


def _files() -> list[str]:
    out = []
    for folder in _folders():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        out += [os.path.join(folder, n) for n in names if n.endswith(".json") and n != _LIST]
    return out


def _list(folder: str) -> dict[str, dict]:
    """The list entry of each session, by id."""
    with contextlib.suppress(OSError, ValueError, UnicodeError):
        with open(os.path.join(folder, _LIST), encoding="utf-8") as f:
            entries = json.load(f)
        return {e["sessionId"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("sessionId"), str)}
    return {}


def detect() -> int | None:
    if not _folders():
        return None
    return len(_files())


def items():
    lists = {folder: _list(folder) for folder in _folders()}
    for path in _files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        folder = os.path.dirname(path)
        entry = lists.get(folder, {}).get(os.path.basename(path).removesuffix(".json"), {})
        # the list entry holds the date and the folder, and a rename changes it and not the document
        fingerprint = f"{st.st_mtime_ns}:{st.st_size}:{entry.get('title')}:{entry.get('dateCreated')}"
        yield Item(path, fingerprint, lambda p=path, e=entry: load(p, e))


def _folder(value) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("file://"):
        url = urlparse(value)
        if url.netloc not in ("", "localhost"):
            return None
        value = unquote(url.path)
        if len(value) > 2 and value[0] == "/" and value[2] == ":":  # file:///C:/proj
            value = value[1:]
    return value if os.path.isabs(value) or (len(value) > 2 and value[1] == ":") else None


def _date(value) -> float | None:
    """dateCreated: an ISO string, or epoch milliseconds in a string on older versions."""
    if isinstance(value, str) and value.strip().isdigit():
        return iso_ts(int(value))
    return iso_ts(value)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))


def _failure(state: dict) -> str | None:
    """What a failed call said, None for one that did not fail."""
    outputs = [o for o in state.get("output") or [] if isinstance(o, dict)]
    failed = state.get("status") == "errored" or any(
        str(o.get("status", "")).startswith("Command failed") for o in outputs
    )
    if not failed:
        return None
    said = "\n".join(o["content"] for o in outputs if isinstance(o.get("content"), str)) or "\n".join(
        str(o["status"]) for o in outputs if o.get("status")
    )
    return signature(said) or "error"


def load(path: str, entry: dict) -> Session | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        mtime = os.stat(path).st_mtime
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(doc, dict):
        return None
    sid = (
        doc["sessionId"]
        if isinstance(doc.get("sessionId"), str) and doc["sessionId"]
        else os.path.basename(path).removesuffix(".json")
    )
    s = Session(KEY, sid, path)
    s.root = KEY
    s.project = _folder(doc.get("workspaceDirectory")) or _folder(entry.get("workspaceDirectory"))
    s.started_at, s.updated_at = _date(entry.get("dateCreated")), mtime
    messages: list[Message] = []
    calls = Calls()
    for item in doc.get("history") if isinstance(doc.get("history"), list) else []:
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict) or item.get("conversationSummary") is not None:
            continue
        role, text = message.get("role"), _text(message.get("content"))
        if role == "user":
            append(messages, "user", clean_user_text(text), None)
        elif role == "assistant":
            append(messages, "assistant", clean_assistant_text(text), None)
            for state in item.get("toolCallStates") or []:
                if not isinstance(state, dict):
                    continue
                call = state.get("toolCall", {}).get("function") if isinstance(state.get("toolCall"), dict) else None
                call = call if isinstance(call, dict) else {}
                args = state.get("parsedArgs") or call.get("arguments")
                if isinstance(args, str):
                    with contextlib.suppress(ValueError):
                        args = json.loads(args)
                calls.add(
                    *position(messages), None, call.get("name") or "", args, state.get("toolCallId"), _failure(state)
                )
    first = next((m.text for m in messages if m.role == "user"), "").split("\n")[0].strip()
    for title in (doc.get("title"), entry.get("title")):
        if isinstance(title, str) and title.strip().lower() not in ("", *_PLACEHOLDER_TITLES, first.lower()):
            s.title = title.strip()
            break
    s.messages = messages
    s.tool_errors = calls.errors()
    return s if messages else None


AGENTS = {KEY: ("Continue", detect, items)}
