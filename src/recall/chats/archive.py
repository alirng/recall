"""A durable copy of every chat recall has indexed.

The chat tables are rebuilt from each agent's files whenever the parser changes
(CHAT_SCHEMA_VERSION), but agents delete and rewrite those files: Claude Code removes
transcripts after `cleanupPeriodDays` (30 by default), and Codex compaction has rewritten a
session file to a hundredth of its size. So recall keeps its own copy of each chat,
compressed, in a table a rebuild never drops.

There is one copy per (chat, source file), because one chat can live in several files:
Cursor writes a chat's transcript under each project folder it was opened in, and Codex
keeps a session in both sessions/ and archived_sessions/. The copies can differ, so the
index holds their union (store.upsert), and the shrink guard compares each file only with
its own earlier copy.

* Every upsert writes its file's copy, after the shrink guard (merge) has put back any
  message a re-read of that file no longer has, marked Kept.
* Before a rebuild drops the old chat tables, chats the archive has no copy of are copied in
  from them (snapshot), so upgrading from a release without this table loses nothing.
* After a rebuild has read every source, chats no source yielded are restored from here and
  marked restored (store.restore).
* `recall forget` and a retention period set by an organisation (retention.py) delete copies.

A copy holds what the index holds: messages already cleaned, truncated and redacted.
"""

import difflib
import json
import os
import sqlite3
import sys
import time
import zlib
from dataclasses import dataclass

from .. import paths
from .transcript import Message, Session, clean_user_text, drop_compaction_summary

SCHEMA = """
create table if not exists chat_archive (
  id text, source text, agent text,
  parser text,       -- CHAT_SCHEMA_VERSION of the parse this copy came from
  source_size int,   -- the file's size then: agents' chat files only grow unless rewritten
  n_messages int, updated_at real, archived_at real,
  restored_at real,  -- set while the chat is served from here because no source yields it
  kept int,          -- messages this copy holds that its file no longer has
  data blob,         -- zlib-compressed JSON: the session's fields and its messages
  primary key (id, source)
);
-- chats `recall forget` removed: never indexed again
create table if not exists forgotten_chats (id text primary key, at real);
"""


@dataclass
class Kept(Message):
    """A message only the archive still has: its file dropped it."""


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def enabled(con: sqlite3.Connection) -> bool:
    """ ":memory:" indexes (the benchmarks) have no archive."""
    return con.execute("select 1 from sqlite_master where name = 'chat_archive'").fetchone() is not None


def pack(s: Session, messages: list[Message]) -> bytes:
    d = {
        "agent": s.agent,
        "native_id": s.native_id,
        "source": s.source,
        "project": s.project,
        "title": s.title,
        "branch": s.branch,
        "started_at": s.started_at,
        "updated_at": s.updated_at,
        "origin": s.origin,
        "messages": [[m.role, m.ts, m.text, 1] if isinstance(m, Kept) else [m.role, m.ts, m.text] for m in messages],
    }
    return zlib.compress(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode())


def unpack(blob: bytes) -> Session:
    d = json.loads(zlib.decompress(blob))
    messages = [(Kept if rest else Message)(role, text, ts) for role, ts, text, *rest in d.pop("messages")]
    return Session(**d, messages=messages)


def messages(row: sqlite3.Row, parser: str) -> list[Message]:
    """A copy's messages. A copy made by another parser version has its user turns put
    through today's cleaning, so a cleaning change reaches chats whose files are gone too.
    That includes compaction summaries, which older parsers indexed as the person's words.
    Another session's message or a task notification cannot be told from the person's text
    once the file is gone, and stays."""
    msgs = unpack(row["data"]).messages
    if row["parser"] == parser:
        return msgs
    out = []
    for m in msgs:
        text = drop_compaction_summary(clean_user_text(m.text)) if m.role == "user" else m.text
        if text.strip():
            out.append(type(m)(m.role, text, m.ts))
    return out


def copies(con: sqlite3.Connection, sid: str) -> list[sqlite3.Row]:
    return con.execute("select * from chat_archive where id = ? order by archived_at", (sid,)).fetchall()


def save(con, s: Session, messages: list[Message], parser: str, size: int | None) -> None:
    con.execute(
        "insert or replace into chat_archive values (?,?,?,?,?,?,?,?,?,?,?)",
        (
            s.id,
            s.source,
            s.agent,
            parser,
            size,
            len(messages),
            s.updated_at,
            time.time(),
            None,
            kept(messages),
            pack(s, messages),
        ),
    )


def source_size(source: str) -> int | None:
    try:
        return os.path.getsize(source) if os.path.isfile(source) else None
    except OSError:
        return None


def kind(source: str) -> str:
    """A chat file, or a row in an agent's database ("<db path>#<key>"). Copies of one chat
    are unioned only within a kind: Cursor's database and its transcripts word a chat
    differently, so their union would hold most messages twice."""
    return "db" if "#" in source else "file"


# --- the shrink guard ------------------------------------------------------------


def merge(old: list[Message], new: list[Message], only_kept: bool = False, mark: bool = True) -> list[Message]:
    """The new parse, plus every message of the old copy it no longer has.

    An old message is still there when the new parse has a message from the same speaker,
    at the same place, whose text equals or contains it: a turn that grew, or two turns
    that merged once the message between them went. The lists are aligned on (speaker,
    text) with difflib; old-only messages stay where they were, new ones go where the parse
    puts them, and a message in both appears once.

    only_kept: carry over only messages already marked Kept, for a copy made by another
    parser version, whose other messages may differ just because the parser changed.
    mark: mark carried-over messages Kept (the guard); False for a union of two files.
    """
    if only_kept and not kept(old):
        return new
    a = [(m.role, m.text) for m in old]
    b = [(m.role, m.text) for m in new]
    if not a or (len(b) >= len(a) and a[:-1] == b[: len(a) - 1] and _covers(new[len(a) - 1 : len(a)], old[-1])):
        return new  # the chat grew: the usual case, without aligning anything
    merged: list[Message] = []
    carried = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            near = new[max(0, j1 - 1) : j2 + 1]
            for m in old[i1:i2]:
                if _covers(near, m) or (only_kept and not isinstance(m, Kept)):
                    continue
                merged.append(Kept(m.role, m.text, m.ts) if mark else m)
                carried += 1
        if tag != "delete":
            merged.extend(new[j1:j2])
    return merged if carried else new


def union(first: list[Message], others: list[list[Message]]) -> list[Message]:
    """One chat's messages across several files, each message once (merge's alignment)."""
    out = first
    for other in others:
        out = merge(other, out, mark=False)
    return out


def kept(messages: list[Message]) -> int:
    return sum(isinstance(m, Kept) for m in messages)


def _covers(candidates: list[Message], m: Message) -> bool:
    return any(c.role == m.role and len(c.text) >= len(m.text) and m.text in c.text for c in candidates)


def log(line: str) -> None:
    """One line to stderr and to logs/archive.log: chat ids and counts, never chat text."""
    print(f"recall: {line}", file=sys.stderr)
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        with open(os.path.join(paths.LOGS, "archive.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


# --- before a rebuild --------------------------------------------------------------


def snapshot(con: sqlite3.Connection, parser: str) -> int:
    """Copy chats from the index tables into the archive, before a rebuild drops them.
    parser: the version that built them. A chat is copied when the archive has no copy of
    it, or when the index saw it more recently than any copy (a release without the archive
    indexed it since), keeping what that copy had marked Kept. Forgotten chats are never
    copied: an older release, which does not know the forgotten list, may have read one back."""
    have = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    if not {"sessions", "messages"} <= have:
        return 0
    sizes = {}
    if "seen" in have:  # the file's size when it was parsed: "<mtime_ns>:<size>"
        for key, fp, sid in con.execute("select key, fingerprint, session_id from seen where session_id is not null"):
            if ":" in (fp or "") and "#" not in key:
                sizes[(sid, key)] = int(fp.rsplit(":", 1)[1])
    rows = con.execute(
        """select s.* from sessions s
           left join (select id, max(coalesce(updated_at, 0)) last from chat_archive group by id) a on a.id = s.id
           where (a.id is null or a.last < coalesce(s.updated_at, 0))
             and s.id not in (select id from forgotten_chats)"""
    ).fetchall()
    for r in rows:
        msgs = [
            Message(m[0], m[2], m[1])
            for m in con.execute("select role, ts, text from messages where session_id = ? order by seq", (r["id"],))
        ]
        if not msgs:
            continue
        # sessions.title falls back to the opening message; keep only a title the agent gave.
        derived = (r["first_prompt"] or "")[:80].replace("\n", " ")
        s = Session(
            r["agent"],
            r["native_id"],
            r["source"],
            project=r["project"],
            title=None if r["title"] == derived else r["title"],
            branch=r["branch"],
            started_at=r["started_at"],
            updated_at=r["updated_at"],
            origin=r["origin"],
        )
        mine = con.execute("select data from chat_archive where id = ? and source = ?", (s.id, s.source)).fetchone()
        if mine:
            msgs = merge(unpack(mine["data"]).messages, msgs, only_kept=True)
        save(con, s, msgs, parser, sizes.get((r["id"], r["source"])))
    return len(rows)
