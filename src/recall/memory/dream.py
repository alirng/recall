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
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass

from .. import config, contexts, paths
from ..chats import indexer, store
from ..llm import Budget, BudgetExceeded, Client, FatalError, LLMError, batching, fallback, from_spec
from ..llm.batch import Batcher, Call
from . import consolidate, context, extract, links, reach, routing
from .notes import NoteStore, today, writing

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
        indexer.index(con, max_age=0)
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
        # Before reading, so a long backlog of chats cannot spend the calls it needs.
        placed = _place(client, log) if opts.fold and contexts.roots() else 0
        if opts.read:
            if batcher:
                batcher.deadline = reading
            if budget.max_usd is not None and opts.fold:  # as with time, keep a share for consolidation
                budget.max_usd = opts.max_usd * READING_SHARE
            stats.update(_read(con, client, opts.max_chats, reading, log, batcher))
            budget.max_usd = opts.max_usd
        if opts.fold:
            if batcher:
                batcher.deadline = deadline
            stats.update(_fold(con, client, log, placed, batcher))
        cost = round(client.usd, 4) if metered and not client.unpriced else None
        record = dict(stats)
        if batcher and batcher.batches:
            record.update(batches=batcher.batches, batched_calls=batcher.requests)
        if cost is not None:
            record["cost_usd"] = cost
            batched = batcher.requests if batcher else 0
            log(f"cost: ${cost:.4f}" + (f" ({batched} calls batched at half price)" if batched else ""))
        con.execute(
            "insert into dream_runs(started, finished, backend, chats, candidates, stats, tokens_in, tokens_out,"
            " llm_calls, error) values (?,?,?,?,?,?,?,?,?,?)",
            (
                started,
                time.time(),
                provider.label,
                stats["chats"],
                stats["candidates"],
                json.dumps(record),
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


def ensure_tables(con) -> None:
    extract.ensure_tables(con)
    con.execute(
        """create table if not exists dream_runs (
             id integer primary key, started real, finished real, backend text, chats int,
             candidates int, stats text, tokens_in int, tokens_out int, llm_calls int, error text)"""
    )


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
    chats = pending_chats(con)
    if max_chats:
        chats = chats[:max_chats]
    if batched:
        # Oldest first: chats that arrive before the next run join the last call instead of shifting
        # every call's chats, so answers a left-running batch gave are still the next run's cache hits.
        chats = sorted(chats, key=lambda s: s["updated_at"])
    items = [
        (s, con.execute("select seq, role, text from messages where session_id=? order by seq", (s["id"],)).fetchall())
        for s in chats
    ]
    batches, skipped = extract.plan(items, client.provider.profile)
    label = client.provider.for_purpose("extract").label
    for s in skipped:
        extract.store(con, s, "skipped", [], None, label)
    con.commit()  # nothing may hold a write lock while workers call the model
    log(f"reading {len(chats) - len(skipped)} chats in {len(batches)} model calls")
    collector = extract.Collector(batches)
    stats = Counter()
    stop_reason = None

    def finish(session, candidates, digest_hash, status="done", error=None):
        extract.store(con, session, status, candidates, digest_hash, label, error)
        con.commit()
        stats["chats"] += 1
        stats["candidates"] += len(candidates)

    def handle(batch, result, error) -> None:
        nonlocal stop_reason
        if isinstance(error, BudgetExceeded):
            stop_reason = stop_reason or str(error)
        elif isinstance(error, (LLMError, OSError)):  # one bad batch must not sink the run
            for unit in batch:
                session = collector.fail(unit)
                if session is not None:
                    finish(session, [], None, "failed", str(error)[:300])
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
                handle(batch, extract.answers(batch, answer), None)
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
    log(f"read {stats['chats']} chats · {stats['candidates']} candidate memories")
    return stats


def _place(client: Client, log) -> int:
    """Judge where each new working preference applies (reach.py); returns how many."""
    with writing(timeout=60):
        notes_store = NoteStore()
        try:
            return reach.run(client, notes_store, notes_store.load())
        except (BudgetExceeded, LLMError) as e:
            log(f"judging where preferences apply stopped early ({e}); the next run continues")
            return 0


def _fold(con, client: Client, log, placed: int = 0, batcher=None) -> Counter:
    with writing(timeout=60):  # a relink in progress finishes first; if one hangs, carry on
        return _fold_notes(con, client, log, placed, batcher)


def _fold_notes(con, client: Client, log, placed: int = 0, batcher=None) -> Counter:
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
        if batcher is not None and client.provider.for_purpose("consolidate").supports_batch:
            stats = _in_rounds(con, client, batcher, notes_store, notes, log)
        else:
            stats = consolidate.run(con, client, notes_store, notes, log=log)
    except (BudgetExceeded, LLMError) as e:
        log(f"consolidation stopped early ({e}); the next run continues from here")
    changes = {k: v for k, v in stats.items() if v and k != "batches"}
    if placed:
        changes["placed"] = placed
    if refiled := routing.refile(con, notes_store, notes):
        changes["refiled"] = refiled
    relinked = links.relink(notes_store, notes)
    if relinked:
        changes["relinked"] = relinked
    summary = ", ".join(f"{v} {k}" for k, v in changes.items())
    # Always rebuilt: the indexes also follow config.toml ([contexts]), which no note records.
    notes_store.compile(notes)
    sha = notes_store.commit(f"dream {today()}: {summary or 'reindexed'}")
    log(f"memory: {summary or 'no changes'}" + (f" · commit {sha}" if sha else ""))
    notes_store.reconcile(notes)
    context.index_notes(con, notes)
    return Counter(changes)


def _in_rounds(con, client: Client, batcher: Batcher, notes_store: NoteStore, notes: dict, log) -> Counter:
    """Consolidation in rounds: every scope's next request goes out in one batch, and the answers
    are applied in turn. A scope's requests stay in order; a scope whose request fails stops for
    this run, and the others carry on."""
    fold = consolidate.Consolidation(con, notes_store, notes, log)
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
    return fold.finish()


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
