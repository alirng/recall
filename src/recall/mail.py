"""Messages between agent chats about files they share.

recall writes the first message itself when an edit hook finds another chat with open changes
to the same file; after that the agents talk through the message_chat MCP tool. A chat reads
its messages through its own hooks: on its next edit, when it finishes a turn, or on the
person's next prompt. A chat is addressed by id, and by transcript path too, because an
agent's hook may report a different id from the one its transcript carries.
"""

import contextlib
import fcntl
import json
import os
import time
import uuid

from . import paths

MAIL = os.path.join(paths.HOME, "cache", "mail.jsonl")
# Overlaps the person pressed Resolve on: key -> when, so the menu shows it and a second press
# does not send twice. A key whose overlap has gone is dropped by status().
RESOLVED = os.path.join(paths.HOME, "cache", "resolved.json")
READ = os.path.join(paths.HOME, "cache", "mail-read.json")
LOCK = os.path.join(paths.HOME, "cache", "mail.lock")
KEEP_SECONDS = 2 * 86400
# A finishing agent is kept going to answer at most this many messages from one chat an hour,
# so two agents cannot keep each other talking.
MAX_WAKES_PER_HOUR = 3


@contextlib.contextmanager
def _locked():
    """Hooks of several chats run at once; the mailbox and read marks change under one lock."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def post(to: str, sender: str, sender_title: str, text: str, to_source: str | None = None, **about) -> dict:
    msg = {
        "id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "to": to,
        "to_source": to_source,
        "from": sender,
        "from_title": sender_title,
        "text": text.strip(),
        **{k: v for k, v in about.items() if v},
    }
    with contextlib.suppress(OSError), _locked():
        if os.path.exists(MAIL) and os.path.getsize(MAIL) > 512_000:
            keep = [m for m in _all() if m.get("at", 0) > time.time() - KEEP_SECONDS][-1000:]
            with open(MAIL + ".tmp", "w") as f:
                f.writelines(json.dumps(m) + "\n" for m in keep)
            os.replace(MAIL + ".tmp", MAIL)
        with open(MAIL, "a") as f:
            f.write(json.dumps(msg) + "\n")
    return msg


def _all() -> list[dict]:
    out = []
    with contextlib.suppress(OSError), open(MAIL) as f:
        for line in f:
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def recent(since: float) -> list[dict]:
    return [m for m in _all() if m.get("at", 0) >= since]


def _state() -> dict:
    try:
        with open(READ) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    now = time.time()
    state = {k: v for k, v in state.items() if now - v.get("at", 0) < KEEP_SECONDS}
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(READ), exist_ok=True)
        with open(READ + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(READ + ".tmp", READ)


def take(chat: str, source: str | None = None, waking: bool = False) -> list[dict]:
    """Unread messages for a chat, marked read. With waking (the chat is about to finish its
    turn and would be kept going to answer), messages past the hourly cap stay unread for the
    next prompt or edit instead."""
    with contextlib.suppress(OSError), _locked():
        return _take(chat, os.path.realpath(source) if source else None, waking)
    return []


def _take(chat: str, source: str | None, waking: bool) -> list[dict]:
    state = _state()
    mine = state.get(chat, {"read": [], "wakes": []})
    unread = [
        m
        for m in _all()
        if (m.get("to") == chat or (source and m.get("to_source") == source))
        and m["id"] not in mine["read"]
        and m.get("from") != chat
    ]
    now = time.time()
    if waking:
        wakes = [w for w in mine.get("wakes", []) if now - w[1] < 3600]
        allowed = []
        for m in unread:
            if sum(1 for sender, _ in wakes if sender == m.get("from")) < MAX_WAKES_PER_HOUR:
                allowed.append(m)
                wakes.append([m.get("from"), now])
        unread, mine["wakes"] = allowed, wakes
    if unread:
        mine["read"] = (mine["read"] + [m["id"] for m in unread])[-500:]
        mine["at"] = now
        state[chat] = mine
        _save(state)
    return unread


def render(messages: list[dict], me: str) -> str:
    """Messages as the agent reads them, with how to answer."""
    lines = []
    for m in messages:
        about = f" about {m['file']}" if m.get("file") else ""
        lines.append(f'recall · message from "{m.get("from_title") or m["from"]}" ({m["from"]}){about}:\n{m["text"]}')
    # A Resolve note comes from recall; the agent answers the other chats it names.
    senders = list(dict.fromkeys(s for m in messages for s in (m.get("others") or [m["from"]])))
    lines.append(
        "To answer, use the message_chat tool of the recall MCP server (in Claude Code:"
        " mcp__recall__message_chat), not any other messaging tool: other chats only hear through"
        f' recall. Pass me="{me}" and to='
        + " or ".join(f'"{s}"' for s in senders)
        + ". Answer only if it changes what either of you does; tell the user what you agreed."
    )
    return "\n\n".join(lines)


def _deliver(event, waking: bool = False) -> str | None:
    me = f"{event.agent}:{event.session}"
    letters = take(me, event.transcript, waking=waking)
    return render(letters, me) if letters else None


def on_turn(event):
    """prompt and stop (hooks.py): unread messages for this chat. At the end of a turn they keep
    the agent going so it can answer (take() caps how often). Cursor's prompt event is skipped:
    its context reply is undocumented, and a message marked read there could be lost."""
    from .integrations.hooks import Reply

    if event.agent == "cursor" and event.kind == "prompt":
        return None
    text = _deliver(event, waking=event.kind == "stop")
    if not text:
        return None
    return Reply(keep_going=text) if event.kind == "stop" else Reply(context=text)


def on_edit(event):
    """edit (hooks.py): unread messages ride along wherever the agent can be told something."""
    from .integrations.hooks import Reply

    if "tell" not in event.acts:
        return None
    text = _deliver(event)
    return Reply(context=text) if text else None


def resolved() -> dict[str, dict]:
    """Resolve marks: overlap key (repo and file) -> {"at": when, "sessions": the chats sent to}."""
    try:
        with open(RESOLVED) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def forget_resolved(keep: set[str]) -> None:
    """Drop Resolve records for overlaps that are gone, so a new one on that file starts fresh."""
    marks = resolved()
    if set(marks) - keep:
        _write_resolved({k: v for k, v in marks.items() if k in keep})


def _write_resolved(marks: dict[str, dict]) -> None:
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(RESOLVED), exist_ok=True)
        with open(RESOLVED + ".tmp", "w") as f:
            json.dump(marks, f)
        os.replace(RESOLVED + ".tmp", RESOLVED)


def resolve(repo: str, file: str, since: float) -> dict:
    """The person pressed Resolve on an overlap: every chat in it gets a note, queued for its
    next edit, turn end or prompt (never interrupting), asking it to agree with the others who
    goes first. Pressing it again sends only to chats that joined the overlap since."""
    from . import status

    found = next((o for o in status.status(since)["overlaps"] if o["repo"] == repo and o["file"] == file), None)
    if not found:
        return {"sent": 0, "reason": "no such overlap any more"}
    marks = resolved()
    mark = marks.get(found["key"]) or {"at": None, "sessions": []}
    new = [sid for sid in found["sessions"] if sid not in mark["sessions"]]
    if not new:
        return {"sent": 0, "reason": "already sent", "sent_at": mark["at"]}
    sources = {s["session"]: s.get("source") for s in found["sides"]}
    for sid in new:
        others = [o for o in found["sessions"] if o != sid]
        text = (
            f"The user pressed Resolve in recall for {file}. {found['notes'].get(sid, '')}\n"
            "Message the other chat now to agree who changes it first (or that you are on different "
            "parts), then tell the user what you agreed."
        )
        post(
            sid, "recall", "recall, for the user", text, to_source=sources.get(sid), file=file, repo=repo, others=others
        )
    marks[found["key"]] = {"at": time.time(), "sessions": mark["sessions"] + new}
    _write_resolved(marks)
    return {"sent": len(new), "sent_at": marks[found["key"]]["at"]}
