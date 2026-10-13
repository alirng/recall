"""Batch APIs for the nightly run: the same calls at half the price, answered within minutes
to hours instead of seconds.

`Batcher.ask(calls)` takes one round of independent calls (every chat batch to extract, or
every scope's next consolidation call) and returns their answers in order. Calls the response
cache already holds cost nothing; the rest go out as one batch per provider, answers that fail
the schema are asked again synchronously with the rejection as feedback, and every call lands
in llm_cache and llm_calls with its batch price, as a synchronous call would.

- Narrow rounds: a round with fewer than `min_batch` calls to make is asked synchronously; a
  batch of one or two calls saves a fraction of a cent and costs minutes of waiting.
- Time: a round goes synchronously once the time left is less than twice the batches' typical
  wait, so the end of the time budget goes on answers rather than on waiting.
- Patience: a batch still running after PATIENCE is cancelled; what it answered is kept, the
  rest is asked synchronously, and the run stays synchronous from then on.
- Deadline: when the run's time budget ends while a batch runs, the batch is left running and
  its calls fail with BudgetExceeded. Its id stays in the llm_batches table, and the next run
  collects its answers into the response cache before it starts (resume), so the calls it
  repeats cost nothing.
- Credits: when the API account is out of credits, the client switches to its fallback
  provider (synchronous) or the round's calls fail with BudgetExceeded and the run stops.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import jsonschema

from .base import CreditsExhausted, FatalError, LLMError, Provider, Request, Result
from .client import PURPOSES, BudgetExceeded, Client

PATIENCE = 20 * 60  # seconds a batch may run before the run falls back to synchronous calls
POLL_FIRST, POLL_MAX = 5.0, 30.0  # seconds between status checks, growing
PROGRESS_EVERY = 120  # seconds between progress lines in the log
CANCEL_WAIT = 120  # seconds to wait for a cancelled batch to settle
TYPICAL_WAIT = 5 * 60  # seconds a batch takes, until this run has timed one


@dataclass(frozen=True)
class Call:
    """One model call. Batcher.ask takes any object with these four fields."""

    purpose: str
    system: str
    prompt: str
    schema: dict


@dataclass
class _Sent:
    """A call in a submitted batch."""

    index: int  # position in the round
    call: Call
    provider: Provider
    key: str
    estimate: float | None


@dataclass
class _Open:
    """A submitted batch: request id -> its call."""

    provider: Provider
    batch_id: str
    sent: dict[str, _Sent]
    submitted: float
    schemas: dict[str, dict] = field(default_factory=dict)  # for batches resumed from an earlier run
    keys: dict[str, str] = field(default_factory=dict)


class Batcher:
    def __init__(self, client: Client, log=print, deadline: float | None = None, patience: float = PATIENCE):
        self.client, self.log, self.deadline, self.patience = client, log, deadline, patience
        self.sync = False  # once a batch overruns its patience, the rest of the run is synchronous
        self.batches = self.requests = 0
        self.waits: list[float] = []  # how long this run's batches took, in seconds
        client._write(
            "create table if not exists llm_batches (id text primary key, backend text, submitted real,"
            " status text, requests text, ended real)",
            (),
        )

    def ask(self, calls: list, min_batch: int = 1) -> list[dict | BaseException]:
        """One round: every call's answer (a dict) or the exception that stopped it, in order."""
        out: list = [None] * len(calls)
        direct: list[tuple[int, str, int]] = []  # (index, feedback, first attempt) to ask synchronously
        batches: dict[int, list[_Sent]] = {}
        client = self.client
        todo = []
        for i, call in enumerate(calls):
            provider = client.provider.for_purpose(call.purpose)
            key = client.key(provider, call.system, call.prompt, call.schema)
            data = client.cached(key)
            if data is not None:
                client.log_call(call.purpose, provider, Result({}), cached=True)
                out[i] = data
            else:
                todo.append((i, call, provider, key))
        wait = sum(self.waits) / len(self.waits) if self.waits else TYPICAL_WAIT
        late = self.deadline is not None and self.deadline - time.time() < 2 * wait
        narrow = sum(p.supports_batch for _, _, p, _ in todo) < min_batch
        for i, call, provider, key in todo:
            if self.sync or late or narrow or not provider.supports_batch:
                direct.append((i, "", 0))
            else:
                estimate = client.estimate(provider, call.purpose, call.system, call.prompt, batch=True)
                try:
                    client.budget.take_call(estimate)
                except BudgetExceeded as e:
                    out[i] = e
                    continue
                with client._lock:
                    client.calls += 1
                batches.setdefault(id(provider), []).append(_Sent(i, call, provider, key, estimate))
        opened = []
        for sent in batches.values():
            o = self._submit(sent, out, direct)
            if o:
                opened.append(o)
        self._wait(opened, out, direct)
        self._directly(calls, direct, out)
        return out

    # --- sending and waiting ------------------------------------------------------------------

    def _submit(self, sent: list[_Sent], out: list, direct: list) -> "_Open | None":
        provider = sent[0].provider
        requests = [Request(f"r{n}", s.call.system, s.call.prompt, s.call.schema) for n, s in enumerate(sent)]
        try:
            batch_id = provider.submit(requests)
        except CreditsExhausted as e:
            self._unsent(sent)
            try:
                self.client.out_of_credits(provider, e)
                direct += [(s.index, "", 0) for s in sent]  # on the fallback provider
            except BudgetExceeded as stop:
                for s in sent:
                    out[s.index] = stop
            return None
        except FatalError as e:
            self._unsent(sent)
            for s in sent:
                out[s.index] = e
            return None
        except LLMError as e:
            self.log(f"could not send a batch to {provider.label} ({str(e)[:120]}); asking directly")
            self._unsent(sent)
            direct += [(s.index, "", 0) for s in sent]
            return None
        self.batches += 1
        self.requests += len(requests)
        self.log(f"sent {len(requests)} calls to {provider.label} as batch {batch_id} (half price)")
        o = _Open(provider, batch_id, {r.id: s for r, s in zip(requests, sent, strict=True)}, time.time())
        self._record(o)
        return o

    def _unsent(self, sent: list[_Sent]) -> None:
        """Give back the budget held for calls that never went out."""
        for s in sent:
            self.client.budget.settle(s.estimate, None)
            with self.client._lock:
                self.client.calls -= 1

    def _wait(self, opened: list[_Open], out: list, direct: list) -> None:
        interval, last_note = POLL_FIRST, time.time()
        while opened:
            time.sleep(interval)
            interval = min(POLL_MAX, interval * 1.5)
            now = time.time()
            note = now - last_note > PROGRESS_EVERY
            for o in list(opened):
                try:
                    ended, progress = o.provider.poll(o.batch_id)
                except FatalError as e:
                    self._mark(o.batch_id, "lost")
                    for s in o.sent.values():
                        self.client.budget.settle(s.estimate, None)
                        out[s.index] = e
                    opened.remove(o)
                    continue
                except LLMError as e:
                    ended, progress = False, f"status unknown ({str(e)[:80]})"
                if ended:
                    self._collect(o, out, direct)
                    opened.remove(o)
                elif self.deadline and now > self.deadline:
                    self.log(f"time budget reached; batch {o.batch_id} keeps running and the next run collects it")
                    for s in o.sent.values():
                        self.client.budget.settle(s.estimate, None)
                        out[s.index] = BudgetExceeded("time budget reached while a batch was running")
                    opened.remove(o)
                elif now - o.submitted > self.patience:
                    minutes = round((now - o.submitted) / 60)
                    self.log(f"batch {o.batch_id} still running after {minutes} min; asking the rest directly")
                    self.sync = True
                    self._cancel(o, out, direct)
                    opened.remove(o)
                elif note:
                    self.log(f"  batch {o.batch_id}: {progress}, {round((now - o.submitted) / 60, 1)} min")
            if note:
                last_note = now

    def _collect(self, o: _Open, out: list, direct: list) -> None:
        """An ended batch: accept each valid answer; ask again what failed or was never answered."""
        try:
            answers = o.provider.results(o.batch_id)
        except LLMError as e:
            answers = {}
            self.log(f"could not read batch {o.batch_id} ({str(e)[:120]}); asking directly")
        self.waits.append(time.time() - o.submitted)
        minutes = round(self.waits[-1] / 60, 1)
        self.log(f"batch {o.batch_id}: {len(answers)} of {len(o.sent)} answered in {minutes} min")
        self._mark(o.batch_id, "collected")
        client = self.client
        for rid, s in o.sent.items():
            answer = answers.get(rid)
            if answer is None:  # cancelled or expired: never answered, never billed
                client.budget.settle(s.estimate, None)
                direct.append((s.index, "", 0))
                continue
            if isinstance(answer, CreditsExhausted):
                client.budget.settle(s.estimate, None)
                try:
                    client.out_of_credits(s.provider, answer)
                    direct.append((s.index, "", 0))
                except BudgetExceeded as stop:
                    out[s.index] = stop
                continue
            try:
                if isinstance(answer, BaseException):
                    raise answer
                jsonschema.validate(answer.data, s.call.schema)
            except (LLMError, jsonschema.ValidationError) as e:
                result = answer if isinstance(answer, Result) else None
                direct.append((s.index, client.rejected(s.call.purpose, s.provider, s.estimate, result, e), 1))
                continue
            client.accepted(s.call.purpose, s.provider, s.key, s.estimate, answer)
            out[s.index] = answer.data

    def _cancel(self, o: _Open, out: list, direct: list) -> None:
        try:
            o.provider.cancel(o.batch_id)
            until = time.time() + CANCEL_WAIT
            while time.time() < until and not o.provider.poll(o.batch_id)[0]:
                time.sleep(POLL_FIRST)
        except LLMError as e:
            self.log(f"could not cancel batch {o.batch_id}: {str(e)[:120]}")
        self._collect(o, out, direct)
        self._mark(o.batch_id, "cancelled")

    def _directly(self, calls: list, direct: list, out: list) -> None:
        """Synchronous calls, a few at a time, unless the time budget is spent."""
        if not direct:
            return
        if self.deadline and time.time() > self.deadline:
            for i, _, _ in direct:
                out[i] = BudgetExceeded("time budget reached")
            return

        def ask(item) -> None:
            i, feedback, attempt = item
            c = calls[i]
            try:
                out[i] = self.client.attempts(
                    c.purpose, c.system, c.prompt, c.schema, feedback=feedback, attempt=attempt
                )
            except Exception as e:  # handed back to the caller, which decides
                out[i] = e

        workers = max(1, min(len(direct), self.client.provider.profile.workers))
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(ask, direct))

    # --- batches an earlier run left running --------------------------------------------------

    def resume(self) -> int:
        """Collect the answers of batches an earlier run left running into the response cache,
        so this run's identical calls are free. Waits for them within the patience and the
        deadline; cancels the late ones. Returns how many answers were kept."""
        providers = {self.client.provider.for_purpose(p).label: self.client.provider.for_purpose(p) for p in PURPOSES}
        with self.client._lock:
            rows = self.client._db.execute(
                "select id, backend, submitted, requests from llm_batches where status='running'"
            ).fetchall()
        opened = []
        for batch_id, backend, submitted, requests in rows:
            provider = providers.get(backend)
            if provider is None or not provider.supports_batch:
                continue  # another model's batch: collected when that model is configured again
            saved = json.loads(requests)
            o = _Open(provider, batch_id, {}, submitted)
            o.keys = {rid: v[0] for rid, v in saved["requests"].items()}
            o.schemas = {rid: saved["schemas"][v[1]] for rid, v in saved["requests"].items()}
            opened.append(o)
        if not opened:
            return 0
        self.log(f"collecting {len(opened)} batch(es) an earlier run left running")
        start, kept, interval = time.time(), 0, POLL_FIRST
        while opened and time.time() - start < self.patience + CANCEL_WAIT:
            for o in list(opened):
                try:
                    ended, _ = o.provider.poll(o.batch_id)
                except FatalError as e:
                    self.log(f"  batch {o.batch_id}: {str(e)[:120]}")
                    self._mark(o.batch_id, "lost")
                    opened.remove(o)
                    continue
                except LLMError:
                    continue
                if not ended and time.time() - start > self.patience:
                    try:
                        o.provider.cancel(o.batch_id)
                    except LLMError:
                        continue
                    ended = True
                if ended:
                    kept += self._cache_answers(o)
                    opened.remove(o)
            if not opened or (self.deadline and time.time() > self.deadline):
                break
            time.sleep(interval)
            interval = min(POLL_MAX, interval * 1.5)
        return kept

    def _cache_answers(self, o: _Open) -> int:
        try:
            answers = o.provider.results(o.batch_id)
        except LLMError as e:
            self.log(f"  could not read batch {o.batch_id}: {str(e)[:120]}")
            return 0
        n = 0
        for rid, answer in answers.items():
            if not isinstance(answer, Result) or rid not in o.keys:
                continue
            usd = self.client.charge(o.provider, answer)  # billed whether or not this run needs it
            self.client.budget.settle(None, usd)
            try:
                jsonschema.validate(answer.data, o.schemas[rid])
            except jsonschema.ValidationError:
                continue
            self.client.store(o.keys[rid], answer.data)
            self.client.log_call("batch-collect", o.provider, answer, usd=usd)
            n += 1
        self._mark(o.batch_id, "collected")
        self.log(f"  batch {o.batch_id}: {n} answers kept for this run")
        return n

    # --- the llm_batches table ----------------------------------------------------------------

    def _record(self, o: _Open) -> None:
        schemas: dict[str, str] = {}
        requests = {}
        for rid, s in o.sent.items():
            text = json.dumps(s.call.schema, sort_keys=True)
            requests[rid] = [s.key, schemas.setdefault(text, f"s{len(schemas)}")]
        saved = {"requests": requests, "schemas": {sid: json.loads(text) for text, sid in schemas.items()}}
        with self.client._lock:
            self.client._write(
                "insert or replace into llm_batches values (?,?,?,?,?,?)",
                (o.batch_id, o.provider.label, o.submitted, "running", json.dumps(saved), None),
            )

    def _mark(self, batch_id: str, status: str) -> None:
        with self.client._lock:
            self.client._write("update llm_batches set status=?, ended=? where id=?", (status, time.time(), batch_id))
