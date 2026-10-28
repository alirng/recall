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
from .sources import Incomplete, agents, all_sources

STALE_AFTER = {"claude": 60, "codex": 60, "cursor": 60, "cursor-legacy": 3 * 3600}
# Cursor's older chats are rows in its state.vscdb, a database of several GB that cannot say which chats
# changed: finding out means reading every chat's header. These sources are read by the nightly run and by
# `recall index`, and by a tool call only on the first index or once READ_AGAIN_AFTER has passed since the
# last full read (a machine without the nightly run, or whose run waited for power), at most every
# STALE_AFTER, in a short-lived `recall index` (the scan leaves hundreds of MB with the allocator). Every
# run skips them while the database's files are as they were at the last full read.
OUT_OF_PROCESS = {"cursor-legacy"}
READ_AGAIN_AFTER = 24 * 3600
CHILD_DEADLINE = 15 * 60  # that background `recall index` stops here; the next one carries on
# A chat that fails to index is read again after these waits, then left until its file changes. Its
# fingerprint in `seen` meanwhile is "!<failures>@<when to try again>:<fingerprint>", which no file matches.
RETRY_AFTER = (60, 600, 3600)
MAX_ATTEMPTS = len(RETRY_AFTER) + 1
_FAILED = re.compile(r"!(\d+)@(\d+):(.*)", re.S)
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
    if max_age:  # tool calls come through here: a memory run that waited for power starts once there is power
        from .. import power

        power.catch_up()
    return result


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
    found = roots.discover()  # once a run: it reads the shell's rc files, about 20 ms
    result = _scan(con, selected, only, force, max_age, log, found)
    retention.apply(con)  # before restoring, so nothing past an organisation's period comes back
    restored = store.restore(con, _read_since_rebuild(con, selected, found))
    if restored:
        result["archive"] = {"indexed": 0, "unchanged": 0, "restored": restored}
    # After the restore: chats indexed before roots existed, and restored ones, come back without a root.
    store.backfill_roots(con, found)
    return result


def _keeper(con):
    """Whether a parsed chat may be indexed: not forgotten, nor a subagent of a forgotten chat, and
    not older than a retention period an organisation sets for its agent."""
    from .. import forget, retention

    gone, cutoffs = forget.forgotten(con), retention.cutoffs()

    def keep(session) -> bool:
        cutoff = cutoffs.get(session.agent)
        forgotten = session.id in gone or session.parent in gone
        return not forgotten and (cutoff is None or (session.updated_at or 0) >= cutoff)

    return keep


def _read(con, item, keep) -> bool:
    """Parse one chat and index it. False when there was nothing to index. One bad chat must not
    stop the run, nor every run after it: a chat that fails is logged by its key and read again
    after each of RETRY_AFTER while its file stays the same (Codex may have been compressing it),
    and is then left until the file changes."""
    try:
        session = item.load()
        indexed = bool(session and session.messages and keep(session))
    except Exception as e:
        if isinstance(e, sqlite3.OperationalError) and (getattr(e, "sqlite_errorcode", None) or 0) & 0xFF in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):  # the agent's own database is busy: no fault of the chat's, so no failure counted
            print(f"recall: {item.key}: its database is busy; read again at the next run", file=sys.stderr)
            return False
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
    """Count a failed read in `seen` (see RETRY_AFTER), and commit it: a run that dies later must not
    lose the count."""
    row = con.execute("select fingerprint, session_id from seen where key = ?", (item.key,)).fetchone()
    tried = _FAILED.fullmatch(row[0] or "") if row else None
    failures = int(tried[1]) + 1 if tried and tried[3] == item.fingerprint else 1
    wait = RETRY_AFTER[failures - 1] if failures <= len(RETRY_AFTER) else None
    con.execute(
        "insert or replace into seen values (?,?,?)",
        (item.key, f"!{failures}@{int(time.time() + (wait or 0))}:{item.fingerprint}", row[1] if row else None),
    )
    con.commit()
    frames = traceback.extract_tb(e.__traceback__)
    at = ([f for f in frames if f.filename.startswith(PACKAGE)] or frames or [None])[-1]  # recall's own line first
    where = f" at {os.path.relpath(at.filename, PACKAGE)}:{at.lineno}" if at else ""
    then = (
        f"tries again in {wait // 60} min ({failures} of {MAX_ATTEMPTS})"
        if wait
        else "left until its file changes (`recall index --force` tries it now)"
    )
    # the key and where it failed, never the exception's text: that can quote the chat
    print(f"recall: could not index {item.key}: {store.error_name(e)}{where}; {then}", file=sys.stderr)
    return False


def _unchanged(stored: str | None, fingerprint: str, now: float) -> bool:
    """Whether a chat need not be read: read at this fingerprint, or failed at it and not due again."""
    if stored == fingerprint:
        return True
    tried = _FAILED.fullmatch(stored or "")
    return bool(tried) and tried[3] == fingerprint and (int(tried[1]) >= MAX_ATTEMPTS or now < int(tried[2]))


def parked() -> list[str]:
    """Chats left after failing MAX_ATTEMPTS times, until their files change: their keys, read from
    the index opened read-only."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)) as db:
            rows = db.execute("select key, fingerprint from seen where fingerprint like '!%'").fetchall()
    except sqlite3.Error:  # no index yet
        return []
    return [key for key, stored in rows if (tried := _FAILED.fullmatch(stored)) and int(tried[1]) >= MAX_ATTEMPTS]


def _read_since_rebuild(con, selected, found) -> list[str]:
    """Agents whose every source has been read in full since the chat tables were last
    rebuilt: their archived chats that are not in the index now came from no source."""
    stamps = {r[0] for r in con.execute("select k from meta where k like 'scanned:%'")}
    return [
        agent
        for agent, (_, _, sources) in agents(found).items()
        if (not selected or agent in selected) and all(f"scanned:{name}" in stamps for name in sources)
    ]


def _scan(con, selected, only, force, max_age, log, found) -> dict:
    from .. import config, roots

    result = {}
    cfg = config.load()
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
        if max_age and name in OUT_OF_PROCESS:  # from a tool call: in the background
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
        started = time.time()
        try:
            for item in items():
                if not force and _unchanged(seen.get(item.key), item.fingerprint, started):
                    skipped += 1
                    continue
                done += _read(con, item, keep)
                # Each chat on its own: the next one's parse, which can take seconds, must not hold the
                # write lock that a tool call in this server (the notes index, commit links) is waiting for.
                con.commit()
                if done and done % 50 == 0 and log:
                    log(f"{name}: {done} indexed…")
        except Incomplete as e:  # not a full read: nothing is stamped, so the next run reads it again
            print(f"recall: {name}: {e}", file=sys.stderr)
            con.commit()
            result[name] = {"indexed": done, "unchanged": skipped}
            continue
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
    """Whether to read an OUT_OF_PROCESS source in full now (see OUT_OF_PROCESS). Always when it has
    not been read in full since the chat tables were built (the first index, and the one after an
    update rebuilds them), or when a chat of it that failed is due to be read again."""
    stamps = dict(
        con.execute("select k, v from meta where k in (?, ?)", (f"scanned:{name}", f"files_at_read:{name}")).fetchall()
    )
    read = stamps.get(f"scanned:{name}")
    now = time.time()
    retry = any(
        (tried := _FAILED.fullmatch(stored)) and int(tried[1]) < MAX_ATTEMPTS and now >= int(tried[2])
        for (stored,) in con.execute("select fingerprint from seen where key like '%#%' and fingerprint like '!%'")
    )  # database rows ("<db>#<key>") are this kind of source's
    if force or not read or retry:
        return True
    changed = stamps.get(f"files_at_read:{name}") != files
    return changed and (not max_age or now - float(read) > READ_AGAIN_AFTER)


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
