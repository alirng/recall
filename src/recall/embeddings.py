"""Optional semantic search: vectors from an embedding model, fused with keyword search.

Off unless config.toml has an [embeddings] table:

    [embeddings]
    provider = "ollama"                    # or "openai-compatible": any /v1/embeddings server
    name = "nomic-embed-text"
    base_url = "http://localhost:11434"    # optional
    api_key = "keychain"                   # optional, for a hosted endpoint (see keys.py)

Each chat message and each memory note gets one vector, stored in recall.db as derived data
and keyed by its text, so a chat that grows only embeds its new messages. `recall index`
builds them; a search embeds only the query. If the model cannot be reached, search uses
keywords alone. The arithmetic needs numpy, which the `embeddings` extra installs.

Search reads the vectors from one file beside the index that every recall process maps
(vectorfile.py), so an agent session's server keeps no copy of them.

Why fuse rather than replace: on LongMemEval, porter BM25 already finds most answer sessions,
and published hybrids gain a few points over it while dense retrieval alone does worse
(see docs/research/retrieval-benchmarks.md).
"""

import contextlib
import fcntl
import hashlib
import os
import sys
import time

from . import config, keys, paths, vectorfile

MAX_CHARS = 2000  # of each message; the opening of a long reply says what it is about
BATCH = 32
INLINE_LIMIT = 200  # new messages a search may embed before answering; more go to the background
QUERY_TIMEOUT = 10  # seconds a search waits for its query's vector before going by keywords
INLINE_TIMEOUT = 30  # each request while a search or a tool call waits on it
BATCH_TIMEOUT = 300  # each request of a backfill, which nobody waits on
EMBED_DEADLINE = 20 * 60  # a `recall embed` run stops after this; the next one carries on
# Some models are trained with task prefixes and retrieve noticeably worse without them.
PREFIXES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "qwen3-embedding": ("Instruct: Given a question, retrieve past chat messages that answer it\nQuery: ", ""),
}
SCHEMA = """
create table if not exists vectors (
  session_id text, seq int, model text, hash text, vec blob, primary key (session_id, seq)
);
create table if not exists note_vectors (note_id text primary key, model text, hash text, vec blob);
"""
# Chats whose messages changed since pending() last read them. Triggers mark them, so writes by
# a release that knows nothing of this are marked too. Rebuilding the chat tables drops the
# triggers with the messages table, and pending() then reads every message once.
DIRTY = """
create table if not exists embed_dirty (id integer primary key autoincrement, session_id text unique);
create trigger if not exists messages_embed_ai after insert on messages begin
  insert or replace into embed_dirty(session_id) values (new.session_id);
end;
create trigger if not exists messages_embed_au after update of text on messages begin
  insert or replace into embed_dirty(session_id) values (new.session_id);
end;
"""
CHECKED = "embed_checked"  # meta: the model and text cut the last read of every message was for

_embedder: tuple[str, "Embedder | None"] | None = None


class Embedder:
    def __init__(self, settings: dict):
        self.provider = settings.get("provider", "ollama")
        self.name = settings.get("name") or "nomic-embed-text"
        default = "http://localhost:11434" if self.provider == "ollama" else ""
        self.base_url = (settings.get("base_url") or default).rstrip("/")
        self.api_key = keys.load(settings.get("api_key"), f"embeddings-{self.provider}")
        base = self.name.split(":")[0]
        self.query_prefix, self.document_prefix = PREFIXES.get(base, ("", ""))
        self.query_prefix = settings.get("query_prefix", self.query_prefix)
        self.document_prefix = settings.get("document_prefix", self.document_prefix)

    @property
    def model(self) -> str:
        return f"{self.provider}:{self.name}"

    def embed(self, texts: list[str], query: bool = False, timeout: float | None = None) -> list[list[float]]:
        from .llm.base import http_json

        timeout = timeout or (QUERY_TIMEOUT if query else BATCH_TIMEOUT)
        prefix = self.query_prefix if query else self.document_prefix
        inputs = [prefix + t[:MAX_CHARS] for t in texts]
        if self.provider == "ollama":
            # keep_alive holds the model in memory between searches, rather than Ollama's 5 minutes
            payload = {"model": self.name, "input": inputs, "keep_alive": "30m"}
            out = http_json(f"{self.base_url}/api/embed", payload, timeout=timeout)
            return out["embeddings"]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        out = http_json(f"{self.base_url}/embeddings", {"model": self.name, "input": inputs}, headers, timeout=timeout)
        return [d["embedding"] for d in sorted(out["data"], key=lambda d: d["index"])]


def configured() -> "Embedder | None":
    """The embedder config.toml asks for, or None when embeddings are off or numpy is missing."""
    global _embedder
    settings = config.load().get("embeddings") or {}
    key = repr(sorted(settings.items()))
    if _embedder is None or _embedder[0] != key:
        ready = bool(settings.get("provider") or settings.get("name")) and _numpy() is not None
        _embedder = (key, Embedder(settings) if ready else None)
    return _embedder[1]


def query_vector(query: str, embedder: "Embedder | None" = None):
    """The query's unit vector, or None when embeddings are off or the model is unreachable."""
    embedder = embedder or configured()
    if embedder is None or not query.strip():
        return None
    try:
        return _unit(embedder.embed([query], query=True)[0])
    except Exception as e:  # a model that is down must never break a search
        print(f"recall: embeddings unavailable, searching by keywords ({e})", file=sys.stderr)
        return None


# --- chat messages -----------------------------------------------------------------


def pending(con, embedder: "Embedder") -> list[tuple]:
    """Messages with no vector from this model for their current text. Reads only the chats
    marked as changed since the last call, or every message once after the chat tables were
    rebuilt or the model changed. A chat with messages still to embed stays marked until a
    later call finds them done."""
    con.executescript(SCHEMA)
    checked = f"{embedder.model} {MAX_CHARS}"
    row = con.execute("select v from meta where k = ?", (CHECKED,)).fetchone() if _has_table(con, "meta") else None
    every = not _has_trigger(con, "messages_embed_au") or not row or row[0] != checked
    if every:
        con.executescript(DIRTY)  # first, so a message written while the read below runs is marked
    marks = {r[0]: r[1] for r in con.execute("select session_id, id from embed_dirty")}
    if every:
        have = {
            (r[0], r[1]): r[2]
            for r in con.execute("select session_id, seq, hash from vectors where model = ?", (embedder.model,))
        }
        rows = con.execute("select session_id, seq, text from messages")
    else:
        sids = list(marks)
        have = {
            (r[0], r[1]): r[2]
            for r in _chunked(
                con,
                "select session_id, seq, hash from vectors where model = ? and session_id in",
                sids,
                [embedder.model],
            )
        }
        rows = _chunked(con, "select session_id, seq, text from messages where session_id in", sids)
    out = []
    for r in rows:
        h = _hash(r[2])
        if have.get((r[0], r[1])) != h:
            out.append((r[0], r[1], r[2], h))
    waiting = {r[0] for r in out}
    # a mark set again since the read above has a new id, so this leaves it
    con.executemany(
        "delete from embed_dirty where session_id = ? and id = ?",
        [(s, i) for s, i in marks.items() if s not in waiting],
    )
    if every:
        con.executemany("insert or ignore into embed_dirty(session_id) values (?)", [(s,) for s in waiting])
        con.execute("create table if not exists meta (k text primary key, v text)")
        con.execute("insert or replace into meta values (?, ?)", (CHECKED, checked))
    con.commit()
    return out


def index_messages(
    con,
    embedder: "Embedder",
    todo: list[tuple] | None = None,
    cache=None,
    log=None,
    deadline: float | None = None,
    timeout: float | None = None,
) -> int:
    """Embed the messages that need it. cache: an optional connection holding vectors by
    text hash from earlier runs, so the same text is never embedded twice. deadline: a
    time.time() to stop by, checked between batches. timeout: seconds each request may take."""
    np = _numpy()
    todo = pending(con, embedder) if todo is None else todo
    if cache is not None:
        cache.execute(
            "create table if not exists vector_cache (hash text, model text, vec blob, primary key (hash, model))"
        )
    done = 0
    for i in range(0, len(todo), BATCH):
        if deadline and time.time() > deadline:
            break
        wait = timeout or BATCH_TIMEOUT
        if deadline:
            wait = max(5.0, min(wait, deadline - time.time()))
        batch = todo[i : i + BATCH]
        vecs: dict[str, bytes] = {}
        if cache is not None:
            hashes = [h for *_, h in batch]
            q = f"select hash, vec from vector_cache where model = ? and hash in ({','.join('?' * len(hashes))})"
            vecs = dict(cache.execute(q, [embedder.model, *hashes]))
        missing = [b for b in batch if b[3] not in vecs]
        if missing:
            for (_, _, _, h), v in zip(missing, embedder.embed([b[2] for b in missing], timeout=wait), strict=True):
                vecs[h] = np.asarray(_unit(v), dtype=np.float16).tobytes()
            if cache is not None:
                cache.executemany(
                    "insert or replace into vector_cache values (?,?,?)",
                    [(b[3], embedder.model, vecs[b[3]]) for b in missing],
                )
                cache.commit()
        con.executemany(
            "insert or replace into vectors values (?,?,?,?,?)",
            [(sid, seq, embedder.model, h, vecs[h]) for sid, seq, _, h in batch],
        )
        con.commit()
        done += len(batch)
        if log and done % (BATCH * 20) < BATCH:
            log(f"embeddings: {done}/{len(todo)}")
    if done:
        try:  # bring the shared file up to date here, so a search does not have to
            vectorfile.current(con, embedder.model, writer=True)
        except Exception as e:
            print(f"recall: could not update the shared vector file: {e}", file=sys.stderr)
    return done


@contextlib.contextmanager
def embedding_lock():
    """Held while chat messages are embedded, so a `recall embed` backfill and a server's refresh
    never embed the same messages at once. Yields False, without waiting, when another holds it."""
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "embed.lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def refresh(con, background: bool = False, log=None) -> int:
    """Embed new messages if embeddings are on. With background, a large backlog goes to a
    separate `recall embed` process instead of holding up the caller."""
    embedder = configured()
    if embedder is None:
        return 0
    with embedding_lock() as mine:
        if not mine:  # the chats it did not take stay marked for the next refresh
            if log:
                log("embeddings: skipped, a recall embed is running")
            return 0
        try:
            todo = pending(con, embedder)
        except Exception:
            return 0
        if todo and not (background and len(todo) > INLINE_LIMIT):
            try:
                return index_messages(con, embedder, todo, log=log, timeout=INLINE_TIMEOUT if background else None)
            except Exception as e:
                print(f"recall: could not embed new messages: {e}", file=sys.stderr)
                return 0
    if todo:  # once the lock is let go, or the run this starts would find it taken
        _spawn_backfill(con)
    return 0


def ranking(con, vector, fsql: str, fargs: list, msql: str, margs: list, pool: int) -> list[str]:
    """Chats ordered by their message closest in meaning to the query vector."""
    np = _numpy()
    if vector is None or np is None or not _has_table(con, "vectors"):
        return []
    model_row = con.execute("select model from vectors limit 1").fetchone()
    if not model_row:
        return []
    embedder = configured()
    model = embedder.model if embedder else model_row[0]
    q = np.asarray(vector, dtype=np.float32)
    closest = vectorfile.best(con, model, q, pool * 4)
    if closest is None:
        closest = _scan(con, model, q, pool * 4)
    allowed = {
        r[0]
        for r in con.execute(
            f"""select distinct m.session_id from messages m join sessions s on s.id = m.session_id
                where 1=1 {fsql} {msql}""",
            [*fargs, *margs],
        )
    }
    seen: dict[str, None] = {}
    for sid in closest:
        if sid in allowed and sid not in seen:
            seen[sid] = None
            if len(seen) >= pool:
                break
    return list(seen)


def _scan(con, model: str, q, n: int) -> list[str]:
    """Session ids of the n messages closest to q, best first, reading the vectors table a chunk
    at a time: for an index with no shared file, such as one in memory."""
    np = _numpy()
    ids: list[str] = []
    scores = []
    cur = con.execute(
        """select v.session_id, v.vec from vectors v
           join messages m on m.session_id = v.session_id and m.seq = v.seq where v.model = ?""",
        (model,),
    )
    while batch := cur.fetchmany(vectorfile.CHUNK):
        batch = [r for r in batch if len(r[1]) == 2 * len(q)]
        if batch:
            matrix = np.frombuffer(b"".join(r[1] for r in batch), dtype=np.float16).reshape(len(batch), -1)
            scores.append(matrix.astype(np.float32) @ q)
            ids += [r[0] for r in batch]
    if not ids:
        return []
    return [ids[i] for i in np.argsort(-np.concatenate(scores))[:n]]


def _spawn_backfill(con) -> None:
    import subprocess

    con.execute("create table if not exists meta (k text primary key, v text)")
    row = con.execute("select v from meta where k = 'embed_spawned'").fetchone()
    if row and time.time() - float(row[0]) < 600:
        return  # one is already running, or ran in the last ten minutes
    con.execute("insert or replace into meta values ('embed_spawned', ?)", (str(time.time()),))
    con.commit()
    subprocess.Popen(
        [sys.executable, "-m", "recall", "embed"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- memory notes --------------------------------------------------------------------


def note_ranking(con, notes: dict, query: str) -> list[str]:
    """Note ids ordered by closeness in meaning to the query; embeds new or edited notes first.
    Empty when embeddings are off."""
    np = _numpy()
    embedder = configured()
    if embedder is None:
        return []
    vector = query_vector(query, embedder)
    if vector is None:
        return []
    con.executescript(SCHEMA)
    have = {
        r[0]: (r[1], r[2])
        for r in con.execute("select note_id, hash, vec from note_vectors where model = ?", (embedder.model,))
    }
    texts = {i: f"{n.title}\n{n.body}" for i, n in notes.items() if n.status == "active"}
    stale = [i for i, t in texts.items() if have.get(i, (None,))[0] != _hash(t)]
    try:
        for start in range(0, len(stale), BATCH):
            ids = stale[start : start + BATCH]
            for i, v in zip(ids, embedder.embed([texts[i] for i in ids], timeout=INLINE_TIMEOUT), strict=True):
                blob = np.asarray(_unit(v), dtype=np.float16).tobytes()
                con.execute(
                    "insert or replace into note_vectors values (?,?,?,?)", (i, embedder.model, _hash(texts[i]), blob)
                )
                have[i] = (_hash(texts[i]), blob)
        con.commit()
    except Exception as e:
        print(f"recall: could not embed notes: {e}", file=sys.stderr)
    ids = [i for i in texts if i in have]
    if not ids:
        return []
    matrix = np.frombuffer(b"".join(have[i][1] for i in ids), dtype=np.float16).reshape(len(ids), -1)
    scores = matrix.astype(np.float32) @ np.asarray(vector, dtype=np.float32)
    return [ids[i] for i in np.argsort(-scores)]


# --- helpers -------------------------------------------------------------------------


def _numpy():
    try:
        import numpy

        return numpy
    except ImportError:
        return None


def _unit(v) -> list[float]:
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def _hash(text: str) -> str:
    return hashlib.sha1(text[:MAX_CHARS].encode()).hexdigest()[:16]


def _has_table(con, name: str) -> bool:
    return bool(con.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone())


def _has_trigger(con, name: str) -> bool:
    return bool(con.execute("select 1 from sqlite_master where type = 'trigger' and name = ?", (name,)).fetchone())


def _chunked(con, sql: str, ids: list[str], before: list | None = None):
    """Rows of `sql ... in (ids)`, a few hundred ids at a time to stay under SQLite's limit on arguments."""
    for a in range(0, len(ids), 500):
        part = ids[a : a + 500]
        yield from con.execute(f"{sql} ({','.join('?' * len(part))})", [*(before or []), *part])
