"""The chat index: sessions and messages in SQLite with full-text search, and the queries over it."""

import contextlib
import os
import re
import sqlite3
import time
from datetime import datetime

from .. import paths
from . import archive, scrub
from .errors import ToolError, anchor, identity, reclean_errors
from .projects import resolve
from .redact import Chat, redact
from .transcript import Message, Session, conversation_fingerprint, fallback_title, opening_prompt

MAX_MESSAGE_CHARS = 20_000
# Text read past a cut so that a secret straddling it is redacted whole before the cut is made.
REDACT_MARGIN = 512
# Bumping this rebuilds the chat tables on next run: chats are re-read from their sources,
# and the ones whose source is gone come back from the archive (archive.py), which a
# rebuild never drops.
CHAT_SCHEMA_VERSION = "7"
CHAT_TABLES = (
    "sessions",
    "messages",
    "messages_fts",
    "sessions_fts",
    "seen",
    "chat_projects",
    "tool_errors",
    "tool_errors_fts",
)

SCHEMA = """
create table if not exists sessions (
  id text primary key, agent text, native_id text, project text, title text, branch text,
  started_at real, updated_at real, source text, n_messages int, first_prompt text,
  origin text, fingerprint text, project_key text, project_name text, root text
);
create index if not exists sessions_project on sessions(project_key, updated_at);
create table if not exists messages (
  id integer primary key, session_id text, seq int, role text, ts real, text text
);
create index if not exists messages_session on messages(session_id, seq);
create virtual table if not exists messages_fts using fts5(
  text, content='messages', content_rowid='id', tokenize='porter unicode61'
);
create trigger if not exists messages_ai after insert on messages begin
  insert into messages_fts(rowid, text) values (new.id, new.text);
end;
create trigger if not exists messages_ad after delete on messages begin
  insert into messages_fts(messages_fts, rowid, text) values ('delete', old.id, old.text);
end;
create virtual table if not exists sessions_fts using fts5(
  id unindexed, title, project, first_prompt, tokenize='porter unicode61'
);
create table if not exists seen (key text primary key, fingerprint text, session_id text);
create table if not exists meta (k text primary key, v text);
-- Failed tool calls (chats/errors.py): kept apart from messages, so search ranking and
-- memory extraction never see them. seq and resolved_seq are NULL unless the message is found
-- exactly once in the list written for the chat.
create table if not exists tool_errors (
  id integer primary key, session_id text, call_id text, ts real, tool text, command_head text, signature text,
  seq int,           -- the message the failure came after; NULL when it cannot be placed exactly
  resolved_seq int,  -- the message where a rerun of the identical call in this chat succeeded; same
  next_reply text    -- the start of what the agent said after the failure
);
create index if not exists tool_errors_session on tool_errors(session_id);
create virtual table if not exists tool_errors_fts using fts5(
  signature, content='tool_errors', content_rowid='id', tokenize='porter unicode61'
);
create trigger if not exists tool_errors_ai after insert on tool_errors begin
  insert into tool_errors_fts(rowid, signature) values (new.id, new.signature);
end;
create trigger if not exists tool_errors_ad after delete on tool_errors begin
  insert into tool_errors_fts(tool_errors_fts, rowid, signature) values ('delete', old.id, old.signature);
end;
"""


def connect(path: str = paths.DB) -> sqlite3.Connection:
    """Open the index, creating or rebuilding its tables as needed. ":memory:" gives a
    throwaway index, as the LongMemEval eval uses."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("create table if not exists meta (k text primary key, v text)")
    durable = path != ":memory:"
    if durable:
        archive.ensure(con)
    if _schema(con) != CHAT_SCHEMA_VERSION:
        con.execute("begin immediate")  # one process rebuilds; any other waits, then finds it done
        built_by = _schema(con)
        if built_by != CHAT_SCHEMA_VERSION:
            if durable and built_by:
                archive.snapshot(con, built_by)  # nothing the old tables hold is lost with them
            for t in CHAT_TABLES:
                con.execute(f"drop table if exists {t}")
            con.execute(
                "delete from meta where k like 'indexed_at:%' or k like 'indexed_at_root:%'"
                " or k like 'scanned:%' or k = 'projects_stamp'"
            )
            # the archive now holds copies as the old tables did: scrub.ensure rewrites them at the next index run
            con.execute("delete from meta where k = ?", (scrub.META_KEY,))
            con.execute("insert or replace into meta values ('chat_schema', ?)", (CHAT_SCHEMA_VERSION,))
        con.commit()
    con.executescript(SCHEMA)
    add_root_column(con)
    return con


def _schema(con) -> str | None:
    row = con.execute("select v from meta where k='chat_schema'").fetchone()
    return row[0] if row else None


def add_root_column(con: sqlite3.Connection) -> None:
    """An index built before roots existed has no sessions.root. Add it in place (nothing is dropped,
    so chats whose files are gone survive); the indexer fills it from each chat's source path.
    Inserts name their columns, so a column added after the ones SCHEMA lists does not break them."""
    if "root" not in {r[1] for r in con.execute("pragma table_info(sessions)")}:
        with contextlib.suppress(sqlite3.OperationalError):  # another process added it just now
            con.execute("alter table sessions add column root text")


def backfill_roots(con: sqlite3.Connection, found: list) -> int:
    """Name the root of every chat that has none yet (indexed before sessions.root existed, or restored
    from the archive) by the folder its source file sits in. Prefixes end in a separator, so ~/.claude
    never claims ~/.claude-work. Cursor transcripts of every build share ~/.cursor/projects, so they
    are attributed to the first Cursor root here and corrected when the chat is next parsed."""
    if not con.execute("select 1 from sessions where root is null limit 1").fetchone():
        return 0
    prefixes: list[tuple[str, str]] = []
    for r in found:
        base = r.path.rstrip(os.sep) + os.sep
        if r.agent == "claude":
            prefixes.append((base + "projects" + os.sep, r.name))
        elif r.agent == "codex":
            prefixes += [(base + "sessions" + os.sep, r.name), (base + "archived_sessions" + os.sep, r.name)]
        elif r.agent == "cursor":
            prefixes.append((base + "projects" + os.sep, r.name))
            if r.data:
                prefixes.append((os.path.join(r.data, "globalStorage", "state.vscdb") + "#", r.name))
    n = 0
    seen = set()
    for prefix, name in prefixes:
        if prefix in seen:  # the first root to claim a folder keeps it
            continue
        seen.add(prefix)
        n += con.execute(
            "update sessions set root = ? where root is null and substr(source, 1, ?) = ?",
            (name, len(prefix), prefix),
        ).rowcount
    con.commit()
    return n


def reparse_shared_cursor(con: sqlite3.Connection, found: list) -> None:
    """With more than one Cursor build, which one owns a transcript is read from the build's own
    database when the chat is parsed. Chats indexed before roots existed are unattributed, and a file
    that has not changed is never parsed again, so forget that it was seen and let this run read it."""
    if sum(1 for r in found if r.agent == "cursor" and r.data) < 2:
        return
    con.execute(
        "delete from seen where session_id in (select id from sessions where root is null"
        " and source like '%/agent-transcripts/%')"
    )


def upsert(con: sqlite3.Connection, s: Session) -> None:
    """Write a parsed chat to the index, and its file's copy to the archive.

    The shrink guard: if this file's earlier copy holds messages the parse no longer has
    (the file was compacted or rewritten), they are kept, marked Kept; archive.merge has the
    rule. Its tool errors are kept the same way. The index then holds the union of the
    chat's copies from every file it lives in. A forgotten chat is not written."""
    if archive.enabled(con) and con.execute("select 1 from forgotten_chats where id = ?", (s.id,)).fetchone():
        return
    keep = [i for i, m in enumerate(s.messages) if m.text.strip()]
    # The chat's texts go through redaction together, so a key pasted again further down, bare, goes too,
    # in the words of its failed tool calls as well.
    chat = Chat([s.messages[i].text[: MAX_MESSAGE_CHARS + REDACT_MARGIN] for i in keep])
    stored = [
        Message(s.messages[i].role, t[:MAX_MESSAGE_CHARS], s.messages[i].ts)
        for i, t in zip(keep, chat.texts, strict=True)
    ]
    errs = archive.merge_errors(anchor(s.tool_errors, s.messages, keep, stored, chat))  # a resumed chat repeats a call
    if not archive.enabled(con):
        _write(con, s, s.messages, stored, errs, chat)
        return
    others, other_errs = [], []
    msgs = stored
    size = archive.source_size(s.source)
    for row in archive.copies(con, s.id):
        copy = archive.unpack(row["data"])
        if row["source"] != s.source:
            if archive.kind(row["source"]) == archive.kind(s.source):
                others.append(archive.messages(row, CHAT_SCHEMA_VERSION))
                other_errs.append(copy.tool_errors)
            continue
        # A copy from another parser version may differ for reasons that have nothing to do
        # with the file, so only what it marked Kept carries over, unless the file shrank
        # since: a rewrite.
        shrank = (size or 0) < (row["source_size"] or 0)
        same = row["parser"] == CHAT_SCHEMA_VERSION or shrank
        msgs = archive.merge(archive.messages(row, CHAT_SCHEMA_VERSION), stored, only_kept=not same)
        if (n := archive.kept(msgs)) > (row["kept"] or 0):
            archive.log(f"kept {n} messages of {s.id} that a re-read of {s.source} no longer has")
        if same:  # another parser may word a signature differently: keep only this one's
            # read again, as the messages of the copy are: the copy may predate a rule, or this chat's values
            errs = archive.merge_errors(reclean_errors(copy.tool_errors, chat), errs)
    archive.save(con, s, msgs, CHAT_SCHEMA_VERSION, size, errs)
    shown = archive.union(msgs, others)
    if shown is stored:
        shown_chat = chat
        parsed = s.messages
    else:
        shown, shown_chat = _redacted(shown)
        parsed = shown
    # The other files' errors are read again with the chat as shown, and before they are merged: a call
    # that has no id is told from another by its words, so a stale copy of a call must not outlive its fresh one.
    shown_errs = (
        archive.merge_errors(*[reclean_errors(e, shown_chat) for e in other_errs], errs) if other_errs else errs
    )
    _write(con, s, parsed, shown, shown_errs, shown_chat)


def restore(con: sqlite3.Connection, agents: list[str]) -> int:
    """Put back archived chats of these agents that no source yielded on the last full
    read, marked restored. Returns how many."""
    if not agents or not archive.enabled(con):
        return 0
    ids = [
        r[0]
        for r in con.execute(
            f"""select distinct id from chat_archive a where agent in ({",".join("?" * len(agents))})
                and not exists (select 1 from sessions s where s.id = a.id)
                and not exists (select 1 from forgotten_chats f where f.id = a.id)""",
            agents,
        )
    ]
    now = time.time()
    for sid in ids:
        rows = archive.copies(con, sid)
        latest = rows[-1]  # the copy a source yielded most recently
        s = archive.unpack(latest["data"])
        same_kind = [r for r in rows[:-1] if archive.kind(r["source"]) == archive.kind(latest["source"])]
        mine = archive.messages(latest, CHAT_SCHEMA_VERSION)
        shown = archive.union(mine, [archive.messages(r, CHAT_SCHEMA_VERSION) for r in same_kind])
        shown, chat = _redacted(shown)
        errs = archive.merge_errors(
            *[reclean_errors(archive.unpack(r["data"]).tool_errors, chat) for r in same_kind],
            reclean_errors(s.tool_errors, chat),
        )  # a copy's failed calls are read again with its messages
        _write(con, s, shown, shown, errs, chat)
        con.execute("update chat_archive set restored_at = coalesce(restored_at, ?) where id = ?", (now, sid))
    if ids:
        con.commit()
        archive.log(f"restored {len(ids)} chat(s) from the archive: no source has them any more")
    return len(ids)


def _redacted(msgs: list[Message]) -> tuple[list[Message], Chat]:
    """Messages put back from the archive, redacted as one chat, and the chat (its values redact the words of a
    failed call that comes with them)."""
    chat = Chat([m.text for m in msgs])
    return [type(m)(m.role, t, m.ts) for m, t in zip(msgs, chat.texts, strict=True)], chat


def _write(
    con: sqlite3.Connection,
    s: Session,
    parsed: list[Message],
    stored: list[Message],
    errors: list[ToolError],
    chat: Chat,
) -> None:
    """The index rows for one chat. parsed: what the fingerprint comes from; stored: the message rows, already
    truncated and redacted, which the opening prompt and a fallback title are cut from (never from the raw parse,
    where a repeat of a key is still bare); errors: its failed tool calls, redacted; chat: what redacted the
    messages, for a title the agent gave."""
    con.execute("delete from messages where session_id=?", (s.id,))
    con.execute("delete from sessions_fts where id=?", (s.id,))
    con.execute("delete from tool_errors where session_id=?", (s.id,))
    where: dict[str, list[int]] = {}
    for i, m in enumerate(stored):
        where.setdefault(identity(m.role, m.text), []).append(i)

    def seq(who):
        hits = where.get(who) if who else None
        return hits[0] if hits and len(hits) == 1 else None  # a message said twice is no place

    con.executemany(
        "insert into tool_errors(session_id, call_id, ts, tool, command_head, signature, seq, resolved_seq, next_reply)"
        " values (?,?,?,?,?,?,?,?,?)",
        [
            (s.id, e.call_id, e.ts, e.tool, e.command, e.signature, seq(e.anchor), seq(e.resolved_anchor), e.next_reply)
            for e in errors
        ],
    )
    msgs = [m for m in parsed if m.text.strip()]
    first_prompt = opening_prompt((m.role, m.text) for m in stored)
    title = chat.repeats(redact(s.title)) if s.title else fallback_title(first_prompt)
    project = resolve(s.project)
    row = {  # named, so a column a later release adds does not break this build's insert
        "id": s.id,
        "agent": s.agent,
        "native_id": s.native_id,
        "project": s.project,
        "title": title,
        "branch": s.branch,
        "started_at": s.started_at,
        "updated_at": s.updated_at,
        "source": s.source,
        "n_messages": len(stored),
        "first_prompt": first_prompt,
        "origin": s.origin,
        "fingerprint": conversation_fingerprint(msgs),
        "project_key": project.key if project else None,
        "project_name": project.name if project else None,
        "root": s.root,
    }
    con.execute(
        f"insert or replace into sessions ({', '.join(row)}) values ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    con.executemany(
        "insert into messages(session_id, seq, role, ts, text) values (?,?,?,?,?)",
        [(s.id, i, m.role, m.ts, m.text) for i, m in enumerate(stored)],
    )
    con.execute(
        "insert into sessions_fts (id, title, project, first_prompt) values (?,?,?,?)",
        (s.id, title, s.project or "", first_prompt),
    )


# --- querying ---------------------------------------------------------------

_SINCE = re.compile(r"^(\d+)\s*([hdwmy])$")


def parse_since(since: str | None) -> float | None:
    if not since:
        return None
    m = _SINCE.match(since.strip().lower())
    if m:
        unit = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000, "y": 31536000}[m.group(2)]
        return time.time() - int(m.group(1)) * unit
    return datetime.fromisoformat(since).timestamp()


# Words that say nothing about which chat is meant. They are dropped from a query once two
# others remain, so "what did we decide about retries" searches for "decide retries".
STOPWORDS = frozenset(
    "the a an and or but to of in on for with this that it is are be can we you i me my our your so do does "
    "did done how what why when where which who whom whose there here from at as by if not no yes please just "
    "like want need make use using have has had will would could should about into then than them they their "
    "also all any some more most very get got let lets one was were been being am its it's i'm i've i'd".split()
)


def fts_query(text: str, mode: str) -> str | None:
    words = list(dict.fromkeys(re.findall(r"\w+", text.lower())))
    if not words:
        return None
    content = [w for w in words if w not in STOPWORDS]
    words = content if len(content) >= 2 else words
    return (" OR " if mode == "or" else " ").join(f'"{w}"' for w in words)


def _filters(con, agent, project, since, alias="s", *, until=None, exclude=None, where=None):
    sql, args = "", []
    if agent:
        sql += f" and {alias}.agent = ?"
        args.append(agent.lower())
    if project:
        from .. import projects

        found = projects.find(project)
        if found:  # a project from projects.yaml: every folder it spans, and chats placed by its words
            projects.refresh(con)
            sql += f" and {alias}.id in (select session_id from chat_projects where project = ?)"
            args.append(found.id)
        else:
            sql += f" and ({alias}.project like ? or {alias}.title like ?)"
            args += [f"%{project}%", f"%{project}%"]
    ts = parse_since(since)
    if ts:
        sql += f" and {alias}.updated_at >= ?"
        args.append(ts)
    end = parse_until(until)
    if end:
        sql += f" and coalesce({alias}.started_at, {alias}.updated_at) <= ?"
        args.append(end)
    if exclude:
        sql += f" and {alias}.id not in ({','.join('?' * len(exclude))})"
        args += list(exclude)
    if where:
        sql += " " + where[0]
        args += list(where[1])
    return sql, args


def parse_until(until: str | None) -> float | None:
    """An end date: "7d" means up to a week ago; a bare ISO date includes that whole day."""
    if not until:
        return None
    if _SINCE.match(until.strip().lower()):
        return parse_since(until)
    end = datetime.fromisoformat(until)
    if len(until.strip()) == 10:  # a date with no time: the end of that day
        end = end.replace(hour=23, minute=59, second=59)
    return end.timestamp()


def fmt_time(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def session_row(r: sqlite3.Row) -> dict:
    return {
        "chat_id": r["id"],
        "agent": r["agent"],
        "title": r["title"],
        "project": r["project"],
        "branch": r["branch"],
        "updated": fmt_time(r["updated_at"]),
        "messages": r["n_messages"],
    }


# Reciprocal rank fusion (Cormack et al., 2009) of up to five rankings of each chat: its best
# message, the summed BM25 of its best few messages, the title an agent gave it, a message
# holding every query word, and, when embeddings are on, its message closest in meaning.
# Weights were tuned on LongMemEval and on personal search evals together; see
# docs/research/retrieval-benchmarks.md.
FUSION_K = 20
WEIGHTS = {"best": 0.5, "sum": 1.0, "title": 2.0, "all": 1.0, "dense": 1.0}
TOP_HITS = 3  # messages per chat summed, and returned as hits
POOL = 2000  # matching messages considered per query
# A title is strong evidence for a query about as long as a title, and weak evidence for a
# long one, such as a pasted issue or error: nearly every title shares a word with it, so the
# title ranking would mostly reward chats for having a title. Past this many query words its
# weight falls with the fourth power of the length, so it is mostly gone by twice a title's
# length (on SWE-ContextBench's issue texts, a full title weight cost more than 20 points of
# hit@5).
TITLE_WORDS = 12


def search(
    con,
    query: str,
    agent=None,
    project=None,
    since=None,
    limit=8,
    until=None,
    exclude=None,
    vector=None,
    explain: dict | None = None,
    where: tuple[str, list] | None = None,
) -> list[dict]:
    """Rank chats for a query, each with its best few matching messages.

    since / until: only chats active in that window, e.g. since="2w" or until="2026-09-30".
    exclude: chat ids to leave out, such as ones already read.
    vector: the query's embedding; by default recall embeds the query itself when embeddings
    are on (see embeddings.py). False searches by keywords only.
    explain: a dict to fill with each chat's fused score, the separate rankings and their
    weights, for callers that judge how sure the ranking is (jit.py).
    where: (SQL starting with "and" over the sessions table as `s`, its arguments) that narrows
    the chats before any ranking, so that the ones left out cannot take a place or a margin.
    """
    q = fts_query(query, "or")
    if not q:
        return []
    if vector is None:
        from .. import embeddings

        vector = embeddings.query_vector(query)
    fsql, fargs = _filters(con, agent, project, since, until=until, exclude=exclude, where=where)
    msql, margs = _message_window(since, until)
    rows = con.execute(
        f"""select m.id mid, m.session_id sid, m.seq seq, bm25(messages_fts) score
            from messages_fts join messages m on m.id = messages_fts.rowid
            join sessions s on s.id = m.session_id
            where messages_fts match ? {fsql} {msql}
            order by score limit ?""",
        [q, *fargs, *margs, POOL],
    ).fetchall()
    hits: dict[str, list] = {}
    for r in rows:  # best first, so each chat's list is in rank order
        hits.setdefault(r["sid"], []).append(r)
    by_best = list(hits)
    by_sum = sorted(hits, key=lambda sid: sum(h["score"] for h in hits[sid][:TOP_HITS]))
    # Only titles an agent wrote: an untitled chat's "title" is its opening message, which the
    # message rankings already count, and counting it twice cost 7 points on LongMemEval.
    by_title = [
        r["sid"]
        for r in con.execute(
            f"""select f.id sid from sessions_fts f join sessions s on s.id = f.id
                where sessions_fts match ? {fsql}
                and s.title != substr(replace(s.first_prompt, char(10), ' '), 1, 80)
                order by bm25(sessions_fts, 0, 10.0, 2.0, 3.0) limit 200""",
            [q, *fargs],
        )
    ]
    # Chats with one message that holds every word: the strongest sign for a short query.
    every = fts_query(query, "and")
    by_all = []
    if every and every != q and WEIGHTS["all"]:
        for r in con.execute(
            f"""select m.session_id sid from messages_fts join messages m on m.id = messages_fts.rowid
                join sessions s on s.id = m.session_id
                where messages_fts match ? {fsql} {msql}
                order by bm25(messages_fts) limit ?""",
            [every, *fargs, *margs, POOL],
        ):
            if r["sid"] not in by_all:
                by_all.append(r["sid"])
    by_dense = []
    if vector is not None and vector is not False and WEIGHTS["dense"]:
        from .. import embeddings

        by_dense = embeddings.ranking(con, vector, fsql, fargs, msql, margs, 200)
    weights = dict(WEIGHTS, title=WEIGHTS["title"] * min(1.0, TITLE_WORDS / (q.count(" OR ") + 1)) ** 4)
    scores: dict[str, float] = {}
    rankings = (("best", by_best), ("sum", by_sum), ("title", by_title), ("all", by_all), ("dense", by_dense))
    for name, ranking in rankings:
        for rank, sid in enumerate(ranking):
            scores[sid] = scores.get(sid, 0) + weights[name] / (FUSION_K + rank + 1)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if explain is not None:
        explain.update(
            scores=scores,
            rankings=dict(rankings),
            weights=weights,
            bm25={sid: h[0]["score"] for sid, h in hits.items()},
        )
    if not top:
        return []
    sessions = {
        r["id"]: r for r in con.execute(f"select * from sessions where id in ({','.join('?' * len(top))})", top)
    }
    snippets = _snippets(con, q, [h["mid"] for sid in top for h in hits.get(sid, [])[:TOP_HITS]])
    out = []
    for sid in top:
        mine = hits.get(sid, [])
        found = [{"seq": h["seq"], "snippet": snippets.get(h["mid"], "")} for h in mine[:TOP_HITS]]
        d = session_row(sessions[sid])
        d["matches"] = len(mine)
        d["snippet"] = found[0]["snippet"] if found else sessions[sid]["first_prompt"][:200]
        d["match_at"] = found[0]["seq"] if found else None
        d["hits"] = found
        out.append(d)
    return out


def _message_window(since, until) -> tuple[str, list]:
    """Keep message hits inside a date window too, for chats that span it."""
    sql, args = "", []
    if ts := parse_since(since):
        sql += " and (m.ts is null or m.ts >= ?)"
        args.append(ts)
    if end := parse_until(until):
        sql += " and (m.ts is null or m.ts <= ?)"
        args.append(end)
    return sql, args


def _snippets(con, q: str, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = con.execute(
        f"""select rowid, snippet(messages_fts, 0, '«', '»', '…', 24) snip from messages_fts
            where messages_fts match ? and rowid in ({",".join("?" * len(ids))})""",
        [q, *ids],
    )
    return {r["rowid"]: r["snip"] for r in rows}


def recent(con, agent=None, project=None, since=None, limit=15, until=None) -> list[dict]:
    fsql, fargs = _filters(con, agent, project, since, until=until)
    rows = con.execute(
        f"select * from sessions s where n_messages > 0 {fsql} order by updated_at desc limit ?",
        [*fargs, limit],
    )
    return [session_row(r) | {"first_prompt": r["first_prompt"][:200]} for r in rows]


def resolve_id(con, chat_id: str) -> str | None:
    prefix = chat_id.split(":", 1)[-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = con.execute(
        "select id from sessions where id = ? or native_id = ? or native_id like ? escape '\\' limit 2",
        (chat_id, chat_id, prefix),
    ).fetchall()
    exact = [r["id"] for r in rows if r["id"] == chat_id or r["id"].endswith(":" + chat_id)]
    if exact:
        return exact[0]
    return rows[0]["id"] if len(rows) == 1 else None  # ambiguous prefixes resolve to nothing


def read(con, chat_id: str, query=None, offset=None, limit=30, max_chars=2000) -> dict | None:
    sid = resolve_id(con, chat_id)
    if not sid:
        return None
    s = con.execute("select * from sessions where id=?", (sid,)).fetchone()
    if offset is None and query and (q := fts_query(query, "or")):
        row = con.execute(
            """select m.seq from messages_fts join messages m on m.id = messages_fts.rowid
               where messages_fts match ? and m.session_id = ?
               order by bm25(messages_fts) limit 1""",
            (q, sid),
        ).fetchone()
        offset = max(0, row["seq"] - limit // 3) if row else 0
    offset = offset or 0
    rows = con.execute(
        "select seq, role, ts, text from messages where session_id=? and seq>=? order by seq limit ?",
        (sid, offset, limit),
    ).fetchall()
    msgs = []
    for r in rows:
        text = r["text"]
        if len(text) > max_chars:
            text = text[:max_chars] + f"… [{len(r['text']) - max_chars} more chars]"
        msgs.append({"seq": r["seq"], "role": r["role"], "time": fmt_time(r["ts"]), "text": text})
    end = offset + len(rows)
    out = session_row(s) | {
        "source": s["source"],
        "showing": f"{offset}-{end - 1} of {s['n_messages']}" if rows else "none",
        "next_offset": end if end < s["n_messages"] else None,
        "transcript": msgs,
    }
    arch = (
        archive.enabled(con)
        and con.execute(
            "select min(restored_at) restored, count(restored_at) = count(*) gone, sum(kept) kept"
            " from chat_archive where id = ?",
            (sid,),
        ).fetchone()
    )
    if arch and arch["gone"] and arch["restored"]:
        when = fmt_time(arch["restored"])
        out["archived"] = f"no source has this chat any more; restored from recall's archive {when}"
    elif arch and arch["kept"]:
        out["archived"] = (
            f"{arch['kept']} of these messages are gone from the chat's files and kept in recall's archive"
        )
    return out


def tool_errors(con, text: str, limit: int = 5, exclude: list[str] | None = None) -> list[dict]:
    """Past failed tool calls whose error signature matches text, best first: where the
    same error came up before, the message it came after (seq, or None when that cannot be
    placed exactly), the message where a later run of the identical call succeeded
    (resolved_seq, or None), and what the agent said next."""
    q = fts_query(text, "or")
    if not q:
        return []
    skip = f"and e.session_id not in ({','.join('?' * len(exclude))})" if exclude else ""
    rows = con.execute(
        f"""select e.session_id, e.seq, e.ts, e.tool, e.command_head, e.signature, e.resolved_seq, e.next_reply
            from tool_errors_fts join tool_errors e on e.id = tool_errors_fts.rowid
            where tool_errors_fts match ? {skip} order by bm25(tool_errors_fts) limit ?""",
        [q, *(exclude or []), limit],
    )
    return [
        {
            "chat_id": r[0],
            "seq": r[1],
            "time": fmt_time(r[2]),
            "tool": r[3],
            "command": r[4],
            "signature": r[5],
            "resolved_seq": r[6],
            "next_reply": r[7],
        }
        for r in rows
    ]


def stats(con) -> dict:
    rows = con.execute("select agent, count(*) n, sum(n_messages) m, max(updated_at) last from sessions group by agent")
    out = {r["agent"]: {"chats": r["n"], "messages": r["m"], "latest": fmt_time(r["last"])} for r in rows}
    if archive.enabled(con):
        for r in con.execute(
            """select agent, count(*) n, sum(gone) restored, sum(kept) kept from (
                 select id, agent, count(restored_at) = count(*) gone, sum(kept) kept from chat_archive group by id
               ) group by agent"""
        ):
            if r["agent"] in out:
                out[r["agent"]] |= {"archived": r["n"], "restored": r["restored"], "kept_messages": r["kept"] or 0}
    return out
