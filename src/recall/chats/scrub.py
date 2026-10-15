"""Rewrite the text recall keeps with today's redaction rules.

The chat tables are derived: a change to redact.py reaches them when CHAT_SCHEMA_VERSION
bumps and they are rebuilt from the agents' files. Some tables hold chat text that no
rebuild reproduces, so a secret that an older rule let through stays in them:

* candidates (the quotes and notes a model drew from chats) and llm_cache (its answers),
* the archive's copies of chats whose files the agent has since deleted (chat_archive),
* tool_errors, where a failed command's text lives (in the index, and in the archive's copies
  of chats), exploration reports (explore.py) and the asks that commit links keep beside an
  edit (commits.py).

A failed tool call's words are redacted as redact.redact_tool() does, with the values that the
messages of the same chat held (redact.Chat), as they are when they are written (store.upsert);
errors.reclean says which rules a stored field can be read by again.

ensure() runs at the start of an index run and rewrites those tables once per
redact.VERSION; run() is the same rewrite without the version check, for a one-off clean.
The messages and sessions tables are rewritten too, so a rule change takes effect without
waiting for a rebuild. store.connect() marks the scrub due again after it has copied chats
into the archive, which are copied as the old tables hold them.

The notes (the markdown files in ~/.agents/memory) and their ledger are yours: ensure() only
looks at them and names the ones the rules match, and the notes that were learned from a
candidate it had to change. It rewrites none of them.
"""

import itertools
import json
import os
import re
import sqlite3
import sys
import time
import zlib
from collections import Counter
from collections.abc import Callable

from .. import paths
from .errors import TEXT_FIELDS, identity, reclean
from .redact import VERSION, Chat, count, redact, redact_json

# table -> the columns that hold chat text. Missing tables and columns are skipped.
TEXT_COLUMNS = {
    "messages": ("text",),
    "sessions": ("title", "first_prompt"),
    "candidates": ("title", "body", "why", "evidence", "reason"),
    "extractions": ("error",),
    "llm_calls": ("error",),
    "dream_runs": ("error",),
    "tool_errors": ("command_head", "signature", "next_reply"),
    "explorations": ("prompt", "report"),
    "exploration_starts": ("prompt",),
    "link_edits": ("first_ask", "ask"),  # commit links: what the person asked before an edit (commits.py)
    "link_events": ("text", "detail"),
    "commit_links": ("ask",),
}
# virtual tables that index a column of the table before: external-content ones are rebuilt,
# the others are rewritten row by row (see _fts)
EXTERNAL_FTS = {"messages": "messages_fts", "tool_errors": "tool_errors_fts"}
META_KEY = "redact_rules"


def stale(con: sqlite3.Connection) -> bool:
    row = con.execute("select v from meta where k = ?", (META_KEY,)).fetchone()
    return not row or row[0] != VERSION


def ensure(con: sqlite3.Connection, log: Callable[[str], None] | None = None) -> None:
    """Scrub once per redact.VERSION. The file is always rewritten too: a rebuild has just dropped
    tables whose free pages still hold the old words, whether or not any row needed a change. The
    version is stamped last, so a busy database means the next run tries again (and says so)."""
    from .. import forget

    touched: dict[str, bool] = {}
    try:
        if not stale(con):
            return
        run(con, stamp=False, log=log, touched=touched)
        forget.scrub_db(con)  # the old words are also in free pages and the log until the file is rewritten
        con.execute("insert or replace into meta values (?, ?)", (META_KEY, VERSION))
        con.commit()
    except sqlite3.OperationalError as e:
        con.rollback()
        _say(f"the redaction scrub did not finish ({e}); it runs again on the next index run", log)
        return
    for line in notes_report(paths.MEMORY, touched):
        _say(line, log)


def _say(line: str, log: Callable[[str], None] | None) -> None:
    """To the caller's log (stderr by default, never stdout: the MCP server speaks on it) and to
    logs/redact.log. Counts, chat and note ids, never text."""
    (log or (lambda m: print(f"recall: {m}", file=sys.stderr)))(line)
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        with open(os.path.join(paths.LOGS, "redact.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


# --- the notes ----------------------------------------------------------------------------------


def notes_report(root: str, touched: dict[str, bool]) -> list[str]:
    """What to tell the person about their notes: the notes and ledger entries that the rules match
    today, and the notes learned from a candidate the scrub changed (touched: note id -> whether
    the candidate's title changed). Nothing is rewritten: a note may be edited by hand, and its
    old versions are in the notes folder's git history, which only the owner can rewrite."""
    files, ledger = _scan_notes(root)
    named = {i for i, _ in files}
    groups = (
        (f"notes in {root} match the redaction rules", files),
        ("entries of .recall/notes.json match them", ledger),
        (
            "more notes were learned from a candidate the scrub changed",
            [(i, t) for i, t in touched.items() if i not in named],
        ),
    )
    lines = []
    for what, items in groups:
        if items:
            ids = sorted(_show(i, title) for i, title in items)
            more = f" and {len(ids) - 20} more" if len(ids) > 20 else ""
            lines.append(f"{len(ids)} {what}: {', '.join(ids[:20])}{more}")
    if lines:
        lines.append("recall leaves your notes as they are: edit them, and see the README on the notes' git history")
    return lines


def _show(note_id: str, title_hit: bool) -> str:
    """A note id is a slug of its title: if the title holds what a rule matches, name only the end of the id."""
    return f"...{note_id[-5:]}" if title_hit else note_id


def _scan_notes(root: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """(note ids, whether the title is what matched) for the note files, then for the ledger entries."""
    files: list[tuple[str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".recall")]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if count(text):
                title = re.search(r"^title:[ \t]*(.*)$", text, re.M)
                if name in ("MEMORY.md", "README.md"):
                    files.append((os.path.relpath(os.path.join(dirpath, name), root), False))
                else:
                    files.append((name[:-3], bool(title and count(title.group(1)))))
    ledger: list[tuple[str, bool]] = []
    try:
        with open(os.path.join(root, ".recall", "notes.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for section in ("known", "rejected"):
        for note_id, entry in (data.get(section) or {}).items() if isinstance(data, dict) else ():
            if isinstance(entry, dict) and any(count(v) for v in entry.values() if isinstance(v, str)):
                ledger.append((note_id, bool(count(str(entry.get("title") or "")))))
    return sorted(files), sorted(ledger)


def run(
    con: sqlite3.Connection,
    stamp: bool = True,
    log: Callable[[str], None] | None = None,
    touched: dict[str, bool] | None = None,
) -> Counter:
    """Redact the text in every table that holds it. Returns the number of values changed per table.column.
    touched, if given, collects the notes learned from candidates that were changed (note id -> title changed)."""
    con.execute("pragma secure_delete = on")  # a rewritten value is zeroed, not left in a free page
    changed: Counter = Counter()
    have = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    chats: Counter | None = None
    for table, wanted in TEXT_COLUMNS.items():
        if table not in have:
            continue
        columns = [r[1] for r in con.execute(f"pragma table_info({table})") if r[1] in wanted]
        if table in ("messages", "tool_errors"):
            if chats is None:  # the messages, and the tool errors of the same chats
                chats = _rewrite_chats(con, changed, have)
                if "tool_errors_fts" in have:  # before the commit below: a stop in between must not strand old terms
                    con.execute("insert into tool_errors_fts(tool_errors_fts) values ('rebuild')")
            n = chats[table] if table == "messages" else 0  # the index of tool_errors was rebuilt above
        else:
            n = _rewrite(con, table, columns, changed, touched if table == "candidates" else None)
        if n and table == "explorations":
            # the matching words kept beside a report are made again from the redacted text (explore._upgrade)
            con.execute("update explorations set prompt_terms = null, report_terms = null")
            if "explorations_fts" in have:
                _standalone_fts(con, "explorations_fts", ("prompt", "report"))
        if n and table in EXTERNAL_FTS and EXTERNAL_FTS[table] in have:
            fts = EXTERNAL_FTS[table]
            con.execute(f"insert into {fts}({fts}) values ('rebuild')")
        if n and table == "sessions" and "sessions_fts" in have:
            _sessions_fts(con)
        con.commit()
    if "llm_cache" in have:
        changed["llm_cache.response"] += _rewrite_json(con, "llm_cache", "key", "response", _cache_value)
    if "chat_archive" in have:
        changed["chat_archive.data"] += _rewrite_json(con, "chat_archive", "id, source", "data", _archive_value)
    if stamp:
        con.execute("insert or replace into meta values (?, ?)", (META_KEY, VERSION))
    con.commit()
    if log and sum(changed.values()):
        log("redacted " + ", ".join(f"{n} in {k}" for k, n in sorted(changed.items()) if n))
    return changed


def _rewrite(con, table: str, columns: list[str], changed: Counter, notes: dict[str, bool] | None = None) -> int:
    if not columns:
        return 0
    select = ", ".join(columns)
    n = 0
    rows: dict[int, bool] = {}  # candidates changed: rowid -> its title changed
    for row in con.execute(f"select rowid, {select} from {table}").fetchall():
        new = [redact(v) if isinstance(v, str) else v for v in row[1:]]
        diff = [c for c, a, b in zip(columns, row[1:], new, strict=True) if a != b]
        if diff:
            sets = ", ".join(f"{c} = ?" for c in diff)
            con.execute(f"update {table} set {sets} where rowid = ?", [new[columns.index(c)] for c in diff] + [row[0]])
            for c in diff:
                changed[f"{table}.{c}"] += 1
            rows[row[0]] = "title" in diff
            n += 1
    if notes is not None and rows:  # a note made from a changed candidate may hold what the candidate held
        ids = list(rows)
        for i in range(0, len(ids), 500):
            part = ids[i : i + 500]
            for rowid, note_id in con.execute(
                f"select rowid, note_id from {table} where rowid in ({','.join('?' * len(part))})", part
            ):
                if note_id:
                    notes[note_id] = notes.get(note_id, False) or rows[rowid]
    return n


def _rewrite_chats(con, changed: Counter, have: set[str]) -> Counter:
    """The messages, a chat at a time, so a key repeated bare further down the chat goes with the one a word
    marked, and the same chat's failed tool calls with them. Returns the rows changed per table."""
    n: Counter = Counter()
    columns = [r[1] for r in con.execute("pragma table_info(tool_errors)") if r[1] in TEXT_COLUMNS["tool_errors"]]
    errors: dict[str, list] = {}  # session -> its tool_errors rows
    if columns and "tool_errors" in have:
        for row in con.execute(f"select rowid, session_id, {', '.join(columns)} from tool_errors"):
            errors.setdefault(row[1], []).append(row)
    rows = []
    if "messages" in have:
        rows = con.execute("select rowid, session_id, text from messages order by session_id, seq").fetchall()
    for sid, group in itertools.groupby(rows, key=lambda r: r[1]):
        group = list(group)
        chat = Chat([r[2] for r in group])
        for (rowid, _, old), text in zip(group, chat.texts, strict=True):
            if text != old:
                con.execute("update messages set text = ? where rowid = ?", (text, rowid))
                changed["messages.text"] += 1
                n["messages"] += 1
        _rewrite_errors(con, columns, errors.pop(sid, []), chat, changed, n)
    chat = Chat([])
    for rows_ in errors.values():  # chats whose messages the index no longer holds
        _rewrite_errors(con, columns, rows_, chat, changed, n)
    return n


def _rewrite_errors(con, columns: list[str], rows: list, chat: Chat, changed: Counter, n: Counter) -> None:
    for row in rows:
        new = [reclean("command" if c == "command_head" else c, v, chat) for c, v in zip(columns, row[2:], strict=True)]
        diff = [c for c, a, b in zip(columns, row[2:], new, strict=True) if a != b]
        if diff:
            sets = ", ".join(f"{c} = ?" for c in diff)
            con.execute(
                f"update tool_errors set {sets} where rowid = ?", [new[columns.index(c)] for c in diff] + [row[0]]
            )
            for c in diff:
                changed[f"tool_errors.{c}"] += 1
            n["tool_errors"] += 1


def _standalone_fts(con: sqlite3.Connection, fts: str, columns: tuple[str, ...]) -> None:
    """A full-text table that keeps its own copy of the text: redact it in place, then rebuild its index."""
    sets = ", ".join(f"{c} = ?" for c in columns)
    for row in con.execute(f"select rowid, {', '.join(columns)} from {fts}").fetchall():
        new = [redact(v) if isinstance(v, str) else v for v in row[1:]]
        if new != list(row[1:]):
            con.execute(f"update {fts} set {sets} where rowid = ?", [*new, row[0]])
    con.execute(f"insert into {fts}({fts}) values ('rebuild')")


def _sessions_fts(con: sqlite3.Connection) -> None:
    """sessions_fts keeps its own copy of the title and the opening prompt."""
    have = {r[1]: (r[0], r[2], r[3]) for r in con.execute("select rowid, id, title, first_prompt from sessions_fts")}
    for sid, title, first in con.execute("select id, title, first_prompt from sessions").fetchall():
        row = have.get(sid)
        if row and row[1:] != (title, first):
            con.execute("update sessions_fts set title = ?, first_prompt = ? where rowid = ?", (title, first, row[0]))
    # an update leaves the old terms in the index's earlier segments until they merge
    con.execute("insert into sessions_fts(sessions_fts) values ('rebuild')")


def _rewrite_json(con, table: str, key: str, column: str, fix: Callable[[object], bytes | str | None]) -> int:
    """For a column that holds encoded JSON: decode, redact every string, encode again."""
    n = 0
    for row in con.execute(f"select {key}, {column} from {table}").fetchall():
        try:
            new = fix(row[-1])
        except (ValueError, zlib.error):
            continue  # not what this code wrote: leave it alone
        if new is not None:
            where = " and ".join(f"{k.strip()} = ?" for k in key.split(","))
            con.execute(f"update {table} set {column} = ? where {where}", [new, *row[:-1]])
            n += 1
    return n


def _cache_value(response: str) -> str | None:
    if not isinstance(response, str):
        return None
    data = json.loads(response)
    new = redact_json(data)
    return json.dumps(new) if new != data else None


def _archive_value(blob: bytes) -> bytes | None:
    """An archived chat: zlib-compressed JSON with the session's fields, its messages and its failed tool calls."""
    data = json.loads(zlib.decompress(blob))
    new = dict(data)
    if "title" in new:
        new["title"] = redact_json(new["title"])
    chat = Chat([m[2] for m in new["messages"]] if "messages" in new else [])  # redacted as one chat
    moved: dict[str, str] = {}  # identity of a message as it was -> as it is now (errors.identity)
    if "messages" in new:  # [role, ts, text, ...] each
        new["messages"] = [[*m[:2], t, *m[3:]] for m, t in zip(new["messages"], chat.texts, strict=True)]
        for old, m in zip(data["messages"], new["messages"], strict=True):
            if old[2] != m[2]:
                moved[identity(old[0], old[2])] = identity(m[0], m[2])
    if new.get("tool_errors"):
        new["tool_errors"] = [_archive_error(e, chat, moved) for e in new["tool_errors"]]
    if new == data:
        return None
    return zlib.compress(json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode())


def _archive_error(e, chat: Chat, moved: dict[str, str]):
    """A stored ToolError: its words redacted, and the identities of the messages it points to follow those
    messages if they changed, so a restore can still place it."""
    if not isinstance(e, dict):
        return e
    e = dict(e)
    for k in TEXT_FIELDS:
        if k in e:
            e[k] = reclean(k, e[k], chat)
    for k in ("anchor", "resolved_anchor"):
        if e.get(k) in moved:
            e[k] = moved[e[k]]
    return e
