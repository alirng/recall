"""`recall forget`: remove chats from recall's index and archive.

For each chat that means its index rows (sessions, messages and their full-text index, its
project assignment, the list of files already read), its embedding vectors, its archive
copies, its extraction record and candidate memories, the exploration reports it produced
(explore.py), and the cached model answers that quote it. The database file is then
rewritten, so the words are not left in its free pages. The chat's id goes on a forgotten
list, so a later index run does not read it back from the agent's files, which recall never
deletes.

What it does not remove:
* Notes. They are the person's own files, so forget does not edit them. It lists the notes
  that cite a forgotten chat, and the git command that removes them from the notes folder's
  history.
* Cached model answers that only paraphrase a chat. Nothing ties those to it; forget finds
  the ones whose evidence is found in the chat's text and the ones that repeat one of its
  candidate memories word for word.
* The usage log, which holds the words asked and chat ids, never chat text.
* The mailbox between chats (mail.py), a short-lived record of notes about files two chats
  share. A note can quote up to 140 characters of what a chat was asked, and names its title.
  It is pruned by age as the file grows, not by forget.
* Anything a model provider already received when the chat was read for memory.
"""

import json
import os
import shlex
import sqlite3
import time

from . import paths
from .chats import archive
from .chats.sources import compressed


class Ambiguous(LookupError):
    """A name that fits several chats: forget deletes, so it does not pick one."""


def resolve(con: sqlite3.Connection, chat_id: str | None = None, project: str | None = None) -> list[str]:
    """Chat ids for one chat (a full id, or the part after "agent:", or the start of one) or
    for every chat in a project. Chats that only the archive still has count too. Raises
    Ambiguous when a name fits more than one chat."""
    ids = []
    if chat_id and chat_id.strip():
        ids += _named(con, chat_id.strip())
    if project:
        ids += _in_project(con, project)
    return list(dict.fromkeys(ids))


def _known(con: sqlite3.Connection) -> list[str]:
    ids = {r[0] for r in con.execute("select id from sessions")}
    if archive.enabled(con):
        ids |= {r[0] for r in con.execute("select distinct id from chat_archive")}
    return sorted(ids)


def _named(con: sqlite3.Connection, name: str) -> list[str]:
    """Compared as plain text, not as a LIKE pattern, so "%" or "_" in a name match nothing
    they should not."""
    known = _known(con)
    found = [i for i in known if i == name or i.endswith(":" + name)]
    if not found:  # the start of an id, or of the part after "agent:"
        found = [i for i in known if (i.startswith(name) if ":" in name else i.split(":", 1)[-1].startswith(name))]
    if len(found) > 1:
        shown = ", ".join(found[:5]) + (" …" if len(found) > 5 else "")
        raise Ambiguous(f"{name!r} fits {len(found)} chats ({shown}): give more of the id")
    return found


def _in_project(con: sqlite3.Connection, project: str) -> list[str]:
    """Chats of a project, matched as search matches them: by projects.yaml when the name is
    one of its projects, else by the folder or title containing the name. Chats that are only
    in the archive are matched from their stored copy."""
    from . import projects
    from .chats.projects import resolve as folder_project

    rows = [dict(r) for r in con.execute("select id, project_key, project, title, first_prompt from sessions")]
    indexed = {r["id"] for r in rows}
    if archive.enabled(con):
        for sid in (r[0] for r in con.execute("select distinct id from chat_archive")):
            if sid in indexed:
                continue
            s = archive.unpack(archive.copies(con, sid)[-1]["data"])
            home = folder_project(s.project)
            first = next((m.text for m in s.messages if m.role == "user"), "")[:500]
            rows.append(
                {
                    "id": sid,
                    "project_key": home.key if home else None,
                    "project": s.project,
                    "title": s.title or first[:80].replace("\n", " "),
                    "first_prompt": first,
                }
            )
    found = projects.find(project)
    if found:
        return [sid for sid, (pid, _) in projects.assign(rows, projects.load()).items() if pid == found.id]
    needle = project.lower()
    return [r["id"] for r in rows if needle in (r["project"] or "").lower() or needle in (r["title"] or "").lower()]


def describe(con: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """What forget would remove, per chat: title, source and counts."""
    out = []
    for sid in ids:
        s = con.execute("select title, agent, source, n_messages from sessions where id = ?", (sid,)).fetchone()
        copies = archive.copies(con, sid) if archive.enabled(con) else []
        held = archive.unpack(copies[-1]["data"]) if copies and not s else None  # only the archive has it
        # A chat Codex has compressed is keyed by its .jsonl path, but the .jsonl.zst is what holds it.
        files = {c["source"] for c in copies} | ({s["source"]} if s else set())
        out.append(
            {
                "chat_id": sid,
                "title": s["title"] if s else held and (held.title or next((m.text[:80] for m in held.messages), None)),
                "messages": s["n_messages"] if s else len(held.messages) if held else 0,
                "archive_copies": len(copies),
                "files": sorted({compressed.resolve(f) or f for f in files}),
            }
        )
    return out


def purge(con: sqlite3.Connection, ids: list[str], remember: bool = True, scrub: bool = True) -> dict:
    """Delete these chats everywhere recall holds them. remember: also put them on the
    forgotten list, so they are never indexed again. scrub: then rewrite the database file
    so the deleted words are not left in its free pages (see scrub_db). Returns counts per place."""
    have = _tables(con)
    counts = dict.fromkeys(
        ("chats", "messages", "vectors", "archive_copies", "candidates", "explorations", "cached_answers"), 0
    )
    texts = {sid: _text(con, sid, have) for sid in ids}
    counts["cached_answers"] = _purge_cache(con, ids, texts, have)
    deleted = counts["cached_answers"]

    def delete(table: str, column: str, sid: str) -> int:
        if table not in have:
            return 0
        return con.execute(f"delete from {table} where {column} = ?", (sid,)).rowcount

    named = {"messages": "messages", "vectors": "vectors", "candidates": "candidates", "explorations": "explorations"}
    for sid in ids:
        counts["chats"] += (n := delete("sessions", "id", sid))
        deleted += n + delete("sessions_fts", "id", sid)
        counts["archive_copies"] += (n := delete("chat_archive", "id", sid))
        deleted += n
        for table in _keyed(con, have):
            if table == "seen" and not remember:
                continue  # an unchanged file stays read, so retention does not re-read it each run
            n = delete(table, "session_id", sid)  # messages: a trigger updates messages_fts
            deleted += n
            if table in named:
                counts[named[table]] += n
        if remember:
            con.execute("insert or replace into forgotten_chats(id, at) values (?, ?)", (sid, time.time()))
    con.commit()
    if counts["vectors"]:
        from . import vectorfile

        try:  # the file search reads beside the index holds them too
            vectorfile.drop_deleted(con)
        except (OSError, sqlite3.Error) as e:  # search skips them anyway, and the next rewrite drops them
            archive.log(f"deleted chats, but could not rewrite the shared vector file ({e})")
    if scrub and deleted:
        try:
            scrub_db(con)
        except sqlite3.OperationalError as e:  # a background index run must not fail over this
            archive.log(f"deleted chats, but could not rewrite the index file ({e}); the words stay in free pages")
    return counts


def scrub_db(con: sqlite3.Connection) -> bool:
    """Make deleted words unrecoverable from the database file. SQLite deletes by marking
    space free: the words stay in free pages, in the full-text index (which records a
    deletion beside the text) and in the write-ahead log, until the file is rewritten.
    Returns False when another process has the index open, so the log could not be emptied
    and the old pages stay in it until that process closes."""
    con.commit()
    for fts in _fts_tables(con):  # chats' words are in more than one: messages, titles, explorations
        con.execute(f"insert into {fts}({fts}) values ('optimize')")  # merge away the deletion records
    con.commit()
    con.execute("vacuum")
    wait = con.execute("pragma busy_timeout").fetchone()[0]
    con.execute("pragma busy_timeout = 5000")  # do not stall on a reader that holds a long transaction
    try:
        return not con.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        con.execute(f"pragma busy_timeout = {wait}")


def leftovers(con: sqlite3.Connection) -> list[str]:
    """Forgotten chats that something still holds. An older release does not know the
    forgotten list: it reads their files back into the chat tables, embeds them and lets
    memory extraction cite them. (`seen` is skipped: it always names the file just read.)"""
    if not forgotten(con):
        return []
    have = _tables(con)
    found = set()
    for table, column in [(t, "session_id") for t in _keyed(con, have) if t != "seen"] + [
        ("sessions", "id"),
        ("chat_archive", "id"),
    ]:
        if table in have:
            found |= {
                r[0]
                for r in con.execute(
                    f"select distinct {column} from {table} where {column} in (select id from forgotten_chats)"
                )
            }
    return sorted(found)


def sweep(con: sqlite3.Connection) -> int:
    """Delete what leftovers finds again, keeping the chats on the forgotten list. Returns
    how many chats."""
    ids = leftovers(con)
    if ids:
        purge(con, ids, remember=False)
        archive.log(f"removed {len(ids)} forgotten chat(s) that an older release had read back")
    return len(ids)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}


def _fts_tables(con: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "select name from sqlite_master where type = 'table' and lower(sql) like 'create virtual table%using fts5%'"
        )
    ]


def _keyed(con: sqlite3.Connection, have: set[str]) -> list[str]:
    """Every table that keys rows by chat: vectors, extractions, candidates, and tables other
    features add later (such as explorations), without forget having to name them."""
    return [
        name
        for name in sorted(have)
        if not name.startswith(("sqlite_", "messages_fts", "sessions_fts"))
        and any(c[1] == "session_id" for c in con.execute(f"pragma table_info('{name}')"))
    ]


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
