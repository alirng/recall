"""The chat index: sessions and messages in SQLite with full-text search, and the queries over it."""

import os
import re
import sqlite3
import time
from datetime import datetime

from .. import paths
from .projects import resolve
from .redact import redact
from .transcript import Session, conversation_fingerprint

MAX_MESSAGE_CHARS = 20_000
# The chat index is derived data: bumping this drops and rebuilds it on next run.
CHAT_SCHEMA_VERSION = "2"
CHAT_TABLES = ("sessions", "messages", "messages_fts", "sessions_fts", "seen")

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("create table if not exists meta (k text primary key, v text)")
    row = con.execute("select v from meta where k='chat_schema'").fetchone()
    if not row or row[0] != CHAT_SCHEMA_VERSION:
        for t in CHAT_TABLES:
            con.execute(f"drop table if exists {t}")
        con.execute("delete from meta where k like 'indexed_at:%'")
        con.execute("insert or replace into meta values ('chat_schema', ?)", (CHAT_SCHEMA_VERSION,))
    con.executescript(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, s: Session) -> None:
    con.execute("delete from messages where session_id=?", (s.id,))
    con.execute("delete from sessions_fts where id=?", (s.id,))
    msgs = [m for m in s.messages if m.text.strip()]
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
            len(msgs),
            redact(first_prompt),
            s.origin,
            conversation_fingerprint(msgs),
            project.key if project else None,
            project.name if project else None,
        ),
    )
    con.executemany(
        "insert into messages(session_id, seq, role, ts, text) values (?,?,?,?,?)",
        [(s.id, i, m.role, m.ts, redact(m.text[:MAX_MESSAGE_CHARS])) for i, m in enumerate(msgs)],
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


def fts_query(text: str, mode: str) -> str | None:
    words = re.findall(r"\w+", text.lower())
    if not words:
        return None
    return (" OR " if mode == "or" else " ").join(f'"{w}"' for w in words)


def _filters(agent, project, since, alias="s"):
    sql, args = "", []
    if agent:
        sql += f" and {alias}.agent = ?"
        args.append(agent.lower())
    if project:
        sql += f" and ({alias}.project like ? or {alias}.title like ?)"
        args += [f"%{project}%", f"%{project}%"]
    ts = parse_since(since)
    if ts:
        sql += f" and {alias}.updated_at >= ?"
        args.append(ts)
    return sql, args


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


def search(con, query: str, agent=None, project=None, since=None, limit=8) -> list[dict]:
    """Rank chats by fusing message-level and title-level full-text matches."""
    fsql, fargs = _filters(agent, project, since)
    scores: dict[str, float] = {}
    hits: dict[str, dict] = {}
    for weight, mode in ((1.0, "and"), (0.5, "or")):
        q = fts_query(query, mode)
        if not q:
            return []
        rows = con.execute(
            f"""select m.session_id sid, m.seq seq,
                       snippet(messages_fts, 0, '«', '»', '…', 24) snip
                from messages_fts join messages m on m.id = messages_fts.rowid
                join sessions s on s.id = m.session_id
                where messages_fts match ? {fsql}
                order by bm25(messages_fts) limit 400""",
            [q, *fargs],
        ).fetchall()
        rank = 0
        for r in rows:
            h = hits.setdefault(r["sid"], {"count": 0, "snippet": r["snip"], "seq": r["seq"], "mode": mode})
            if h["mode"] != mode:
                continue  # already counted by the stricter pass
            h["count"] += 1
            if h["count"] == 1:
                scores[r["sid"]] = scores.get(r["sid"], 0) + weight / (20 + rank)
                rank += 1
        trows = con.execute(
            f"""select f.id sid from sessions_fts f join sessions s on s.id = f.id
                where sessions_fts match ? {fsql}
                order by bm25(sessions_fts, 0, 10.0, 2.0, 3.0) limit 100""",
            [q, *fargs],
        ).fetchall()
        for rank, r in enumerate(trows):
            scores[r["sid"]] = scores.get(r["sid"], 0) + 1.5 * weight / (20 + rank)
        if len(scores) >= limit * 2:
            break
    for sid, h in hits.items():
        scores[sid] += 0.002 * min(h["count"], 20)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if not top:
        return []
    rows = {r["id"]: r for r in con.execute(f"select * from sessions where id in ({','.join('?' * len(top))})", top)}
    out = []
    for sid in top:
        d = session_row(rows[sid])
        h = hits.get(sid)
        d["matches"] = h["count"] if h else 0
        d["snippet"] = h["snippet"] if h else rows[sid]["first_prompt"][:200]
        d["match_at"] = h["seq"] if h else None
        out.append(d)
    return out


def recent(con, agent=None, project=None, since=None, limit=15) -> list[dict]:
    fsql, fargs = _filters(agent, project, since)
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
    return session_row(s) | {
        "source": s["source"],
        "showing": f"{offset}-{end - 1} of {s['n_messages']}" if rows else "none",
        "next_offset": end if end < s["n_messages"] else None,
        "transcript": msgs,
    }


def stats(con) -> dict:
    rows = con.execute("select agent, count(*) n, sum(n_messages) m, max(updated_at) last from sessions group by agent")
    return {r["agent"]: {"chats": r["n"], "messages": r["m"], "latest": fmt_time(r["last"])} for r in rows}
