"""The memory job: read new chats, propose memories, fold them into notes, commit.

Runs at setup and then nightly. Every step is resumable: extraction results are
stored per chat and consolidation marks each candidate as it is handled, so a crash
or an exhausted budget simply means the next run carries on from there.

With an API provider that has a batch API (Anthropic, OpenAI), calls go out as batches at
half the price (see llm/batch.py): extraction as one batch, then consolidation in rounds,
every project's next batch of candidates side by side. A run stops at its call budget, its
dollar budget (max_usd, metered providers only) or its time budget, whichever comes first.
"""

import fcntl
import json
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass

from .. import config, contexts, paths
from ..chats import indexer, store
from ..chats.redact import redact
from ..llm import Budget, BudgetExceeded, Client, FatalError, LLMError, batching, fallback, from_spec
from ..llm.batch import Batcher, Call
from . import consolidate, context, extract, links, reach, routing, runs
from .notes import NotesBusy, NoteStore, today, turn

MIN_MESSAGES = 2
READING_SHARE = 0.8  # of a time or dollar budget; the rest is kept for consolidation
MIN_ROUND = 3  # consolidation calls worth a batch; a narrower round (one big project's tail) goes directly
MAX_BATCHED_CALLS = 1000  # extraction calls in one batch (about 100 MB); the rest carry over


@dataclass
class Options:
    model: str | None = None  # one-off override such as "chatgpt:gpt-6-luna"
    max_chats: int | None = None
    max_calls: int | None = None
    max_minutes: int | None = None
    max_usd: float | None = None  # dollar budget; only metered providers (API keys) cost money
    batch: bool = True  # False: never use a batch API, whatever config.toml says
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
        run_id = runs.open_run(con, started)
        log(f"run {run_id} started")  # the log's own mark for this run (api.run_log)
        try:
            return _run(opts, log, con, run_id, started)
        except BaseException as e:
            # A model's or its setup's words, redacted; of anything else only the type, whose text could quote a chat.
            runs.close_failed(con, run_id, redact(str(e))[:300] if isinstance(e, LLMError) else store.error_name(e))
            raise


def _run(opts: Options, log, con, run_id: int, started: float) -> dict:
    moved = None
    try:
        indexer.index(con, max_age=0)
    except sqlite3.OperationalError:
        if not (moved := _moved(con)):
            raise
    _link_commits(con, log)
    settings = config.load()["model"]
    provider = from_spec(opts.model, settings)
    problem = provider.check()
    if problem:
        raise FatalError(problem)
    spare = fallback(settings)
    if spare is not None and (spare.check() or spare.label == provider.label):
        spare = None
    used = {provider.for_purpose(x) for x in ("extract", "consolidate")}
    metered = [p for p in used if p.metered]
    # A call cap guards a plan's rate limits; on an API key the dollar budget is the cap.
    by_dollar = opts.max_usd is not None and all(p.metered and p.priced for p in used)
    budget = Budget(None if by_dollar else opts.max_calls, opts.max_usd)
    client = Client(provider, budget=budget, fallback=spare, log=log)
    log(
        f"model: {provider.label}"
        + (" (private: nothing leaves this machine)" if provider.private else "")
        + (f" · budget ${opts.max_usd:.2f}" if by_dollar else "")
        + (f" · after the API credits run out: {spare.label}" if spare and metered else "")
    )
    if opts.max_usd is not None and any(not p.priced for p in metered):
        log("max_usd is not enforced: recall does not know this model's price (set price = [in, out] in [model])")
    deadline = started + opts.max_minutes * 60 if opts.max_minutes else None
    reading = started + opts.max_minutes * 60 * READING_SHARE if opts.max_minutes else None
    batcher = None
    if opts.batch and batching(settings, provider):
        batcher = Batcher(client, log, deadline=reading)
        log("calls go through the batch API at half price; answers can take minutes")
        batcher.resume()
    stats = Counter()
    busy: list[str] = []  # why the notes could not be finished: another process held them too long
    # Before reading, so a long backlog of chats cannot spend the calls it needs.
    placed = _place(client, log) if opts.fold and contexts.roots() else 0
    if opts.read and not moved:
        if batcher:
            batcher.deadline = reading
        if budget.max_usd is not None and opts.fold:  # as with time, keep a share for consolidation
            budget.max_usd = opts.max_usd * READING_SHARE
        try:
            stats.update(_read(con, client, opts.max_chats, reading, log, batcher))
        except ChatsMoved as e:
            moved = str(e)
        budget.max_usd = opts.max_usd
    if opts.fold and not (moved := moved or _moved(con)):  # routing and said.py read the chat tables
        if batcher:
            batcher.deadline = deadline
        stats.update(_fold(con, client, log, placed, batcher, deadline, busy))
    if moved:
        log(moved)
    cost = round(client.usd, 4) if metered and not client.unpriced else None
    record = dict(stats)
    if batcher and batcher.batches:
        record.update(batches=batcher.batches, batched_calls=batcher.requests)
    if cost is not None:
        record["cost_usd"] = cost
        batched = batcher.requests if batcher else 0
        log(f"cost: ${cost:.4f}" + (f" ({batched} calls batched at half price)" if batched else ""))
    con.execute(
        "update dream_runs set finished=?, backend=?, chats=?, candidates=?, stats=?, tokens_in=?, tokens_out=?,"
        " llm_calls=?, error=?, status=? where id=?",
        (
            time.time(),
            provider.label,
            stats["chats"],
            stats["candidates"],
            json.dumps(record),
            client.tokens_in,
            client.tokens_out,
            client.calls,
            moved or (f"{busy[0]}; the next run finishes the notes" if busy else None),
            "failed" if busy else "done",
            run_id,
        ),
    )
    con.commit()
    return {
        **stats,
        "model calls": client.calls,
        "tokens in": client.tokens_in,
        "tokens out": client.tokens_out,
        **({"cost (USD)": cost} if cost is not None else {}),
        **({"batches": batcher.batches} if batcher and batcher.batches else {}),
        "minutes": round((time.time() - started) / 60, 1),
    }


def _link_commits(con, log) -> None:
    """Which chats produced which commits. Local, git read-only, no model calls."""
    from .. import commits

    try:
        r = commits.update(con)
        if r.get("links_written"):
            log(f"commit links: {r['links_written']} written in {r['repos']} repos ({r['seconds']}s)")
    except Exception as e:  # never costs the night's memory run
        con.rollback()
        log(f"commit links skipped: {e!r}")


class ChatsMoved(Exception):
    """The chat tables stopped being this release's while the run read them."""


def _moved(con) -> str | None:
    """Why this run leaves the chats alone, when the chat tables are no longer this release's: a recall
    process an upgrade outlived (an agent's MCP server) rebuilt them for its own release. Reading them now
    would fail or find them half full; the next run carries on."""
    built_by = store.schema(con)
    if built_by == store.CHAT_SCHEMA_VERSION:
        return None
    return (
        f"chats left for the next run: an older recall rebuilt the chat index during this run (chat schema"
        f" {built_by}); restart agents started before recall was last upgraded"
    )


def ensure_tables(con) -> None:
    extract.ensure_tables(con)
    runs.ensure_table(con)


def pending_chats(con) -> list:
    """Interactive chats not yet read at the current prompt version, newest first.
    Of a chat copied between agents, only the fullest copy is read. Only chats of a root the person
    has opted into are read ([roots.NAME] mine = true; the default roots are in unless mine = false),
    whether or not that root is still found. A chat with no root yet is read: it was indexed before
    roots existed and is the default's."""
    from .. import roots

    names = roots.mined()
    marks = ",".join("?" * len(names))
    room = f" and (s.root is null or s.root in ({marks}))"
    rows = con.execute(
        f"""select s.* from sessions s left join extractions e on e.session_id = s.id
           where s.origin = 'interactive' and s.n_messages >= ?{room}
             and (e.session_id is null or e.prompt_version != ? or e.at < s.updated_at or e.status = 'failed')
           order by s.updated_at desc""",
        (MIN_MESSAGES, *names, extract.PROMPT_VERSION),
    ).fetchall()
    fullest = {}
    for r in con.execute(
        "select fingerprint, id from sessions where fingerprint is not null and origin = 'interactive' "
        + room.replace("s.root", "root")
        + " order by n_messages desc, updated_at asc",
        names,
    ):
        fullest.setdefault(r["fingerprint"], r["id"])
    return [r for r in rows if not r["fingerprint"] or fullest[r["fingerprint"]] == r["id"]]


def _read(con, client: Client, max_chats: int | None, deadline: float | None, log, batcher=None) -> Counter:
    batched = batcher is not None and client.provider.for_purpose("extract").supports_batch
    try:
        chats = pending_chats(con)
        if max_chats:
            chats = chats[:max_chats]
        if batched:
            # Oldest first: chats that arrive before the next run join the last call instead of shifting
            # every call's chats, so answers a left-running batch gave are still the next run's cache hits.
            chats = sorted(chats, key=lambda s: s["updated_at"])
        messages = "select seq, role, text from messages where session_id=? order by seq"
        items = [(s, con.execute(messages, (s["id"],)).fetchall()) for s in chats]
    except sqlite3.OperationalError:
        if not (moved := _moved(con)):
            raise
        raise ChatsMoved(moved) from None
    if moved := _moved(con):  # rebuilt mid-read: chats whose messages came back empty would be marked skipped
        raise ChatsMoved(moved)
    batches, skipped = extract.plan(items, client.provider.profile)
    label = client.provider.for_purpose("extract").label
    for s in skipped:
        extract.store(con, s, "skipped", [], None, label)
    con.commit()  # nothing may hold a write lock while workers call the model
    log(f"reading {len(chats) - len(skipped)} chats in {len(batches)} model calls")
    collector = extract.Collector(batches)
    stats = Counter()
    stop_reason = None
    reasons = set()  # why calls failed, each said once in the log

    def finish(session, candidates, digest_hash, status="done", error=None):
        extract.store(con, session, status, candidates, digest_hash, label, error)
        con.commit()
        stats["chats" if status == "done" else "failed"] += 1
        stats["candidates"] += len(candidates)

    def handle(batch, result, error) -> None:
        nonlocal stop_reason
        if isinstance(error, BudgetExceeded):
            stop_reason = stop_reason or str(error)
        elif isinstance(error, (LLMError, OSError)):  # one bad batch must not sink the run
            failed = 0
            for unit in batch:
                session = collector.fail(unit)
                if session is not None:
                    finish(session, [], None, "failed", str(error)[:300])
                    failed += 1
            reason = str(error).split("\n")[0][:300]
            if failed and reason not in reasons and len(reasons) < 5:
                reasons.add(reason)
                log(f"reading failed for {failed} chats ({reason}); the next run tries them again")
        elif error is not None:
            raise error
        else:
            for unit, candidates in result:
                complete = collector.add(unit, candidates)
                if complete:
                    finish(*complete)

    if batched:
        batches = batches[:MAX_BATCHED_CALLS]  # one batch; the batcher keeps the time budget
        calls = [Call("extract", extract.SYSTEM, extract.prompt(b), extract.SCHEMA) for b in batches]
        for batch, answer in zip(batches, batcher.ask(calls), strict=True):
            if isinstance(answer, BaseException):
                handle(batch, None, answer)
            else:
                try:
                    parsed = extract.answers(batch, answer)
                except (KeyError, TypeError, ValueError, AttributeError) as e:  # a malformed answer fails its batch
                    handle(batch, None, LLMError(f"malformed extraction answer: {e!r}"))
                else:
                    handle(batch, parsed, None)
    else:
        pool = ThreadPoolExecutor(client.provider.profile.workers)
        futures = {pool.submit(extract.run_batch, client, b): b for b in batches}
        try:
            for done, future in enumerate(as_completed(futures), 1):
                if future.cancelled():  # dropped once a budget ran out; its chats carry over
                    continue
                try:
                    handle(futures[future], future.result(), None)
                except (BudgetExceeded, LLMError, OSError) as e:
                    handle(futures[future], None, e)
                if deadline and time.time() > deadline:
                    stop_reason = stop_reason or "time budget reached"
                if stop_reason:
                    for f in futures:
                        f.cancel()
                if done % 5 == 0:
                    read, found = stats["chats"], stats["candidates"]
                    log(f"  {done}/{len(batches)} calls · {read} chats read · {found} candidates")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    con.commit()
    if stop_reason:
        log(f"stopped early ({stop_reason}); unread chats carry over to the next run")
    failed = f" · {stats['failed']} failed, tried again next run" if stats["failed"] else ""
    log(f"read {stats['chats']} chats · {stats['candidates']} candidate memories{failed}")
    return stats


def _place(client: Client, log, notes_store: NoteStore | None = None, notes: dict | None = None) -> int:
    """Judge where each new working preference applies (reach.py); returns how many."""
    notes_store = notes_store or NoteStore()
    try:
        return reach.run(client, notes_store, notes_store.load() if notes is None else notes)
    except (BudgetExceeded, LLMError, NotesBusy) as e:
        log(f"judging where preferences apply stopped early ({e}); the next run continues")
        return 0


def _fold(
    con, client: Client, log, placed: int = 0, batcher=None, deadline: float | None = None, busy: list | None = None
) -> Counter:
    """`busy` collects why, when another process held the notes so long that this run could not finish them."""
    busy = [] if busy is None else busy
    try:
        return _fold_notes(con, client, log, placed, batcher, deadline, busy)
    except NotesBusy as e:
        log(f"notes left for the next run: {e}")
        busy.append(str(e))
        return Counter()


def _fold_notes(
    con, client: Client, log, placed: int = 0, batcher=None, deadline: float | None = None, busy: list | None = None
) -> Counter:
    """The notes lock is taken only to write (notes.turn): while a model answers, `recall remember`
    and the app can change notes, and the close below reads them again so that what landed is kept."""
    notes_store = NoteStore()
    with turn():
        notes = notes_store.load()
        deleted = notes_store.reconcile(notes)
    if deleted:
        log(f"{deleted} notes were deleted by hand; they will not be learned again")
    moved = routing.unify_moved(con) + routing.reroute_mentions(con)
    if moved:
        log(f"moved {moved} candidates to the project they are about")
    con.commit()
    fold = consolidate.Consolidation(con, notes_store, notes, log)
    try:
        if batcher is not None and client.provider.for_purpose("consolidate").supports_batch:
            _in_rounds(fold, batcher, log)
        else:
            consolidate.drain(fold, client, deadline)
    except (BudgetExceeded, LLMError, NotesBusy) as e:  # what was applied stands, and the close below is still tried
        log(f"consolidation stopped early ({e}); the next run continues from here")
        if isinstance(e, NotesBusy) and busy is not None:
            busy.append(str(e))
    stats = fold.finish()
    changes = {k: v for k, v in stats.items() if v and k != "batches"}
    if (
        contexts.roots() and not busy
    ):  # what tonight wrote or reworded: before the indexes, so it loads in the right place
        placed += _place(client, log, notes_store, notes)
    if placed:
        changes["placed"] = placed
    with turn():
        notes = notes_store.load()
        if refiled := routing.refile(con, notes_store, notes):
            changes["refiled"] = refiled
        relinked = links.relink(notes_store, notes)
        if relinked:
            changes["relinked"] = relinked
        summary = ", ".join(f"{v} {k}" for k, v in changes.items())
        # Always rebuilt: the indexes also follow config.toml ([contexts]), which no note records.
        notes_store.compile(notes)
        sha = notes_store.commit(f"dream {today()}: {summary or 'reindexed'}")
        notes_store.reconcile(notes)
    ending = (
        f" · commit {sha}"
        if sha
        else f" · not committed: {notes_store.commit_failed}"
        if notes_store.commit_failed
        else ""
    )
    log(f"memory: {summary or 'no changes'}{ending}")
    context.index_notes(con, notes)
    return Counter(changes)


def _in_rounds(fold: consolidate.Consolidation, batcher: Batcher, log) -> None:
    """Consolidation in rounds: every scope's next request goes out in one batch, and the answers
    are applied in turn. A scope's requests stay in order; a scope whose request fails stops for
    this run, and the others carry on."""
    active, stopped = list(fold.groups), []
    while active:
        requests = [(g, fold.prepare(g)) for g in active]
        requests = [(g, r) for g, r in requests if r is not None]
        answers = batcher.ask([r for _, r in requests], min_batch=MIN_ROUND)
        active = []
        for (g, _), answer in zip(requests, answers, strict=True):
            if isinstance(answer, (BudgetExceeded, LLMError)):
                stopped.append(answer)
                continue
            if isinstance(answer, BaseException):
                raise answer
            fold.apply(g, answer)
            active.append(g)
    if stopped:  # the other scopes' changes stand, and are committed below
        log(f"consolidation stopped early in {len(stopped)} scope(s) ({stopped[0]}); the next run continues")


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
