"""claude.ai chats, from the account export (Settings > Privacy > Export data), by `recall import`.

These chats live on Anthropic's servers: the desktop app's Chat tab keeps only a few recently opened ones on
disk. The export is a ZIP with conversations.json, an array of conversations, each with `uuid`, `name`,
`created_at`, `updated_at` and `chat_messages`. A message has `sender` (human or assistant), `content`
(typed blocks) and `text`, which older exports use for the body and newer ones may leave as a preview.

There is no folder to watch, so items() has nothing to yield and a chat's durable copy is the one in recall's
archive, which brings it back after a rebuild and which `recall forget` deletes. Importing a later export
updates the chats that changed and leaves the rest. Thinking and tool calls are dropped, and of an attachment
only the file name is kept. The chats have no project, and stay out of `recall dream` until
[roots.claudeai] mine = true.
"""

import re

from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, text_blocks

CHAT_SCHEMA = "2026-11-10 new source: claude.ai account export, read by recall import"

KEY = "claudeai"
LABEL = "claude.ai"
_UUID = re.compile(r"[0-9a-fA-F-]{36}")
_ROLES = {"human": "user", "user": "user", "assistant": "assistant"}


def detect() -> int | None:
    return None  # nothing on disk to find: chats arrive by `recall import`


def items():
    return iter(())


def usable(found: list[dict]) -> list[dict]:
    return [c for c in found if _UUID.fullmatch(str(c.get("uuid"))) and isinstance(_messages(c), list)]


def _messages(c: dict):
    return c.get("chat_messages") if "chat_messages" in c else c.get("messages")


def _files(m: dict) -> list[str]:
    names = []
    for field in ("attachments", "files"):
        for f in m.get(field) or []:
            name = f.get("file_name") if isinstance(f, dict) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def _text(m: dict) -> str:
    content = m.get("content")
    body = text_blocks(content, ("text",)) if isinstance(content, list) else ""
    return body or (m["text"] if isinstance(m.get("text"), str) else "")


def load(c: dict) -> Session:
    uuid = str(c["uuid"])
    s = Session(KEY, uuid, f"claude.ai export#{uuid}")
    s.root = KEY
    s.title = c.get("name") or c.get("summary") or None
    s.started_at = iso_ts(c.get("created_at"))
    s.updated_at = iso_ts(c.get("updated_at"))
    messages: list[Message] = []
    for m in _messages(c):
        if not isinstance(m, dict) or (role := _ROLES.get(m.get("sender") or m.get("role"))) is None:
            continue
        ts = iso_ts(m.get("created_at"))
        s.started_at = s.started_at or ts
        s.updated_at = s.updated_at or ts
        if role == "user":
            text = clean_user_text(_text(m))
            if files := _files(m):
                text = "\n\n".join(t for t in (text, "Attached: " + ", ".join(files)) if t)
        else:
            text = clean_assistant_text(_text(m))
        append(messages, role, text, ts)
    s.messages = messages
    return s


def export_items(chats: list[dict]):
    for c in chats:
        fingerprint = f"{c.get('updated_at')}:{len(_messages(c))}"
        yield Item(f"claude.ai export#{c['uuid']}", fingerprint, lambda c=c: load(c))


AGENTS = {KEY: (LABEL, detect, items)}
