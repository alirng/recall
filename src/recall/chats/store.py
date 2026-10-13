"""The chat index: sessions and messages in SQLite with full-text search, and the queries over it."""

import os
import re
import sqlite3
import time
from datetime import datetime

from .. import paths
from . import archive
from .projects import resolve
from .redact import redact
from .transcript import Message, Session, conversation_fingerprint

MAX_MESSAGE_CHARS = 20_000
# Bumping this rebuilds the chat tables on next run: chats are re-read from their sources,
# and the ones whose source is gone come back from the archive (archive.py), which a
# rebuild never drops.
CHAT_SCHEMA_VERSION = "2"
CHAT_TABLES = ("sessions", "messages", "messages_fts", "sessions_fts", "seen", "chat_projects")

SCHEMA = """
create table if not exists sessions (
  id text primary key, agent text, native_id text, project text, title text, branch text,
  started_at real, updated_at real, source text, n_messages int, first_prompt text,
  origin text, fingerprint text, project_key text, project_name text
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
            con.execute("delete from meta where k like 'indexed_at:%' or k like 'scanned:%' or k = 'projects_stamp'")
            con.execute("insert or replace into meta values ('chat_schema', ?)", (CHAT_SCHEMA_VERSION,))
        con.commit()
    con.executescript(SCHEMA)
    return con


def _schema(con) -> str | None:
    row = con.execute("select v from meta where k='chat_schema'").fetchone()
    return row[0] if row else None


def upsert(con: sqlite3.Connection, s: Session) -> None:
    """Write a parsed chat to the index, and its file's copy to the archive.

    The shrink guard: if this file's earlier copy holds messages the parse no longer has
    (the file was compacted or rewritten), they are kept, marked Kept; archive.merge has the
    rule. The index then holds the union of the chat's copies from every file it lives in."""
    stored = [Message(m.role, redact(m.text[:MAX_MESSAGE_CHARS]), m.ts) for m in s.messages if m.text.strip()]
    if not archive.enabled(con):
        _write(con, s, s.messages, stored)
        return
    others = []
    msgs = stored
    size = archive.source_size(s.source)
    for row in archive.copies(con, s.id):
        if row["source"] != s.source:
            if archive.kind(row["source"]) == archive.kind(s.source):
                others.append(archive.unpack(row["data"]).messages)
            continue
        # A copy from another parser version may differ for reasons that have nothing to do
        # with the file, so only what it marked Kept carries over, unless the file shrank
        # since: a rewrite.
        shrank = (size or 0) < (row["source_size"] or 0)
        same = row["parser"] == CHAT_SCHEMA_VERSION or shrank
        msgs = archive.merge(archive.unpack(row["data"]).messages, stored, only_kept=not same)
        if (n := archive.kept(msgs)) > (row["kept"] or 0):
            archive.log(f"kept {n} messages of {s.id} that a re-read of {s.source} no longer has")
    archive.save(con, s, msgs, CHAT_SCHEMA_VERSION, size)
    shown = archive.union(msgs, others)
    if shown is stored:
        _write(con, s, s.messages, stored)
    else:
        _write(con, s, shown, shown)


def restore(con: sqlite3.Connection, agents: list[str]) -> int:
    """Put back archived chats of these agents that no source yielded on the last full
    read, marked restored. Returns how many."""
    if not agents or not archive.enabled(con):
        return 0
    ids = [
        r[0]
        for r in con.execute(
            f"""select distinct id from chat_archive a where agent in ({",".join("?" * len(agents))})
                and not exists (select 1 from sessions s where s.id = a.id)""",
            agents,
        )
    ]
    now = time.time()
    for sid in ids:
        rows = archive.copies(con, sid)
        latest = rows[-1]  # the copy a source yielded most recently
        s = archive.unpack(latest["data"])
        same_kind = [r for r in rows[:-1] if archive.kind(r["source"]) == archive.kind(latest["source"])]
        shown = archive.union(s.messages, [archive.unpack(r["data"]).messages for r in same_kind])
        _write(con, s, shown, shown)
        con.execute("update chat_archive set restored_at = coalesce(restored_at, ?) where id = ?", (now, sid))
    if ids:
        con.commit()
        archive.log(f"restored {len(ids)} chat(s) from the archive: no source has them any more")
    return len(ids)


def _write(con: sqlite3.Connection, s: Session, parsed: list[Message], stored: list[Message]) -> None:
    """The index rows for one chat. parsed: what the title and opening prompt come from;
    stored: the message rows, already truncated and redacted."""
    con.execute("delete from messages where session_id=?", (s.id,))
    con.execute("delete from sessions_fts where id=?", (s.id,))
    msgs = [m for m in parsed if m.text.strip()]
    first_prompt = next((m.text for m in msgs if m.role == "user"), "")[:500]
    title = s.title or first_prompt[:80].replace("\n", " ")
    project = resolve(s.project)
    con.execute(
        "insert or replace into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            s.id,
            s.agent,
            s.native_id,
            s.project,
            title,
            s.branch,
            s.started_at,
            s.updated_at,
            s.source,
            len(stored),
            redact(first_prompt),
            s.origin,
            conversation_fingerprint(msgs),
            project.key if project else None,
            project.name if project else None,
        ),
    )
    con.executemany(
        "insert into messages(session_id, seq, role, ts, text) values (?,?,?,?,?)",
        [(s.id, i, m.role, m.ts, m.text) for i, m in enumerate(stored)],
    )
    con.execute(
        "insert into sessions_fts values (?,?,?,?)",
        (s.id, title, s.project or "", redact(first_prompt)),
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


def _filters(con, agent, project, since, alias="s", *, until=None, exclude=None):
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
    con, query: str, agent=None, project=None, since=None, limit=8, until=None, exclude=None, vector=None
) -> list[dict]:
    """Rank chats for a query, each with its best few matching messages.

    since / until: only chats active in that window, e.g. since="2w" or until="2026-09-30".
    exclude: chat ids to leave out, such as ones already read.
    vector: the query's embedding; by default recall embeds the query itself when embeddings
    are on (see embeddings.py). False searches by keywords only.
    """
    q = fts_query(query, "or")
    if not q:
        return []
    if vector is None:
        from .. import embeddings

        vector = embeddings.query_vector(query)
    fsql, fargs = _filters(con, agent, project, since, until=until, exclude=exclude)
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
    rows = con.execute(
        "select id from sessions where id = ? or native_id = ? or native_id like ? limit 2",
        (chat_id, chat_id, chat_id.split(":", 1)[-1] + "%"),
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
