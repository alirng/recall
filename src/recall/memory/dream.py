"""The memory job: read new chats, propose memories, fold them into notes, commit.

Runs at setup and then nightly. Every step is resumable: extraction results are
stored per chat and consolidation marks each candidate as it is handled, so a crash
or an exhausted budget simply means the next run carries on from there.
"""

import fcntl
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass

from .. import config, paths
from ..chats import indexer, store
from ..llm import BudgetExceeded, Client, FatalError, LLMError, from_spec
from . import consolidate, context, extract, routing
from .notes import NoteStore, today

MIN_MESSAGES = 2
READING_SHARE = 0.8  # of a time budget; the rest is kept for consolidation


@dataclass
class Options:
    model: str | None = None  # one-off override such as "chatgpt:gpt-6-luna"
    max_chats: int | None = None
    max_calls: int | None = None
    max_minutes: int | None = None
    read: bool = True  # read new chats into candidates
    fold: bool = True  # fold candidates into notes


def run(opts: Options, log=print) -> dict:
    with _exclusive() as acquired:
        if not acquired:
            log("another memory run is in progress")
            return {}
        con = store.connect()
        ensure_tables(con)
        started = time.time()
        indexer.index(con, max_age=0)
        provider = from_spec(opts.model, config.load()["model"])
        problem = provider.check()
        if problem:
            raise FatalError(problem)
        client = Client(provider, max_calls=opts.max_calls)
        log(f"model: {provider.label}" + (" (private: nothing leaves this machine)" if provider.private else ""))
        deadline = started + opts.max_minutes * 60 * READING_SHARE if opts.max_minutes else None
        stats = Counter()
        if opts.read:
            stats.update(_read(con, client, opts.max_chats, deadline, log))
        if opts.fold:
            stats.update(_fold(con, client, log))
        con.execute(
            "insert into dream_runs(started, finished, backend, chats, candidates, stats, tokens_in, tokens_out,"
            " llm_calls, error) values (?,?,?,?,?,?,?,?,?,?)",
            (
                started,
                time.time(),
                provider.label,
                stats["chats"],
                stats["candidates"],
                json.dumps(stats),
                client.tokens_in,
                client.tokens_out,
                client.calls,
                None,
            ),
        )
        con.commit()
        return {
            **stats,
            "model calls": client.calls,
            "tokens in": client.tokens_in,
            "tokens out": client.tokens_out,
            "minutes": round((time.time() - started) / 60, 1),
        }


def ensure_tables(con) -> None:
    extract.ensure_tables(con)
    con.execute(
        """create table if not exists dream_runs (
             id integer primary key, started real, finished real, backend text, chats int,
             candidates int, stats text, tokens_in int, tokens_out int, llm_calls int, error text)"""
    )


def pending_chats(con) -> list:
    """Interactive chats not yet read at the current prompt version, newest first.
    Of a chat copied between agents, only the fullest copy is read."""
    rows = con.execute(
        """select s.* from sessions s left join extractions e on e.session_id = s.id
           where s.origin = 'interactive' and s.n_messages >= ?
             and (e.session_id is null or e.prompt_version != ? or e.at < s.updated_at or e.status = 'failed')
           order by s.updated_at desc""",
        (MIN_MESSAGES, extract.PROMPT_VERSION),
    ).fetchall()
    fullest = {}
    for r in con.execute(
        "select fingerprint, id from sessions where fingerprint is not null and origin = 'interactive' "
        "order by n_messages desc, updated_at asc"
    ):
        fullest.setdefault(r["fingerprint"], r["id"])
    return [r for r in rows if not r["fingerprint"] or fullest[r["fingerprint"]] == r["id"]]


def _read(con, client: Client, max_chats: int | None, deadline: float | None, log) -> Counter:
    chats = pending_chats(con)
    if max_chats:
        chats = chats[:max_chats]
    items = [
        (s, con.execute("select seq, role, text from messages where session_id=? order by seq", (s["id"],)).fetchall())
        for s in chats
    ]
    batches, skipped = extract.plan(items, client.provider.profile)
    label = client.provider.label
    for s in skipped:
        extract.store(con, s, "skipped", [], None, label)
    con.commit()  # nothing may hold a write lock while workers call the model
    log(f"reading {len(chats) - len(skipped)} chats in {len(batches)} model calls")
    collector = extract.Collector(batches)
    stats = Counter()

    def finish(session, candidates, digest_hash, status="done", error=None):
        extract.store(con, session, status, candidates, digest_hash, label, error)
        con.commit()
        stats["chats"] += 1
        stats["candidates"] += len(candidates)

    pool = ThreadPoolExecutor(client.provider.profile.workers)
    futures = {pool.submit(extract.run_batch, client, b): b for b in batches}
    stop_reason = None
    try:
        for done, future in enumerate(as_completed(futures), 1):
            try:
                for unit, candidates in future.result():
                    complete = collector.add(unit, candidates)
                    if complete:
                        finish(*complete)
            except BudgetExceeded as e:
                stop_reason = stop_reason or str(e)
            except (LLMError, OSError) as e:  # one bad batch must not sink the run
                for unit in futures[future]:
                    session = collector.fail(unit)
                    if session is not None:
                        finish(session, [], None, "failed", str(e)[:300])
            if deadline and time.time() > deadline:
                stop_reason = stop_reason or "time budget reached"
            if stop_reason:
                for f in futures:
                    f.cancel()
            if done % 5 == 0:
                log(f"  {done}/{len(batches)} calls · {stats['chats']} chats read · {stats['candidates']} candidates")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        con.commit()
    if stop_reason:
        log(f"stopped early ({stop_reason}); unread chats carry over to the next run")
    log(f"read {stats['chats']} chats · {stats['candidates']} candidate memories")
    return stats


def _fold(con, client: Client, log) -> Counter:
    notes_store = NoteStore()
    notes = notes_store.load()
    deleted = notes_store.reconcile(notes)
    if deleted:
        log(f"{deleted} notes were deleted by hand; they will not be learned again")
    moved = routing.unify_moved(con) + routing.reroute_mentions(con)
    if moved:
        log(f"moved {moved} candidates to the project they are about")
    con.commit()
    stats = Counter()
    try:
        stats = consolidate.run(con, client, notes_store, notes, log=log)
    except (BudgetExceeded, LLMError) as e:
        log(f"consolidation stopped early ({e}); the next run continues from here")
    changes = {k: v for k, v in stats.items() if v and k != "batches"}
    summary = ", ".join(f"{v} {k}" for k, v in changes.items())
    if changes or deleted:
        notes_store.compile(notes)
        sha = notes_store.commit(f"dream {today()}: {summary or 'reindexed'}")
        log(f"memory: {summary or 'reindexed'}" + (f" · commit {sha}" if sha else ""))
    else:
        log("memory: no changes")
    notes_store.reconcile(notes)
    context.index_notes(con, notes)
    return Counter(changes)


@contextmanager
def _exclusive():
    """One memory run at a time, whether started nightly or by hand."""
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "dream.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
