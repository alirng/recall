"""Optional late interaction: a ColBERT model's one vector per token, scored by MaxSim, as one
more ranking in search's fusion.

Off unless config.toml has a [late] table with a mode, or RECALL_LATE is set (it wins, so an eval
can compare modes without editing the config):

    [late]
    mode = "rerank"                        # or "rank"
    model = "topk-io/Iso-ModernColBERT"    # any pylate ColBERT checkpoint
    candidates = 20                        # rerank: the fused top chats it scores
    messages = 4                           # rerank: messages scored a chat
    weight = 1.0                           # its weight in the fusion
    reorder = false                        # rerank: order the candidates by MaxSim alone instead
    query_tokens = 0                       # 0: the model's own lengths (32 and 300 for Iso-ModernColBERT)
    document_tokens = 0
    device = "auto"                        # mps, cuda or cpu
    store = true                           # keep token vectors in late-vectors.db; false: rerank encodes every time
    pool = 1                               # merge a document's similar token vectors, this many to one
    bits = 16                              # stored vectors: 16 (float16) or 8 (int8, a scale per token)
    smart = 1.0                            # smart: models only: the token term's weight (0: pooled only)

    RECALL_LATE="rerank candidates=30 weight=2"    or    RECALL_LATE=off

A model named "smart:<Hugging Face repo>" reads the token vectors out of a single-vector embedder
instead of a ColBERT model (SMART, arXiv 2605.24938): the repo's ONNX export, onnxruntime, no torch.
A message then scores its pooled cosine plus the mean over query tokens of their best match among
its token vectors (the final layer's hidden states, each normalised), which needs no retraining.
"smart:nomic-ai/nomic-embed-text-v1.5" is the model recall's dense side runs through Ollama.

Two modes:
- rerank scores the chats fusion ranked highest, by each one's best few messages (its best
  keyword hits and its message closest in meaning), encoded at query time. No index is built.
- rank scores every message's stored token vectors and fuses chats ordered by their best message,
  as the dense ranking does. A search encodes what is missing, up to INLINE_LIMIT messages for an
  index on disk; `recall embed` encodes the rest.

Token vectors are kept by their text in late-vectors.db beside the index (in recall's home for an
index in memory), so a message is encoded once. The model runs in this process through pylate (the
`late` extra) and makes no network call once Hugging Face has downloaded it. A late-interaction
failure never breaks a search: it ranks without it.
"""

import contextlib
import hashlib
import importlib.util
import os
import sqlite3
import sys
import threading
import time

from . import config, paths

DEFAULTS = {
    "mode": "",
    "model": "topk-io/Iso-ModernColBERT",
    "candidates": 20,
    "messages": 4,
    "weight": 1.0,
    "reorder": False,
    "query_tokens": 0,
    "document_tokens": 0,
    "device": "auto",
    "store": True,
    "pool": 1,
    "bits": 16,
    "smart": 1.0,
}
SMART = "smart:"
MODES = ("rerank", "rank")
INLINE_LIMIT = 200  # messages a rank search on an index on disk may encode before answering
BATCH = 32
CHUNK = 2048  # messages scored at a time
SCHEMA = "create table if not exists tokens (key text primary key, n int, vec blob)"

_lock = threading.Lock()
_model: tuple[tuple, object] | None = None
_stores: dict[str, sqlite3.Connection] = {}
_warned: set[str] = set()


def settings() -> dict | None:
    """The [late] settings with RECALL_LATE applied, or None when late interaction is off or pylate
    is missing."""
    s = {**DEFAULTS, **(config.load().get("late") or {})}
    env = os.environ.get("RECALL_LATE")
    if env is not None:
        words = env.replace(",", " ").split()
        s["mode"] = words.pop(0) if words and "=" not in words[0] else s["mode"]
        for word in words:
            key, _, value = word.partition("=")
            if key in DEFAULTS:
                s[key] = _typed(value, DEFAULTS[key])
    if s["mode"] not in MODES:
        return None
    needs = "onnxruntime" if s["model"].startswith(SMART) else "pylate"
    if importlib.util.find_spec(needs) is None:
        _warn(f"late interaction is on but {needs} is missing: install the late extra (recall-agents[late])")
        return None
    return s


def describe(s: dict | None) -> str:
    """One line naming the mode and the settings that differ from the defaults, for an eval's note."""
    if not s:
        return "late off"
    return " ".join([f"late {s['mode']}"] + [f"{k}={v}" for k, v in s.items() if k != "mode" and v != DEFAULTS.get(k)])


def rerank(con, query: str, scores: dict, hits: dict, vector, msql: str, margs: list, s: dict) -> list[str]:
    """The fused top chats ordered by MaxSim against each one's best messages: its best keyword hits
    (hits: chat -> rows with seq, best first) and its message closest to the query vector, then its
    other hits and close messages, or else its first messages. A chat with no message to score comes
    last."""
    pool = sorted(scores, key=scores.get, reverse=True)[: s["candidates"]]
    if not pool:
        return []
    near: dict[str, list[int]] = {sid: [] for sid in pool}
    if vector is not None and vector is not False:
        for sid, seq in _closest(con, pool, vector, msql, margs):
            near[sid].append(seq)
    picks = {}
    for sid in pool:
        words = [h["seq"] for h in hits.get(sid, [])]
        # one place kept for the message closest in meaning, which keyword hits alone could crowd out
        order = words[: s["messages"] - 1] + near[sid][:1] + words + near[sid]
        picks[sid] = list(dict.fromkeys(order))[: s["messages"]]
    for sid in [sid for sid in pool if not picks[sid]]:
        picks[sid] = [
            r[0]
            for r in con.execute(
                f"select seq from messages m where session_id = ? {msql} order by seq limit ?",
                [sid, *margs, s["messages"]],
            )
        ]
    rows = []
    for sid in pool:
        for seq in picks[sid]:
            row = con.execute("select text from messages where session_id = ? and seq = ?", (sid, seq)).fetchone()
            if row and row[0].strip():
                rows.append((sid, row[0]))
    ranked = _by_best(rows, query, s, keep=s["store"], store_of=con)
    return ranked + [sid for sid in pool if sid not in set(ranked)]


def ranking(con, query: str, fsql: str, fargs: list, msql: str, margs: list, pool: int, s: dict) -> list[str]:
    """Chats ordered by their message whose stored token vectors best match the query's."""
    rows = [
        (r[0], r[1])
        for r in con.execute(
            f"""select m.session_id, m.text from messages m join sessions s on s.id = m.session_id
                where length(trim(m.text)) > 0 {fsql} {msql}""",
            [*fargs, *margs],
        )
    ]
    limit = INLINE_LIMIT if _store_path(con) else None
    return _by_best(rows, query, s, limit=limit, store_of=con)[:pool]


def index_messages(con, s: dict, log=None, deadline: float | None = None) -> int:
    """Encode and store every message of the index that has no token vectors yet (rank mode's
    backfill, run by `recall embed`). Returns how many it encoded."""
    store = _store(con)
    texts = list(dict.fromkeys(r[0] for r in con.execute("select text from messages where length(trim(text)) > 0")))
    have = set()
    for a in range(0, len(texts), 500):
        part = [_key(t, s) for t in texts[a : a + 500]]
        with _lock:
            have.update(
                r[0] for r in store.execute(f"select key from tokens where key in ({','.join('?' * len(part))})", part)
            )
    todo = [t for t in texts if _key(t, s) not in have]
    done = 0
    for a in range(0, len(todo), BATCH * 8):
        if deadline and time.time() > deadline:
            break
        _encoded(todo[a : a + BATCH * 8], s, store)
        done += len(todo[a : a + BATCH * 8])
        if log:
            log(f"late interaction: {done}/{len(todo)}")
    return done


# --- scoring -------------------------------------------------------------------------


def _by_best(rows, query, s, keep=True, limit=None, store_of=None) -> list[str]:
    """Chats of rows (chat id, message text), best first by their best message's MaxSim. With limit,
    at most that many messages are encoded now and the ones left wait for a later search."""
    np = _numpy()
    if not rows:
        return []
    store = _store(store_of) if keep else None
    texts = list(dict.fromkeys(t for _, t in rows))
    have = _load(store, [_key(t, s) for t in texts], s) if store is not None else {}
    missing = [t for t in texts if _key(t, s) not in have]
    if limit is not None and len(missing) > limit:
        _warn(f"late interaction: {len(missing)} messages have no token vectors yet; `recall embed` encodes them")
        missing = missing[:limit]
    have.update(_encoded(missing, s, store))
    rows = [(sid, have[_key(t, s)]) for sid, t in rows if _key(t, s) in have]
    q = _model_for(s).encode([query], is_query=True)[0].astype(np.float32)
    if s["model"].startswith(SMART):  # row 0 is the pooled vector, the rest are tokens (_Smart)
        late = np.array(_maxsim(q[1:], [m[1:] for _, m in rows])) / (len(q) - 1)
        pooled = np.stack([m[0] for _, m in rows]).astype(np.float32) @ q[0]
        scores = (pooled + s["smart"] * late).tolist()
    else:
        scores = _maxsim(q, [m for _, m in rows])
    best: dict[str, float] = {}
    for (sid, _), score in zip(rows, scores, strict=True):
        best[sid] = max(best.get(sid, score), score)
    return sorted(best, key=best.get, reverse=True)


def _maxsim(q, docs: list) -> list[float]:
    """Each document's MaxSim with the query: for every query token its most similar document token,
    summed. Documents are scored CHUNK at a time, so a large index never makes one huge matrix."""
    np = _numpy()
    out = np.empty(len(docs), dtype=np.float32)
    for a in range(0, len(docs), CHUNK):
        part = docs[a : a + CHUNK]
        lens = np.array([len(d) for d in part])
        sims = q @ np.concatenate(part).astype(np.float32).T
        out[a : a + len(part)] = np.maximum.reduceat(sims, np.concatenate([[0], np.cumsum(lens)[:-1]]), axis=1).sum(0)
    return out.tolist()


def _closest(con, sids: list[str], vector, msql: str, margs: list) -> list[tuple[str, int]]:
    """(chat, seq) of these chats' messages, closest to the query vector first, from the dense
    vectors of the embedding model in use."""
    from . import embeddings

    np = _numpy()
    embedder = embeddings.configured()
    if embedder is None or not con.execute("select 1 from sqlite_master where name = 'vectors'").fetchone():
        return []
    q = np.asarray(vector, dtype=np.float32)
    rows = [
        r
        for r in con.execute(
            f"""select v.session_id, v.seq, v.vec from vectors v
                join messages m on m.session_id = v.session_id and m.seq = v.seq
                where v.model = ? and v.session_id in ({",".join("?" * len(sids))}) {msql}""",
            [embedder.model, *sids, *margs],
        )
        if len(r[2]) == 2 * len(q)
    ]
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16).reshape(len(rows), -1)
    return [(rows[i][0], rows[i][1]) for i in np.argsort(-(matrix.astype(np.float32) @ q))]


# --- the model and the store ---------------------------------------------------------


class _Encoder:
    def __init__(self, s: dict):
        import torch
        from pylate import models

        device = s["device"]
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        # Checkpoints saved in bfloat16 load in it, which a Mac's CPU runs about 30 times slower than float32.
        dtype = torch.float32 if device == "cpu" else torch.float16
        lengths = {k: s[f"{k.split('_')[0]}_tokens"] or None for k in ("query_length", "document_length")}
        _warn(f"{describe(s)}: loading {s['model']} on {device}")
        with contextlib.redirect_stdout(sys.stderr):  # `recall serve` speaks MCP on stdout
            self.model = models.ColBERT(
                model_name_or_path=s["model"], device=device, model_kwargs={"dtype": dtype}, **lengths
            )
        self.document_length = self.model.document_length
        if device == "mps":
            self.model.tokenize = _bucketed(self.model)

    def encode(self, texts: list[str], is_query: bool, pool: int = 1) -> list:
        with _lock, contextlib.redirect_stdout(sys.stderr):
            return self.model.encode(
                texts, is_query=is_query, batch_size=BATCH, show_progress_bar=False, pool_factor=pool
            )


def _bucketed(model, step: int = 64):
    """The model's tokenize with every batch padded to a multiple of step tokens. MPS compiles a graph
    for each input shape it meets and keeps it: with a shape per batch length, a rank run over
    LongMemEval grew to 27 GB. Padding is masked out of a document's vectors, and of a query's unless
    the model expands queries with its own padding (then a query is left as it is)."""
    import torch

    tokenize, pad = model.tokenize, model.tokenizer.pad_token_id

    def padded(texts, is_query=True, **kwargs):
        out = tokenize(texts, is_query=is_query, **kwargs)
        extra = -out["input_ids"].shape[1] % step
        if extra and not (is_query and model.do_query_expansion):
            for k, fill in (("input_ids", pad), ("attention_mask", 0), ("token_type_ids", 0)):
                if k in out:
                    out[k] = torch.cat([out[k], torch.full((len(out[k]), extra), fill, dtype=out[k].dtype)], dim=1)
        return out

    return padded


class _Smart:
    """A single-vector embedder read as a multi-vector one (SMART): its ONNX export's final hidden
    states. Each text comes back as one matrix, the normalised mean of its tokens' states (the pooled
    embedding the model was trained on) in row 0, then each token's normalised state."""

    document_length = 512
    cut = 2000  # characters, as recall's dense side embeds them (embeddings.MAX_CHARS)

    def __init__(self, s: dict):
        import onnxruntime
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        repo = s["model"][len(SMART) :]
        _warn(f"{describe(s)}: loading {repo} (ONNX) on cpu")
        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        self.session = onnxruntime.InferenceSession(hf_hub_download(repo, "onnx/model.onnx"), options)
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        self.lengths = (s["query_tokens"] or self.document_length, s["document_tokens"] or self.document_length)
        self.prefixes = ("search_query: ", "search_document: ") if "nomic-embed" in repo else ("", "")

    def encode(self, texts: list[str], is_query: bool, pool: int = 1) -> list:
        np = _numpy()
        self.tokenizer.enable_truncation(self.lengths[0 if is_query else 1])
        self.tokenizer.enable_padding()
        out = []
        with _lock:
            for a in range(0, len(texts), 8):
                batch = self.tokenizer.encode_batch([self.prefixes[0 if is_query else 1] + t for t in texts[a : a + 8]])
                ids = np.array([e.ids for e in batch], dtype=np.int64)
                mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
                feed = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
                states = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0]
                for h, m in zip(states, mask, strict=True):
                    h = h[m.astype(bool)]
                    rows = np.vstack([h.mean(0, keepdims=True), h])
                    out.append(rows / np.linalg.norm(rows, axis=1, keepdims=True))
        return out


def _model_for(s: dict):
    global _model
    key = (s["model"], s["device"], s["query_tokens"], s["document_tokens"])
    with _lock:
        if _model is None or _model[0] != key:
            _model = (key, _Smart(s) if s["model"].startswith(SMART) else _Encoder(s))
    return _model[1]


def _encoded(texts: list[str], s: dict, store) -> dict:
    """Token vectors (float16, one row a token) of texts, encoded now and kept in the store."""
    np = _numpy()
    if not texts:
        return {}
    encoder = _model_for(s)
    # characters: more than the model reads, less than a long message
    cut = getattr(encoder, "cut", encoder.document_length * 10)
    out = {}
    for a in range(0, len(texts), BATCH * 8):
        part = texts[a : a + BATCH * 8]
        encoded = encoder.encode([t[:cut] for t in part], is_query=False, pool=s["pool"])
        rows = [
            (_key(t, s), len(v), _packed(np.asarray(v, dtype=np.float32), s))
            for t, v in zip(part, encoded, strict=True)
        ]
        out.update({key: _unpacked(blob, n, s) for key, n, blob in rows})  # what a later search loads
        if store is not None:
            with _lock:
                store.executemany("insert or replace into tokens values (?,?,?)", rows)
                store.commit()
    return out


def _packed(v, s: dict) -> bytes:
    """Token vectors as stored: float16, or with bits = 8 a float16 scale per token then int8 values."""
    np = _numpy()
    if s["bits"] != 8:
        return v.astype(np.float16).tobytes()
    scale = np.maximum(np.abs(v).max(axis=1, keepdims=True), 1e-6) / 127
    return scale.astype(np.float16).tobytes() + np.round(v / scale).astype(np.int8).tobytes()


def _unpacked(blob: bytes, n: int, s: dict):
    np = _numpy()
    if s["bits"] != 8:
        return np.frombuffer(blob, dtype=np.float16).reshape(n, -1)
    scale = np.frombuffer(blob[: 2 * n], dtype=np.float16).reshape(n, 1)
    return (np.frombuffer(blob[2 * n :], dtype=np.int8).reshape(n, -1) * scale).astype(np.float16)


def _load(store, keys: list[str], s: dict) -> dict:
    out = {}
    for a in range(0, len(keys), 500):
        part = keys[a : a + 500]
        with _lock:
            rows = store.execute(f"select key, n, vec from tokens where key in ({','.join('?' * len(part))})", part)
            rows = rows.fetchall()
        out.update({k: _unpacked(v, n, s) for k, n, v in rows})
    return out


def _key(text: str, s: dict) -> str:
    # pool and bits join the key only when set, so stores written before they existed stay valid
    extra = "".join(f"\0{k}={s[k]}" for k in ("pool", "bits") if s[k] != DEFAULTS[k])
    return hashlib.sha1(f"{s['model']}\0{s['document_tokens']}{extra}\0{text}".encode()).hexdigest()[:24]


def _store(con) -> sqlite3.Connection:
    """The token-vector store: late-vectors.db beside the index, or in recall's home for an index in
    memory, so an eval's indexes share one."""
    path = _store_path(con) or os.path.join(paths.HOME, "late-vectors.db")
    with _lock:
        if path not in _stores:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            store = sqlite3.connect(path, timeout=30, check_same_thread=False)
            store.execute("pragma journal_mode=wal")
            store.execute(SCHEMA)
            _stores[path] = store
        return _stores[path]


def _store_path(con) -> str | None:
    if con is None:
        return None
    for row in con.execute("pragma database_list"):
        if row[1] == "main":
            return os.path.splitext(row[2])[0] + "-late.db" if row[2] else None
    return None


def _typed(value: str, like):
    if isinstance(like, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return value


def _numpy():
    import numpy

    return numpy


def _warn(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(f"recall: {message}", file=sys.stderr)
