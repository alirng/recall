"""Grok Bot history (Cursor's personal-agent app, bundle com.anysphere.sand): the transcript cache the
app keeps for each bot in ~/Library/Application Support/Grok Bot/sand-client-persistence/.

The folder holds JSON blobs whose file names are the base32 of a key such as
sand.client.slice.account.<account>.transcript.replicas.<bot id>. A bot's replica holds its last 200
entries at most (older ones are the app's to fetch again from the cloud), and the roster blob names the
bots. Only those two kinds of blob are opened: the rest of the folder, and the app's other files
(sand-secrets.json among them), are never read.

One chat is one bot on one UTC day, so a day keeps its id as the window slides and recall's archive
(archive.merge) carries the messages a re-read no longer has. Bots have no project folder: each chat's
project is a folder named for its bot under the app's own, so [contexts] can put them in a context
with one folder entry, and Session.root is "grokbot", which `recall dream` does not read until
[roots.grokbot] mine = true.

Reading is opt-in (enabled()). The format is undocumented. A blob in a shape this does not know is
skipped with a warning, and the run raises Incomplete, so that nothing is stamped as read in full.
"""

import base64
import binascii
import datetime
import glob
import json
import os
import re
import sys

from ... import config, roots
from ..transcript import Message, Session
from . import Incomplete, Item

CHAT_SCHEMA = "2026-11-03 new source: one chat per bot per UTC day from the app's transcript cache"

PERSISTENCE = "sand-client-persistence"
AGENT = "grokbot"
# schemaVersion of each blob kind as of Grok Bot 0.78
KNOWN_SCHEMAS = {"replica": 1, "roster": 4}
_KEY = re.compile(r"\.(?:transcript\.replicas\.(?P<bot>[0-9a-f-]{36})|(?P<roster>roster\.last-roster))$")
_THREAD = re.compile(r"t(\d+)")


def _folder() -> str:
    return os.path.join(roots.grokbot_dir(), PERSISTENCE)


def _blobs() -> list[tuple[str, str, str | None]]:
    """(kind, path, bot id) of the transcript and roster blobs. A file's key is in its name, so no
    other blob is opened."""
    out = []
    for path in sorted(glob.glob(os.path.join(_folder(), "*.blob"))):
        stem = os.path.basename(path)[: -len(".blob")].upper()
        try:
            key = base64.b32decode(stem + "=" * (-len(stem) % 8)).decode()
        except (binascii.Error, UnicodeDecodeError):
            continue
        if m := _KEY.search(key):
            out.append(("replica", path, m["bot"]) if m["bot"] else ("roster", path, None))
    return out


def detect() -> int | None:
    """How many bot-days it would index, or None when Grok Bot has not been used on this machine."""
    if not os.path.isdir(_folder()):
        return None
    return sum(len(days) for _, days in _bots()[0])


def _read(path: str, kind: str) -> dict | None:
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("schemaVersion") != KNOWN_SCHEMAS[kind] or not isinstance(d.get("value"), dict):
        return None
    return d["value"]


def enabled() -> bool:
    """Reading Grok Bot is opt-in: `recall setup` asks, or set [grokbot] enabled = true (or list "grokbot"
    in `sources`) in config.toml. These are personal agents, so an upgrade never starts reading them."""
    cfg = config.load()
    table = cfg.get("grokbot")
    return (isinstance(table, dict) and table.get("enabled") is True) or "grokbot" in (cfg.get("sources") or [])


def deleted_bots() -> int:
    """How many bots the roster no longer lists, whose chats are therefore left out."""
    return _bots()[2] if os.path.isdir(_folder()) else 0


def _skipped() -> list[str]:
    """[grokbot] skip = ["name or bot id", ...] in config.toml: bots recall never reads. The names are
    the person's, so they are kept in their config, not here."""
    cfg = config.load().get("grokbot")
    skip = cfg.get("skip") if isinstance(cfg, dict) else None
    return [s.strip().lower() for s in skip if isinstance(s, str) and s.strip()] if isinstance(skip, list) else []


def _bots() -> tuple[list[tuple[dict, dict[str, list[dict]]]], int, int]:
    """Each bot as ({"id", "name", "path"}, {UTC day: its entries}), without the skipped bots and
    without the entries another skipped bot sent or was sent; how many blobs were in a shape this
    does not know, which are left out; and how many deleted bots there are. A bot the roster no longer
    lists is deleted, and is never read: its chats can hold what the person set it up to watch (a work
    calendar), and nothing says they should still be searchable. Without a roster nothing can be told
    from a deleted bot, so nothing is read."""
    blobs = _blobs()
    names, bad = {}, []
    for kind, path, _ in blobs:
        if kind != "roster":
            continue
        value = _read(path, kind)
        rows = value.get("rows") if value else None
        if not isinstance(rows, list):
            bad.append(path)
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("name"), str):
                names.setdefault(row["id"], row["name"])
    skip = _skipped()
    gone = {i for i, n in names.items() if n.lower() in skip or i.lower() in skip}
    gone |= {i for i in skip if re.fullmatch(r"[0-9a-f-]{36}", i)}
    replicas = {bot for kind, _, bot in blobs if kind == "replica"}
    if replicas and not names:
        bad.append("roster")
    deleted = replicas - set(names)
    gone |= deleted
    out = []
    for kind, path, bot in blobs:
        if kind != "replica" or bot in gone:
            continue
        value = _read(path, kind)
        entries = value.get("entries") if value else None
        if not isinstance(entries, list):
            bad.append(path)
            continue
        days: dict[str, list[dict]] = {}
        for e in sorted((e for e in entries if isinstance(e, dict)), key=lambda e: e.get("seq") or 0):
            ts = e.get("timestampMs")
            if not isinstance(ts, int | float) or {_id(e.get("fromAgent")), _id(e.get("toAgent"))} & gone:
                continue
            day = datetime.datetime.fromtimestamp(ts / 1000, datetime.UTC).date().isoformat()
            days.setdefault(day, []).append(e)
        out.append(({"id": bot, "name": names.get(bot) or f"bot {bot[:6]}", "path": path}, days))
    for path in bad:
        print(
            f"recall: grokbot: {os.path.basename(path)[:12]}… is in a shape this release does not read", file=sys.stderr
        )
    return out, len(bad), len(deleted)


def _id(agent) -> str | None:
    return agent.get("id") if isinstance(agent, dict) else None


def items():
    if not os.path.isdir(_folder()) or not enabled():
        return
    bots, unreadable, _ = _bots()
    for bot, days in bots:
        for day, entries in days.items():
            # the same chat has the same count and ends while nothing has been added to it
            ident = f"{len(entries)}:{entries[0].get('seq')}:{entries[-1].get('seq')}:{entries[-1].get('timestampMs')}"
            yield Item(f"{bot['path']}#{day}", ident, lambda b=bot, d=day, e=entries: load(b, d, e))
    if unreadable:  # the rest is indexed, but this was not a full read: nothing is stamped as one
        raise Incomplete(f"{unreadable} Grok Bot blobs skipped: the app's format has changed")


def load(bot: dict, day: str, entries: list[dict]) -> Session | None:
    s = Session(AGENT, f"{bot['id']}:{day}", f"{bot['path']}#{day}")
    s.root = AGENT
    s.title = f"{bot['name']} · {day}"
    s.project = os.path.join(roots.grokbot_dir(), bot["name"].replace(os.sep, " "))
    messages: list[Message] = []
    thread = None
    for e in entries:
        said = _said(e)
        if not said:
            continue
        role, text = said
        ts = e["timestampMs"] / 1000
        s.started_at = s.started_at or ts
        s.updated_at = ts
        t = _THREAD.match(str(e.get("id")))
        # a run of one speaker's messages is one turn, but only within a thread: a routine that fires
        # many times a day stays several messages
        if messages and messages[-1].role == role and (t.group(1) if t else thread) == thread:
            messages[-1].text += "\n\n" + text
        else:
            messages.append(Message(role, text, ts))
        thread = t.group(1) if t else thread
    s.messages = messages
    return s


def _said(e: dict) -> tuple[str, str] | None:
    """(role, text) of an entry, or None for what recall does not keep: widgets' options, attachments'
    contents, email drafts, connector, secret-request and local-tool-permission payloads, events, notices."""
    if e.get("suppressed") or e.get("isStreaming"):
        return None
    kind = e.get("kind")
    if kind == "send-message":  # what the bot said
        m = e.get("message")
        m = m if isinstance(m, dict) else {}
        if m.get("type") == "text":
            return _text("assistant", m.get("content"))
        if m.get("type") == "widget":  # a question the bot put to the person, and the answer they picked
            w = m.get("widget")
            prompt = w.get("prompt") if isinstance(w, dict) else None
            if (answer := e.get("respondedValue")) and isinstance(answer, str):
                return _text("user", f'[answered "{prompt}"] {answer}' if isinstance(prompt, str) else answer)
            return _text("assistant", prompt)
        if m.get("type") == "attachment":
            return _text("assistant", f"[sent a file: {m['file_name']}]" if m.get("file_name") else None)
        if m.get("type") == "cursor-agent":
            return _text("assistant", f"[started a Cursor background agent: {m['title']}]" if m.get("title") else None)
        return None
    if kind == "user-attachment":
        return _text("user", f"[sent a file: {e['file_name']}]" if e.get("file_name") else None)
    if kind != "message" or not isinstance(e.get("content"), str):
        return None
    sender = e.get("fromAgent")
    author = e.get("author")
    if e.get("role") == "assistant":
        to = e.get("toAgent")
        return _text(
            "assistant", (f"[to {to['name']}] " if isinstance(to, dict) and to.get("name") else "") + e["content"]
        )
    if e.get("role") == "user":
        if isinstance(sender, dict):  # another bot's brief, not the person's words
            return _text("assistant", f"[from {sender.get('name') or 'another bot'}] " + e["content"])
        if e.get("channel") and not (isinstance(author, dict) and author.get("kind") == "cursor_user"):
            return None  # a channel's message from someone else
        return _text("user", e["content"])
    return None


def _text(role: str, text) -> tuple[str, str] | None:
    text = text.strip() if isinstance(text, str) else ""
    return (role, text) if text else None
