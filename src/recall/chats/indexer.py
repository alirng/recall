"""Keep the chat index in step with each agent's history, re-reading only what changed."""

import fcntl
import os
import subprocess
import sys
import threading
import time

from .. import paths
from . import store
from .sources import all_sources

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
        result = _index(con, only, force, max_age, log)
        if max_age is None or any(r["indexed"] for r in result.values()):
            from .. import embeddings

            embeddings.refresh(con, background=max_age is not None, log=log)
        return result  # the lock is released when the file closes


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
