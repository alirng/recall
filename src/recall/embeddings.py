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

Why fuse rather than replace: on LongMemEval, porter BM25 already finds most answer sessions,
and published hybrids gain a few points over it while dense retrieval alone does worse
(see docs/research/retrieval-benchmarks.md).
"""

import hashlib
import sys
import time

from . import config, keys

MAX_CHARS = 2000  # of each message; the opening of a long reply says what it is about
BATCH = 32
INLINE_LIMIT = 200  # new messages a search may embed before answering; more go to the background
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

_embedder: tuple[str, "Embedder | None"] | None = None
_matrix: dict = {}


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

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        from .llm.base import http_json

        prefix = self.query_prefix if query else self.document_prefix
        inputs = [prefix + t[:MAX_CHARS] for t in texts]
        if self.provider == "ollama":
            # keep_alive holds the model in memory between searches, rather than Ollama's 5 minutes
            payload = {"model": self.name, "input": inputs, "keep_alive": "30m"}
            out = http_json(f"{self.base_url}/api/embed", payload, timeout=300)
            return out["embeddings"]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        out = http_json(f"{self.base_url}/embeddings", {"model": self.name, "input": inputs}, headers, timeout=300)
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
    """Messages with no vector from this model for their current text."""
    con.executescript(SCHEMA)
    have = {
        (r[0], r[1]): r[2]
        for r in con.execute("select session_id, seq, hash from vectors where model = ?", (embedder.model,))
    }
    out = []
    for r in con.execute("select session_id, seq, text from messages"):
        h = _hash(r[2])
        if have.get((r[0], r[1])) != h:
            out.append((r[0], r[1], r[2], h))
    return out


def index_messages(con, embedder: "Embedder", todo: list[tuple] | None = None, cache=None, log=None) -> int:
    """Embed the messages that need it. cache: an optional connection holding vectors by
    text hash from earlier runs, so the same text is never embedded twice."""
    np = _numpy()
    todo = pending(con, embedder) if todo is None else todo
    if cache is not None:
        cache.execute(
            "create table if not exists vector_cache (hash text, model text, vec blob, primary key (hash, model))"
        )
    done = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        vecs: dict[str, bytes] = {}
        if cache is not None:
            hashes = [h for *_, h in batch]
            q = f"select hash, vec from vector_cache where model = ? and hash in ({','.join('?' * len(hashes))})"
            vecs = dict(cache.execute(q, [embedder.model, *hashes]))
        missing = [b for b in batch if b[3] not in vecs]
        if missing:
            for (_, _, _, h), v in zip(missing, embedder.embed([b[2] for b in missing]), strict=True):
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
    return done


def refresh(con, background: bool = False, log=None) -> int:
    """Embed new messages if embeddings are on. With background, a large backlog goes to a
    separate `recall embed` process instead of holding up the caller."""
    embedder = configured()
    if embedder is None:
        return 0
    try:
        todo = pending(con, embedder)
    except Exception:
        return 0
    if not todo:
        return 0
    if background and len(todo) > INLINE_LIMIT:
        _spawn_backfill(con)
        return 0
    try:
        return index_messages(con, embedder, todo, log=log)
    except Exception as e:
        print(f"recall: could not embed new messages: {e}", file=sys.stderr)
        return 0


def ranking(con, vector, fsql: str, fargs: list, msql: str, margs: list, pool: int) -> list[str]:
    """Chats ordered by their message closest in meaning to the query vector."""
    np = _numpy()
    rows = _session_rows(con)
    if rows is None or vector is None:
        return []
    keys_, matrix = rows
    allowed = {
        r[0]
        for r in con.execute(
            f"""select distinct m.session_id from messages m join sessions s on s.id = m.session_id
                where 1=1 {fsql} {msql}""",
            [*fargs, *margs],
        )
    }
    scores = matrix @ np.asarray(vector, dtype=np.float32)
    order = np.argsort(-scores)
    seen: dict[str, None] = {}
    for i in order[: pool * 4]:
        sid = keys_[i][0]
        if sid in allowed and sid not in seen:
            seen[sid] = None
            if len(seen) >= pool:
                break
    return list(seen)


def _session_rows(con):
    """Every message vector for the configured model as one matrix, cached between searches
    while the table is unchanged."""
    np = _numpy()
    embedder = configured()
    model_row = con.execute("select model from vectors limit 1").fetchone() if _has_table(con, "vectors") else None
    if not model_row:
        return None
    model = embedder.model if embedder else model_row[0]
    stamp = con.execute("select count(*), max(rowid) from vectors where model = ?", (model,)).fetchone()
    path = con.execute("pragma database_list").fetchone()[2]
    key = (path, model, tuple(stamp))
    if path and _matrix.get("key") == key:
        return _matrix["rows"]
    keys_, blobs = [], []
    for r in con.execute(
        """select v.session_id, v.seq, v.vec from vectors v
           join messages m on m.session_id = v.session_id and m.seq = v.seq where v.model = ?""",
        (model,),
    ):
        keys_.append((r[0], r[1]))
        blobs.append(r[2])
    if not blobs:
        return None
    matrix = np.frombuffer(b"".join(blobs), dtype=np.float16).reshape(len(blobs), -1).astype(np.float32)
    rows = (keys_, matrix)
    if path:
        _matrix.update(key=key, rows=rows)
    return rows


def _spawn_backfill(con) -> None:
    import subprocess

    from . import power

    if power.defer():  # on battery the backlog waits for power
        return

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
            for i, v in zip(ids, embedder.embed([texts[i] for i in ids]), strict=True):
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
