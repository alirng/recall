"""Keep the chat index in step with each agent's history, re-reading only what changed."""

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

from .. import paths
from . import scrub, store
from .sources import agents, all_sources

STALE_AFTER = {"claude": 60, "codex": 60, "cursor": 60, "cursor-legacy": 1800}
# Cursor's older chats are rows in its state.vscdb, a database of several GB that cannot say which chats
# changed: finding out means reading every chat's header. A tool call never pays for that. These sources
# are read on the first index (by a short-lived `recall index`, as the scan leaves hundreds of MB with the
# allocator), by the nightly run and by `recall index`, and skipped while the database's files are as they
# were at the last full read.
OUT_OF_PROCESS = {"cursor-legacy"}
CHILD_DEADLINE = 15 * 60  # that background `recall index` stops here; the next one carries on
MAX_ATTEMPTS = 3  # reads of a chat that fails before it waits for its file to change
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def index(con, only=None, force=False, max_age=None, log=None) -> dict:
    """Incrementally index sources. Returns {source: {"indexed": n, "unchanged": n}}.

    With max_age, only sources not refreshed in the last max_age (or their own
    STALE_AFTER, whichever is longer) seconds are scanned, and a positive max_age
    starts OUT_OF_PROCESS sources in the background rather than waiting for them.
    A lock file stops two agents' servers from indexing at the same time; the loser
    just skips.
    """
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "index.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if max_age is not None else 0))
        except BlockingIOError:
            return {}
        scrub.ensure(con, log=log)  # candidates, caches and the archive are not rebuilt: see scrub.py
        result = _index(con, only, force, max_age, log)
        # _scan commits only the sources it reads. Anything else this run wrote must not stay open:
        # the caller's connection would hold the write lock until it is closed, and every other
        # recall process (another agent's server, a hook) would wait on it and fail.
        con.commit()
        if max_age is None or any(r["indexed"] or r.get("restored") for r in result.values()):
            from .. import embeddings

            embeddings.refresh(con, background=max_age is not None, log=log)
        return result  # the lock is released when the file closes


@contextlib.contextmanager
def exclusive(waiting: str | None = None):
    """Hold the index lock, waiting for an index run in progress to finish. For commands that
    change what the index holds (recall forget), so they do not interleave with a run that
    is reading the same chats. waiting: what to say if there is a wait."""
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "index.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if waiting:
                print(waiting, file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def index_paths(con, files: list[str], wait: float = 5.0) -> int:
    """Index these chat files now: a session-end hook passes the transcript it just closed,
    so the chat is in the archive before any cleanup can delete the file. Waits up to
    `wait` seconds for an index run already under way. Returns how many were indexed."""
    from .. import config, roots

    wanted = {os.path.realpath(f) for f in files}
    cfg = config.load()
    found = [r for r in roots.discover() if roots.option(r, "index", cfg)]  # index = false: not read at all
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "index.lock"), "w") as lock:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    return 0
                time.sleep(0.2)
        keep = _keeper(con)
        done = 0
        for name, items in all_sources(cfg["sources"], found).items():
            if name in OUT_OF_PROCESS:
                continue
            for item in items():
                if os.path.realpath(item.key) in wanted:
                    done += _read(con, item, keep)
        con.commit()
        return done


def _index(con, only, force, max_age, log) -> dict:
    from .. import config, forget, retention, roots

    forget.sweep(con)  # before reading anything: an older release may have brought forgotten chats back
    selected = config.load()["sources"]
    result = _scan(con, selected, only, force, max_age, log)
    retention.apply(con)  # before restoring, so nothing past an organisation's period comes back
    restored = store.restore(con, _read_since_rebuild(con, selected))
    if restored:
        result["archive"] = {"indexed": 0, "unchanged": 0, "restored": restored}
    # After the restore: chats indexed before roots existed, and restored ones, come back without a root.
    store.backfill_roots(con, roots.discover())
    return result


def _keeper(con):
    """Whether a parsed chat may be indexed: not forgotten, and not older than a retention
    period an organisation sets for its agent."""
    from .. import forget, retention

    gone, cutoffs = forget.forgotten(con), retention.cutoffs()

    def keep(session) -> bool:
        cutoff = cutoffs.get(session.agent)
        return session.id not in gone and (cutoff is None or (session.updated_at or 0) >= cutoff)

    return keep


def _read(con, item, keep) -> bool:
    """Parse one chat and index it. False when there was nothing to index. One bad chat must not
    stop the run, nor every run after it: a chat that fails is logged by its key and read again
    at the next run, up to MAX_ATTEMPTS times while its file stays the same (Codex may have been
    compressing it), and is then left until the file changes."""
    try:
        session = item.load()
        indexed = bool(session and session.messages and keep(session))
    except Exception as e:
        return _failed(con, item, e)
    if indexed:
        try:
            with store.atomic(con):  # a chat half written is undone, not committed with the next batch
                store.upsert(con, session)
        except sqlite3.OperationalError:  # the index itself (locked, full): no chat's fault
            raise
        except Exception as e:
            return _failed(con, item, e)
    con.execute(
        "insert or replace into seen values (?,?,?)",
        (item.key, item.fingerprint, session.id if session else None),
    )
    return indexed


def _failed(con, item, e: Exception) -> bool:
    """Count a failed read in `seen`: until MAX_ATTEMPTS its fingerprint is stored as
    "!<attempts>:<fingerprint>", which matches no file, so the next run reads the chat again."""
    row = con.execute("select fingerprint, session_id from seen where key = ?", (item.key,)).fetchone()
    tried = re.fullmatch(r"!(\d+):(.*)", row[0] or "", re.S) if row else None
    attempts = int(tried[1]) + 1 if tried and tried[2] == item.fingerprint else 1
    left = attempts < MAX_ATTEMPTS
    con.execute(
        "insert or replace into seen values (?,?,?)",
        (item.key, f"!{attempts}:{item.fingerprint}" if left else item.fingerprint, row[1] if row else None),
    )
    frames = traceback.extract_tb(e.__traceback__)
    at = ([f for f in frames if f.filename.startswith(PACKAGE)] or frames or [None])[-1]  # recall's own line first
    where = f" at {os.path.relpath(at.filename, PACKAGE)}:{at.lineno}" if at else ""
    then = f"tries again at the next run ({attempts} of {MAX_ATTEMPTS})" if left else "waits for it to change"
    # the key and where it failed, never the exception's text: that can quote the chat
    print(f"recall: could not index {item.key}: {store.error_name(e)}{where}; {then}", file=sys.stderr)
    return False


def _read_since_rebuild(con, selected) -> list[str]:
    """Agents whose every source has been read in full since the chat tables were last
    rebuilt: their archived chats that are not in the index now came from no source."""
    stamps = {r[0] for r in con.execute("select k from meta where k like 'scanned:%'")}
    return [
        agent
        for agent, (_, _, sources) in agents().items()
        if (not selected or agent in selected) and all(f"scanned:{name}" in stamps for name in sources)
    ]


def _scan(con, selected, only, force, max_age, log) -> dict:
    from .. import config, roots

    result = {}
    cfg = config.load()
    found = roots.discover()
    wanted = [r for r in found if roots.option(r, "index", cfg)]  # [roots.NAME] index = false skips one
    store.reparse_shared_cursor(con, found)
    for name, items in all_sources(selected, wanted).items():
        if only and name not in only:
            continue
        stamp_key = f"indexed_at:{name}"
        if max_age is not None:
            row = con.execute("select v from meta where k=?", (stamp_key,)).fetchone()
            if row and time.time() - float(row[0]) < max(max_age, STALE_AFTER[name]):
                continue
        files = _database_files(wanted) if name in OUT_OF_PROCESS else None
        if files is not None and not _whole_read_due(con, name, max_age, force, files):
            if log and not max_age:
                log(f"{name}: unchanged since it was last read")
            continue
        if max_age and name in OUT_OF_PROCESS:  # the first index, from a tool call
            from .. import power

            if power.defer():  # on battery: a later call on power starts it
                continue
            # Stamped now so no other server starts one too; the child waits for this lock.
            con.execute("insert or replace into meta values (?,?)", (stamp_key, str(time.time())))
            con.commit()
            child = subprocess.Popen(
                [sys.executable, "-m", "recall", "index", "--source", name, "--background"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            threading.Thread(target=child.wait, daemon=True).start()  # reap it, so no zombie lingers
            continue
        seen = dict(con.execute("select key, fingerprint from seen"))
        keep = _keeper(con)
        done = skipped = 0
        for item in items():
            if not force and seen.get(item.key) == item.fingerprint:
                skipped += 1
                continue
            done += _read(con, item, keep)
            if done and done % 50 == 0:
                con.commit()
                if log:
                    log(f"{name}: {done} indexed…")
        now = str(time.time())
        con.execute("insert or replace into meta values (?,?)", (stamp_key, now))
        con.execute("insert or replace into meta values (?,?)", (f"scanned:{name}", now))
        if files is not None:  # as they were before the read: a write during it means another read
            con.execute("insert or replace into meta values (?,?)", (f"files_at_read:{name}", files))
        for r in wanted:  # when each root was last read, for `recall doctor`
            if r.agent == name.split("-")[0]:
                con.execute("insert or replace into meta values (?,?)", (f"indexed_at_root:{r.name}", now))
        con.commit()
        result[name] = {"indexed": done, "unchanged": skipped}
    return result


def _whole_read_due(con, name: str, max_age, force: bool, files: str) -> bool:
    """Whether to read an OUT_OF_PROCESS source in full now. A tool call's refresh (a positive
    max_age) reads it only when it has not been read in full since the chat tables were built:
    the first index, and the one after an update rebuilds them. Every other run reads it unless
    its files are as they were at the last full read."""
    read = con.execute("select 1 from meta where k = ?", (f"scanned:{name}",)).fetchone()
    if max_age:
        return not read
    if force or not read:
        return True
    last = con.execute("select v from meta where k = ?", (f"files_at_read:{name}",)).fetchone()
    return not last or last[0] != files


def _database_files(wanted) -> str:
    """The size and modification time of each Cursor chat database and its write-ahead log."""
    from .sources import cursor

    state = []
    for path in cursor.database_files([r for r in wanted if r.agent == "cursor"]):
        try:
            st = os.stat(path)
            state.append([path, st.st_size, st.st_mtime_ns])
        except OSError:
            state.append([path, None, None])
    return json.dumps(state)
