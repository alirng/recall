"""Keep the chat index in step with each agent's history, re-reading only what changed."""

import contextlib
import fcntl
import os
import subprocess
import sys
import threading
import time

from .. import paths
from . import scrub, store
from .sources import agents, all_sources

# File-based sources are cheap to re-scan; Cursor's legacy store means reading a
# multi-GB database, so it refreshes less often.
STALE_AFTER = {"claude": 60, "codex": 60, "cursor": 60, "cursor-legacy": 1800}
# Scanning these leaves hundreds of MB with the allocator after the work is done, so a
# long-running server hands them to a short-lived `recall index` instead.
OUT_OF_PROCESS = {"cursor-legacy"}


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
    """Whether a parsed chat may be indexed: not forgotten, and not older than a retention
    period an organisation sets for its agent."""
    from .. import forget, retention

    gone, cutoffs = forget.forgotten(con), retention.cutoffs()

    def keep(session) -> bool:
        cutoff = cutoffs.get(session.agent)
        return session.id not in gone and (cutoff is None or (session.updated_at or 0) >= cutoff)

    return keep


def _read(con, item, keep) -> bool:
    """Parse one chat and index it. False when there was nothing to index."""
    try:
        session = item.load()
    except Exception as e:  # one unreadable chat must not stop the run
        print(f"recall: skipped {item.key}: {e!r}", file=sys.stderr)
        session = None
    indexed = bool(session and session.messages and keep(session))
    if indexed:
        store.upsert(con, session)
    con.execute(
        "insert or replace into seen values (?,?,?)",
        (item.key, item.fingerprint, session.id if session else None),
    )
    return indexed


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
        if max_age and name in OUT_OF_PROCESS:
            # Stamped now so no other server starts one too; the child waits for this lock.
            con.execute("insert or replace into meta values (?,?)", (stamp_key, str(time.time())))
            con.commit()
            child = subprocess.Popen(
                [sys.executable, "-m", "recall", "index", "--source", name],
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
        for r in wanted:  # when each root was last read, for `recall doctor`
            if r.agent == name.split("-")[0]:
                con.execute("insert or replace into meta values (?,?)", (f"indexed_at_root:{r.name}", now))
        con.commit()
        result[name] = {"indexed": done, "unchanged": skipped}
    return result
