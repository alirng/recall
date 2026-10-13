"""`recall forget`: remove chats from everything recall holds about them.

For each chat that means its index rows (sessions, messages and their full-text index, its
project assignment, the list of files already read), its embedding vectors, its archive
copies, its extraction record and candidate memories, and the cached model answers that
quote it. Its id then goes on a forgotten list, so a later index run does not read it back
from the agent's files, which recall never deletes.

Notes are the person's own files, so forget does not edit them. It lists the notes that cite
a forgotten chat, and the git command that removes them from the notes folder's history.
"""

import json
import os
import shlex
import sqlite3
import time

from . import paths
from .chats import archive, store


def resolve(con: sqlite3.Connection, chat_id: str | None = None, project: str | None = None) -> list[str]:
    """Chat ids for one chat (any id form search shows) or for every chat in a project."""
    ids = []
    if chat_id:
        sid = store.resolve_id(con, chat_id)
        if not sid and archive.enabled(con):  # archived but not in the index
            row = con.execute(
                "select id from chat_archive where id = ? or id like ?", (chat_id, f"%:{chat_id}")
            ).fetchone()
            sid = row[0] if row else None
        ids += [sid] if sid else []
    if project:
        fsql, fargs = store._filters(con, None, project, None)
        ids += [r[0] for r in con.execute(f"select s.id from sessions s where 1 = 1 {fsql}", fargs)]
    return list(dict.fromkeys(ids))


def describe(con: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """What forget would remove, per chat: title, source and counts."""
    out = []
    for sid in ids:
        s = con.execute("select title, agent, source, n_messages from sessions where id = ?", (sid,)).fetchone()
        copies = archive.copies(con, sid) if archive.enabled(con) else []
        out.append(
            {
                "chat_id": sid,
                "title": s["title"] if s else None,
                "messages": s["n_messages"] if s else 0,
                "archive_copies": len(copies),
                "files": sorted({c["source"] for c in copies} | ({s["source"]} if s else set())),
            }
        )
    return out


def purge(con: sqlite3.Connection, ids: list[str], remember: bool = True) -> dict:
    """Delete these chats everywhere recall holds them. remember: also put them on the
    forgotten list, so they are never indexed again. Returns counts per place."""
    have = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    counts = dict.fromkeys(("chats", "messages", "vectors", "archive_copies", "candidates", "cached_answers"), 0)
    texts = {sid: _text(con, sid, have) for sid in ids}
    counts["cached_answers"] = _purge_cache(con, ids, texts, have)

    def delete(table: str, column: str, sid: str) -> int:
        if table not in have:
            return 0
        return con.execute(f"delete from {table} where {column} = ?", (sid,)).rowcount

    # Every table that keys rows by chat: vectors, extractions, candidates, and tables other
    # features add later (such as explorations), without forget having to name them.
    keyed = [
        name
        for name in sorted(have)
        if not name.startswith(("sqlite_", "messages_fts", "sessions_fts"))
        and any(c[1] == "session_id" for c in con.execute(f"pragma table_info('{name}')"))
    ]
    named = {"messages": "messages", "vectors": "vectors", "candidates": "candidates"}
    for sid in ids:
        counts["chats"] += delete("sessions", "id", sid)
        delete("sessions_fts", "id", sid)
        counts["archive_copies"] += delete("chat_archive", "id", sid)
        for table in keyed:
            if table == "seen" and not remember:
                continue  # an unchanged file stays read, so retention does not re-read it each run
            n = delete(table, "session_id", sid)  # messages: a trigger updates messages_fts
            if table in named:
                counts[named[table]] += n
        if remember:
            con.execute("insert or replace into forgotten_chats values (?, ?)", (sid, time.time()))
    con.commit()
    return counts


def forgotten(con: sqlite3.Connection) -> set[str]:
    if not archive.enabled(con):
        return set()
    return {r[0] for r in con.execute("select id from forgotten_chats")}


def notes_citing(ids: list[str]) -> list[str]:
    """Paths of the notes whose sources include any of these chats."""
    if not os.path.isdir(paths.MEMORY):
        return []
    from .memory.notes import NoteStore

    wanted = set(ids)
    return sorted(n.path for n in NoteStore().load(include_archived=True).values() if wanted & set(n.sources))


def history_command(note_paths: list[str]) -> str:
    """The command that removes these notes from every commit of the notes repository."""
    rel = [os.path.relpath(p, paths.MEMORY) for p in note_paths]
    args = " ".join(f"--path {shlex.quote(r)}" for r in rel)
    return f"git -C {shlex.quote(paths.MEMORY)} filter-repo --invert-paths {args}"


def _text(con, sid: str, have: set[str]) -> str:
    """Everything recall holds of one chat's words, to find cached answers that quote it."""
    parts = []
    if "messages" in have:
        parts += [r[0] for r in con.execute("select text from messages where session_id = ?", (sid,))]
    if "chat_archive" in have:
        for row in archive.copies(con, sid):
            parts += [m.text for m in archive.unpack(row["data"]).messages]
    return "\n".join(parts)


def _purge_cache(con, ids: list[str], texts: dict[str, str], have: set[str]) -> int:
    """Delete cached model answers that quote these chats: an extraction answer whose
    evidence is grounded in a chat's text (the test extraction itself uses), or any answer
    that repeats one of the chat's candidate memories word for word."""
    if "llm_cache" not in have:
        return 0
    from .memory.extract import grounded

    needles = []
    if "candidates" in have:
        for sid in ids:
            for r in con.execute("select title, body, evidence from candidates where session_id = ?", (sid,)):
                needles += [t for t in r if t and len(t) >= 20]
    doomed = []
    for key, response in con.execute("select key, response from llm_cache").fetchall():
        data = _load(response)
        said = "\n".join(_strings(data))
        quotes = _strings(data, "evidence")
        if any(n in said for n in needles) or any(grounded(q, t) for q in quotes for t in texts.values() if t):
            doomed.append(key)
    con.executemany("delete from llm_cache where key = ?", [(k,) for k in doomed])
    return len(doomed)


def _load(response: str):
    try:
        return json.loads(response)
    except (TypeError, ValueError):
        return None


def _strings(data, key: str | None = None) -> list[str]:
    """Every string in a decoded answer, or only those under this key."""
    if isinstance(data, dict):
        own = [v for k, v in data.items() if isinstance(v, str) and (key is None or k == key)]
        return own + [s for v in data.values() if not isinstance(v, str) for s in _strings(v, key)]
    if isinstance(data, list):
        return [s for v in data for s in (_strings(v, key) if not isinstance(v, str) else [v] if key is None else [])]
    return []
