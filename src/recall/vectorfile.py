"""One copy of the message vectors on disk, shared by every recall process.

Each agent session runs its own `recall serve`, and a matrix of every vector held in each of
them grew with history and with the sessions open: over 100 MB a server for 23k messages. Instead
the vectors of a model sit in one file beside the index. A search maps it read-only, scores
it a chunk at a time and unmaps it, so the OS page cache holds the one copy all processes
read, and a process keeps nothing once its search is done.

In <index>-vectors/ beside recall.db, per model:
  <model>.json         the header: which data file, how many of its rows are complete
  <model>.<gen>.vec    fixed-size rows (session id, seq, text hash, fp16 vector), only appended

A writer appends rows, then replaces the header, so a reader, which maps only the rows the
header counts, never sees a half-written one. The vectors table stays the source of truth: a
trigger logs each row any recall release inserts into it to vector_log, the file catches up
from that log, and a missing, corrupt or out-of-step file is rebuilt from the table. Rowids
cannot be followed instead: VACUUM renumbers them, and a replaced last row gets its own back.
A row a later insert replaced stays in the file until a rebuild, so a search checks each
candidate against the table; recall forget rebuilds at once, so a forgotten chat's vectors
leave the file with its rows.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time

FORMAT = 1
CHUNK = 2048  # rows read or scored at a time: 6 MB as float32 at 768 dimensions
STALE_SHARE = 0.25  # rebuild once this share of the rows are replaced or gone
LOG = (
    "create table if not exists meta (k text primary key, v text)",
    "create table if not exists vector_log (id integer primary key autoincrement, session_id text, seq int)",
    """create trigger if not exists vectors_logged after insert on vectors begin
         insert into vector_log(session_id, seq) values (new.session_id, new.seq);
       end""",
)
PRUNED = "vector_log_pruned"  # meta: entries up to this id are gone, so a file older than it is rebuilt


def best(con, model: str, vector, n: int) -> list[str] | None:
    """Session ids of the n stored messages closest to the query vector, best first, one per
    message. Only rows the vectors table still holds, for a message the index still has, count.
    None when there is no file to read (an index in memory, or one it cannot write beside):
    the caller then scans the table."""
    try:
        return _best(con, model, vector, n)
    except Exception as e:  # the file must never break a search
        print(f"recall: shared vector file unusable, scanning the index ({e})", file=sys.stderr)
        return None


def _best(con, model: str, vector, n: int) -> list[str] | None:
    np = _numpy()
    h = current(con, model)
    if h is None:
        return None
    if not h["rows"]:
        return []
    q = np.asarray(vector, dtype=np.float32)
    if q.shape != (h["dim"],):
        return []
    try:
        rows = _map(np, _folder(con), h)
    except FileNotFoundError:  # rebuilt between reading the header and opening its file
        h = current(con, model)
        rows = _map(np, _folder(con), h)
    scores = np.empty(len(rows), dtype=np.float32)
    for a in range(0, len(rows), CHUNK):
        scores[a : a + CHUNK] = rows["vec"][a : a + CHUNK].astype(np.float32) @ q
    order = np.argsort(-scores)
    out: list[str] = []
    for a in range(0, len(order), n):
        if len(out) >= n:
            break
        idx = order[a : a + n]
        keys = [
            (s.decode(), int(seq), hsh.decode())
            for s, seq, hsh in zip(rows["sid"][idx], rows["seq"][idx], rows["hash"][idx], strict=True)
        ]
        live = _live(con, model, keys)
        out += [k[0] for k in keys if k in live][: n - len(out)]
    return out


def current(con, model: str, writer: bool = False) -> dict | None:
    """The header of the model's file, caught up with the vectors table first. None for an
    index with no file of its own. writer: also prune the log and other models' files, which
    only the processes that embed do, so a search never writes to the index."""
    folder = _folder(con)
    if folder is None or _numpy() is None:
        return None
    _ensure_log(con)
    h = _header(folder, model)
    if h and h["log"] == _top(con) and _intact(folder, h) and not writer:
        return h
    with _locked(folder):
        with _snapshot(con):  # one read of the index, so the log's top and the rows it covers agree
            top = _top(con)
            h = _header(folder, model)  # another process may have caught it up while this one waited
            if not (h and h["log"] == top and _intact(folder, h)):
                fits = h and h["rows"] and _intact(folder, h) and _pruned(con) <= h["log"] <= top
                h = (fits and _append(con, folder, model, h, top)) or _rebuild(con, folder, model, top)
        if writer:
            _prune(con, folder, model, h)
    return h


def drop_deleted(con) -> None:
    """Rewrite each model's file from the vectors table after rows were deleted from it (recall
    forget), so their vectors leave the disk now rather than at the next rebuild."""
    folder = _folder(con)
    if folder is None or not os.path.isdir(folder):
        return
    with _locked(folder):
        models = [h["model"] for h in map(_load, _names(folder, ".json")) if h]
        if _numpy() is None:  # nothing here can rewrite them, so they go
            for name in _names(folder, ".json") + _names(folder, ".vec"):
                os.unlink(name)
            return
        _ensure_log(con)
        for model in models:
            with _snapshot(con):
                _rebuild(con, folder, model, _top(con))


# --- writing -------------------------------------------------------------------------


def _append(con, folder: str, model: str, h: dict, top: int) -> dict | None:
    """Add the rows logged since the file was last caught up. None when the file should be
    rebuilt instead: a row it cannot hold, or too many of its rows replaced since."""
    np = _numpy()
    dt = _dtype(np, h)
    cur = con.execute(
        """select v.session_id, v.seq, v.hash, v.vec
           from (select distinct session_id, seq from vector_log where id > ? and id <= ?) l
           join vectors v on v.session_id = l.session_id and v.seq = l.seq where v.model = ?""",
        (h["log"], top, model),
    )
    n = h["rows"]
    with open(_data(folder, model, h["gen"]), "r+b") as f:
        f.truncate(n * dt.itemsize)  # a run that died mid-append left rows no header counts
        f.seek(n * dt.itemsize)
        while batch := cur.fetchmany(CHUNK):
            recs = _records(np, dt, batch)
            if recs is None:
                return None
            f.write(recs.tobytes())
            n += len(recs)
        f.flush()
        os.fsync(f.fileno())
    live = con.execute("select count(*) from vectors where model = ?", (model,)).fetchone()[0]
    if n - live > STALE_SHARE * n:
        return None
    h = dict(h, rows=n, log=top)
    _publish(folder, model, h)
    return h


def _rebuild(con, folder: str, model: str, top: int) -> dict:
    """Write the model's file afresh from the vectors table, then retire the one it replaces."""
    np = _numpy()
    longest = con.execute(
        "select max(length(cast(session_id as blob))) from vectors where model = ?", (model,)
    ).fetchone()[0]
    size = con.execute(
        "select length(vec) from vectors where model = ? order by rowid desc limit 1", (model,)
    ).fetchone()
    h = {
        "format": FORMAT,
        "model": model,
        "gen": f"{time.time_ns():x}",
        "width": max(64, 1 << max(0, (longest or 0) - 1).bit_length()),
        "dim": size[0] // 2 if size else 0,
        "rows": 0,
        "log": top,
    }
    dt = _dtype(np, h)
    path = _data(folder, model, h["gen"])
    with open(path, "wb") as f:
        cur = con.execute(
            "select session_id, seq, hash, vec from vectors where model = ? and length(vec) = ?",
            (model, 2 * h["dim"]),
        )
        while batch := cur.fetchmany(CHUNK):
            f.write(_records(np, dt, batch).tobytes())
            h["rows"] += len(batch)
        f.flush()
        os.fsync(f.fileno())
    _publish(folder, model, h)
    for name in _names(folder, ".vec", _slug(model) + "."):
        if name != path:
            os.unlink(name)  # a search that mapped it keeps reading it until it unmaps
    return h


def _records(np, dt, rows: list):
    """The rows as records of dt, or None when one does not fit: a longer id, or a vector of another size."""
    width, dim = dt["sid"].itemsize, dt["vec"].shape[0]
    ids = [r[0].encode() for r in rows]
    if any(len(i) > width for i in ids) or any(len(r[3]) != 2 * dim for r in rows):
        return None
    recs = np.zeros(len(rows), dtype=dt)
    recs["sid"] = ids
    recs["seq"] = [r[1] for r in rows]
    recs["hash"] = [r[2].encode() for r in rows]
    recs["vec"] = np.frombuffer(b"".join(r[3] for r in rows), dtype="<f2").reshape(len(rows), dim)
    return recs


def _publish(folder: str, model: str, h: dict) -> None:
    tmp = os.path.join(folder, f"{_slug(model)}.json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(h, f)
    os.replace(tmp, os.path.join(folder, _slug(model) + ".json"))


def _prune(con, folder: str, model: str, h: dict) -> None:
    """Drop the log entries the file has taken in, other models' files, and a header a crash
    left half-written."""
    for name in _names(folder, ".json") + _names(folder, ".vec") + _names(folder, ".tmp"):
        base = os.path.basename(name)
        if base.endswith(".tmp") or not base.startswith(_slug(model) + "."):
            os.unlink(name)
    if con.execute("select 1 from vector_log where id <= ? limit 1", (h["log"],)).fetchone():
        con.execute("delete from vector_log where id <= ?", (h["log"],))
        con.execute("insert or replace into meta values (?, ?)", (PRUNED, str(h["log"])))
        con.commit()


def _ensure_log(con) -> None:
    if not con.execute("select 1 from sqlite_master where type = 'trigger' and name = 'vectors_logged'").fetchone():
        for stmt in LOG:
            con.execute(stmt)
        con.commit()


# --- reading -------------------------------------------------------------------------


def _live(con, model: str, keys: list[tuple]) -> set[tuple]:
    """Which (session id, seq, hash) rows the vectors table still holds for a message the index has."""
    found: set[tuple] = set()
    for a in range(0, len(keys), 200):
        part = keys[a : a + 200]
        found |= {
            tuple(r)
            for r in con.execute(
                f"""with c(session_id, seq, hash) as (values {",".join(["(?,?,?)"] * len(part))})
                    select c.session_id, c.seq, c.hash from c
                    join vectors v on v.session_id = c.session_id and v.seq = c.seq and v.hash = c.hash
                    join messages m on m.session_id = c.session_id and m.seq = c.seq
                    where v.model = ?""",
                [x for k in part for x in k] + [model],
            )
        }
    return found


def _map(np, folder: str, h: dict):
    return np.memmap(_data(folder, h["model"], h["gen"]), dtype=_dtype(np, h), mode="r", shape=(h["rows"],))


def _header(folder: str, model: str) -> dict | None:
    h = _load(os.path.join(folder, _slug(model) + ".json"))
    return h if h and h["model"] == model else None


def _load(path: str) -> dict | None:
    try:
        with open(path) as f:
            h = json.load(f)
    except (OSError, ValueError):
        return None
    keys = ("model", "gen", "width", "dim", "rows", "log")
    return h if isinstance(h, dict) and h.get("format") == FORMAT and all(k in h for k in keys) else None


def _intact(folder: str, h: dict) -> bool:
    try:
        return os.path.getsize(_data(folder, h["model"], h["gen"])) >= h["rows"] * (h["width"] + 20 + 2 * h["dim"])
    except OSError:
        return False


def _top(con) -> int:
    """The last id the log ever gave, which deleting its entries does not lower."""
    row = con.execute("select seq from sqlite_sequence where name = 'vector_log'").fetchone()
    return row[0] if row else 0


def _pruned(con) -> int:
    row = con.execute("select v from meta where k = ?", (PRUNED,)).fetchone()
    return int(row[0]) if row else 0


# --- helpers -------------------------------------------------------------------------


def _folder(con) -> str | None:
    for row in con.execute("pragma database_list"):
        if row[1] == "main":
            return os.path.splitext(row[2])[0] + "-vectors" if row[2] else None
    return None


def _slug(model: str) -> str:
    """A file name for the model, e.g. ollama-nomic-embed-text-0b6c2a1f."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-.")[:60] + "-" + hashlib.sha1(model.encode()).hexdigest()[:8]


def _data(folder: str, model: str, gen: str) -> str:
    return os.path.join(folder, f"{_slug(model)}.{gen}.vec")


def _names(folder: str, suffix: str, prefix: str = "") -> list[str]:
    return [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(suffix) and n.startswith(prefix)]


def _dtype(np, h: dict):
    return np.dtype([("sid", f"S{h['width']}"), ("seq", "<i4"), ("hash", "S16"), ("vec", "<f2", (h["dim"],))])


@contextlib.contextmanager
def _locked(folder: str):
    """One process at a time changes the files; a search that finds them current never waits."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _snapshot(con):
    own = not con.in_transaction
    if own:
        con.execute("begin")
    try:
        yield
    finally:
        if own:
            con.rollback()  # it only read


def _numpy():
    try:
        import numpy

        return numpy
    except ImportError:
        return None
