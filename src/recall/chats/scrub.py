"""Rewrite the text recall keeps with today's redaction rules.

The chat tables are derived: a change to redact.py reaches them when CHAT_SCHEMA_VERSION
bumps and they are rebuilt from the agents' files. Some tables hold chat text that no
rebuild reproduces, so a secret that an older rule let through stays in them:

* candidates (the quotes and notes a model drew from chats) and llm_cache (its answers),
* the archive's copies of chats whose files the agent has since deleted (chat_archive),
* tool_errors, where a failed command's text lives, exploration reports (explore.py) and the
  asks that commit links keep beside an edit (commits.py).

ensure() runs at the start of an index run and rewrites those tables once per
redact.VERSION; run() is the same rewrite without the version check, for a one-off clean.
The messages and sessions tables are rewritten too, so a rule change takes effect without
waiting for a rebuild.
"""

import itertools
import json
import sqlite3
import zlib
from collections import Counter
from collections.abc import Callable

from .redact import VERSION, redact, redact_json, redact_many

# table -> the columns that hold chat text. Missing tables and columns are skipped.
TEXT_COLUMNS = {
    "messages": ("text",),
    "sessions": ("title", "first_prompt"),
    "candidates": ("title", "body", "why", "evidence"),
    "extractions": ("error",),
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
    version is stamped last, so a busy database means the next run tries again."""
    try:
        if stale(con):
            from .. import forget

            run(con, stamp=False, log=log)
            forget.scrub_db(con)  # the old words are also in free pages and the log until the file is rewritten
            con.execute("insert or replace into meta values (?, ?)", (META_KEY, VERSION))
            con.commit()
    except sqlite3.OperationalError:
        pass


def run(con: sqlite3.Connection, stamp: bool = True, log: Callable[[str], None] | None = None) -> Counter:
    """Redact the text in every table that holds it. Returns the number of values changed per table.column."""
    con.execute("pragma secure_delete = on")  # a rewritten value is zeroed, not left in a free page
    changed: Counter = Counter()
    have = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    for table, wanted in TEXT_COLUMNS.items():
        if table not in have:
            continue
        columns = [r[1] for r in con.execute(f"pragma table_info({table})") if r[1] in wanted]
        n = _rewrite_chats(con, changed) if table == "messages" else _rewrite(con, table, columns, changed)
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


def _rewrite(con, table: str, columns: list[str], changed: Counter) -> int:
    if not columns:
        return 0
    select = ", ".join(columns)
    n = 0
    for row in con.execute(f"select rowid, {select} from {table}").fetchall():
        new = [redact(v) if isinstance(v, str) else v for v in row[1:]]
        diff = [c for c, a, b in zip(columns, row[1:], new, strict=True) if a != b]
        if diff:
            sets = ", ".join(f"{c} = ?" for c in diff)
            con.execute(f"update {table} set {sets} where rowid = ?", [new[columns.index(c)] for c in diff] + [row[0]])
            for c in diff:
                changed[f"{table}.{c}"] += 1
            n += 1
    return n


def _rewrite_chats(con, changed: Counter) -> int:
    """The messages, a chat at a time, so a key repeated bare further down the chat goes with the one a word marked."""
    n = 0
    rows = con.execute("select rowid, session_id, text from messages order by session_id, seq").fetchall()
    for _, group in itertools.groupby(rows, key=lambda r: r[1]):
        group = list(group)
        for (rowid, _, old), text in zip(group, redact_many([r[2] for r in group]), strict=True):
            if text != old:
                con.execute("update messages set text = ? where rowid = ?", (text, rowid))
                changed["messages.text"] += 1
                n += 1
    return n


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
    """An archived chat: zlib-compressed JSON with the session's fields and its messages."""
    data = json.loads(zlib.decompress(blob))
    new = dict(data)
    if "title" in new:
        new["title"] = redact_json(new["title"])
    if "messages" in new:  # [role, ts, text, ...] each; redacted as one chat
        texts = redact_many([m[2] for m in new["messages"]])
        new["messages"] = [[*m[:2], t, *m[3:]] for m, t in zip(new["messages"], texts, strict=True)]
    if "tool_errors" in new:
        new["tool_errors"] = redact_json(new["tool_errors"])
    if new == data:
        return None
    return zlib.compress(json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode())
