"""The record of memory runs: one dream_runs row, written before anything can fail and closed however
the run ends, so a run that failed is told from one that did not happen. status: running, done or
failed. A run killed outright stays "running" with a pid that is gone, which status() reads as crashed,
and the next run marks it so.

Kept apart from dream.py so that `recall api` and `recall doctor` can read a run's state without
importing the pipeline.
"""

import contextlib
import os
import sqlite3
import time

from .. import paths

STOPPED = "the run stopped before finishing"  # the record of one that left no word of its own
OVERDUE_DAYS = 3  # with a run scheduled, this long without one starting is worth saying


def ensure_table(con) -> None:
    con.execute(
        """create table if not exists dream_runs (
             id integer primary key, started real, finished real, backend text, chats int,
             candidates int, stats text, tokens_in int, tokens_out int, llm_calls int, error text,
             status text, pid int)"""
    )
    have = {r[1] for r in con.execute("pragma table_info(dream_runs)")}
    for column, kind in (("status", "text"), ("pid", "int")):
        if column not in have:
            with contextlib.suppress(sqlite3.OperationalError):  # another process added it just now
                con.execute(f"alter table dream_runs add column {column} {kind}")


def open_run(con, started: float) -> int:
    """Record a run that is starting. The caller holds dream.lock, so a run still marked running
    ended without saying so."""
    con.execute("update dream_runs set status='crashed', error=coalesce(error, ?) where status='running'", (STOPPED,))
    run_id = con.execute(
        "insert into dream_runs(started, status, pid) values (?, 'running', ?)", (started, os.getpid())
    ).lastrowid
    con.commit()
    return run_id


def close_failed(con, run_id: int, why: str) -> None:
    with contextlib.suppress(sqlite3.Error):
        con.rollback()
        con.execute("update dream_runs set finished=?, status='failed', error=? where id=?", (time.time(), why, run_id))
        con.commit()


def status(row) -> str:
    """running, done, failed or crashed, for a dream_runs row. A row from before runs had a status is done."""
    keys = row.keys()
    state = row["status"] if "status" in keys else None
    if state == "running" and not _alive(row["pid"]):
        return "crashed"
    return state or "done"


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists, and is not ours
        pass
    return True


def health() -> list[str]:
    """What `recall doctor` says about the memory run and the notes folder: nothing when all is well."""
    from ..integrations import scheduler
    from .notes import NoteStore

    out = []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("select * from dream_runs order by id desc limit 1").fetchone()
    except sqlite3.Error:
        row = None
    if row:
        state, at = status(row), row["started"] or 0
        when = time.strftime("%b %d %H:%M", time.localtime(at))
        if state == "failed":
            out.append(f"The last memory run failed ({when}): {row['error']}. `recall dream` runs it again.")
        elif state == "crashed":
            out.append(f"The last memory run stopped before finishing ({when}). The next run carries on.")
        elif time.time() - at > OVERDUE_DAYS * 86400 and scheduler.status() != "not scheduled":
            out.append(f"No memory run has started since {when}, though one is scheduled.")
    if os.path.isdir(paths.MEMORY):
        bad = NoteStore().unreadable()
        if bad:
            names = ", ".join(os.path.basename(p) for p in bad[:3]) + (" …" if len(bad) > 3 else "")
            out.append(f"{len(bad)} note file(s) have a header recall cannot read, so they are left out: {names}")
        lock = os.path.join(paths.MEMORY, ".git", "index.lock")
        if os.path.exists(lock):
            out.append(
                "The notes folder's git is locked, so changes may not be committed."
                f" If no git command is running, remove {lock}"
            )
    return out
