"""Keep the chat index in step with each agent's history, re-reading only what changed."""

import fcntl
import os
import sys
import time

from .. import paths
from . import store
from .sources import all_sources

# File-based sources are cheap to re-scan; Cursor's legacy store means reading a
# multi-GB database, so it refreshes less often.
STALE_AFTER = {"claude": 60, "codex": 60, "cursor": 60, "cursor-legacy": 1800}


def index(con, only=None, force=False, max_age=None, log=None) -> dict:
    """Incrementally index sources. Returns {source: {"indexed": n, "unchanged": n}}.

    With max_age, only sources not refreshed in the last max_age (or their own
    STALE_AFTER, whichever is longer) seconds are scanned. A lock file stops two
    agents' servers from indexing at the same time; the loser just skips.
    """
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "index.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if max_age is not None else 0))
        except BlockingIOError:
            return {}
        return _index(con, only, force, max_age, log)  # the lock is released when the file closes


def _index(con, only, force, max_age, log) -> dict:
    from .. import config

    result = {}
    for name, items in all_sources(config.load()["sources"]).items():
        if only and name not in only:
            continue
        stamp_key = f"indexed_at:{name}"
        if max_age is not None:
            row = con.execute("select v from meta where k=?", (stamp_key,)).fetchone()
            if row and time.time() - float(row[0]) < max(max_age, STALE_AFTER[name]):
                continue
        seen = dict(con.execute("select key, fingerprint from seen"))
        done = skipped = 0
        for item in items():
            if not force and seen.get(item.key) == item.fingerprint:
                skipped += 1
                continue
            try:
                session = item.load()
            except Exception as e:  # one unreadable chat must not stop the run
                print(f"recall: skipped {item.key}: {e!r}", file=sys.stderr)
                session = None
            if session and session.messages:
                store.upsert(con, session)
                done += 1
            con.execute(
                "insert or replace into seen values (?,?,?)",
                (item.key, item.fingerprint, session.id if session else None),
            )
            if done and done % 50 == 0:
                con.commit()
                if log:
                    log(f"{name}: {done} indexed…")
        con.execute("insert or replace into meta values (?,?)", (stamp_key, str(time.time())))
        con.commit()
        result[name] = {"indexed": done, "unchanged": skipped}
    return result
