"""ChatGPT chats, from the account export (Settings > Data controls > Export data), by `recall import`.

Classic desktop chats are encrypted on disk and Chat mode lives on OpenAI's servers, so the export is the only
way to read them. OpenAI documents neither its schema nor its file names, and changes them without notice:
what is known comes from community parsers. A conversation has `conversation_id` (or `id`), `title`,
`create_time`, `update_time`, `current_node` and `mapping`, a tree of {id, parent, children, message}. Edited and
regenerated turns are branches of it, so only the path from `current_node` up to the root is read.

Kept: the text of the user's and the assistant's visible turns, and the names of attached files. Dropped:
system and tool turns, the assistant's calls to tools, reasoning, hidden context and citation markers. As with
claude.ai (claudeai.py), there is no folder to watch: a chat's durable copy is the one in recall's archive. The
chats have no project, and stay out of `recall dream` until [roots.chatgpt] mine = true.
"""

import re

from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append

CHAT_SCHEMA = "2026-11-10 new source: ChatGPT account export, read by recall import"

KEY = "chatgpt"
LABEL = "ChatGPT"
_ID = re.compile(r"[\w-]{8,80}")
_TEXT_TYPES = ("text", "multimodal_text")
_CITATION = re.compile("【[^】]*†[^】]*】|[^]*")


def detect() -> int | None:
    return None  # nothing on disk to find: chats arrive by `recall import`


def items():
    return iter(())


def _id(c: dict) -> str:
    return str(c.get("conversation_id") or c.get("id"))


def usable(found: list[dict]) -> list[dict]:
    return [c for c in found if _ID.fullmatch(_id(c)) and isinstance(c.get("mapping"), dict)]


def _time(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _leaf(mapping: dict):
    """The node to read back from when an export has no usable current_node: the newest message."""
    timed = [
        (_time(n["message"].get("create_time")) or 0, k)
        for k, n in mapping.items()
        if isinstance(n, dict) and isinstance(n.get("message"), dict)
    ]
    return max(timed)[1] if timed else None


def _path(c: dict) -> list[dict]:
    """The active branch's messages, oldest first. A parent loop in the tree ends the walk."""
    mapping = c["mapping"]
    node_id = c.get("current_node") if c.get("current_node") in mapping else _leaf(mapping)
    out, visited = [], set()
    while node_id in mapping and node_id not in visited:
        visited.add(node_id)
        node = mapping[node_id]
        if not isinstance(node, dict):
            break
        if isinstance(node.get("message"), dict):
            out.append(node["message"])
        node_id = node.get("parent")
    out.reverse()
    return out


def _body(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, dict) or content.get("content_type") not in _TEXT_TYPES:
        return ""
    texts = []
    for part in content.get("parts") or []:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("content_type") == "audio_transcription":
            texts.append(str(part.get("text") or ""))
    return "\n".join(t for t in texts if t)


def _files(meta: dict) -> list[str]:
    names = []
    for f in meta.get("attachments") or []:
        name = f.get("name") if isinstance(f, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def load(c: dict) -> Session:
    cid = _id(c)
    s = Session(KEY, cid, f"chatgpt export#{cid}")
    s.root = KEY
    s.title = c.get("title") or None
    s.started_at = _time(c.get("create_time"))
    s.updated_at = _time(c.get("update_time"))
    messages: list[Message] = []
    for m in _path(c):
        author = m.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
        if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation"):
            continue
        if role == "assistant" and m.get("recipient", "all") != "all":
            continue  # a call to a tool
        ts = _time(m.get("create_time"))
        s.started_at = s.started_at or ts
        s.updated_at = s.updated_at or ts
        if role == "user":
            text = clean_user_text(_body(m))
            if files := _files(meta):
                text = "\n\n".join(t for t in (text, "Attached: " + ", ".join(files)) if t)
        else:
            text = clean_assistant_text(_CITATION.sub("", _body(m)))
        append(messages, role, text, ts)
    s.messages = messages
    return s


def export_items(chats: list[dict]):
    for c in chats:
        fingerprint = f"{c.get('update_time')}:{len(c['mapping'])}:{c.get('current_node')}"
        yield Item(f"chatgpt export#{_id(c)}", fingerprint, lambda c=c: load(c))


AGENTS = {KEY: (LABEL, detect, items)}
