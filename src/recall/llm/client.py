"""Every model call goes through the Client: schema validation, retries with
backoff, a response cache, a call log, and a budget in calls and, for metered
providers, in dollars."""

import hashlib
import json
import sqlite3
import threading
import time

import jsonschema

from .. import paths
from ..chats.redact import redact, redact_json
from .base import CreditsExhausted, FatalError, LLMError, Provider, Result

# For holding a call's worth of the dollar budget before its cost is known: input from the
# prompt's length, output from this model's recent answers for the same purpose.
CHARS_PER_TOKEN = 3
TYPICAL_OUTPUT_TOKENS = 8000  # until there is history (max_tokens is 16000): a cold batch must not overshoot max_usd
PURPOSES = ("extract", "consolidate")


class BudgetExceeded(Exception):
    pass


class Budget:
    """Model calls and dollars a run may spend; shared by every client in the run."""

    def __init__(self, max_calls: int | None = None, max_usd: float | None = None):
        self.max_calls, self.max_usd = max_calls, max_usd
        self.calls, self.usd, self.held = 0, 0.0, 0.0
        self._lock = threading.Lock()

    def take_call(self, estimate_usd: float | None = None) -> None:
        """Count one call, and hold its estimated cost until settle()."""
        with self._lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise BudgetExceeded(f"call budget of {self.max_calls} reached")
            if self.max_usd is not None and estimate_usd is not None:
                if self.usd + self.held + estimate_usd > self.max_usd:
                    raise BudgetExceeded(f"dollar budget of ${self.max_usd:.2f} reached")
                self.held += estimate_usd
            self.calls += 1

    def settle(self, estimate_usd: float | None, actual_usd: float | None) -> None:
        with self._lock:
            if self.max_usd is not None and estimate_usd is not None:
                self.held = max(0.0, self.held - estimate_usd)
            self.usd += actual_usd or 0.0


class Client:
    """Safe to share across worker threads: it owns its own database connection. Calls are
    synchronous; llm/batch.py sends a round of calls through a provider's batch API, with the
    same cache, log and budget."""

    def __init__(
        self,
        provider: Provider,
        max_calls: int | None = None,
        db_path: str = paths.DB,
        cache: bool = True,
        *,
        budget: Budget | None = None,
        fallback: Provider | None = None,
        log=None,
    ):
        self.provider, self.cache, self.fallback = provider, cache, fallback
        self.budget = budget or Budget(max_calls)
        self.max_calls = self.budget.max_calls
        self.calls = self.tokens_in = self.tokens_out = 0
        self.usd = 0.0  # what this client's calls cost, where the price is known
        self.unpriced = 0  # metered calls whose price recall does not know
        self.log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._typical: dict[tuple[str, str], int] = {}
        self._db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._db.executescript(
            """
            create table if not exists llm_cache (key text primary key, response text, created real);
            create table if not exists llm_calls (
              id integer primary key, ts real, backend text, purpose text, ok int, cached int,
              tokens_in int, tokens_out int, seconds real, error text
            );
            """
        )
        columns = {r[1] for r in self._db.execute("pragma table_info(llm_calls)")}
        for column, kind in (("usd", "real"), ("batch", "int")):
            if column not in columns:
                self._write(f"alter table llm_calls add column {column} {kind}", ())

    def key(self, provider: Provider, system: str, prompt: str, schema: dict) -> str:
        return hashlib.sha256(json.dumps([provider.label, system, prompt, schema]).encode()).hexdigest()

    def cached(self, key: str) -> dict | None:
        if not self.cache:
            return None
        with self._lock:
            row = self._db.execute("select response from llm_cache where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def json(self, purpose: str, system: str, prompt: str, schema: dict, retries: int = 2) -> dict:
        provider = self.provider.for_purpose(purpose)
        data = self.cached(self.key(provider, system, prompt, schema))
        if data is not None:
            self.log_call(purpose, provider, Result({}), cached=True)
            return data
        return self.attempts(purpose, system, prompt, schema, retries)

    def attempts(
        self,
        purpose: str,
        system: str,
        prompt: str,
        schema: dict,
        retries: int = 2,
        feedback: str = "",
        attempt: int = 0,
    ) -> dict:
        """Synchronous calls until one answers to the schema. A batched call whose answer was
        rejected carries on here from its second attempt, with the rejection as feedback."""
        while attempt <= retries:
            provider = self.provider.for_purpose(purpose)  # the fallback once credits run out
            key = self.key(provider, system, prompt, schema)
            estimate = self.estimate(provider, purpose, system, prompt + feedback)
            self.budget.take_call(estimate)
            with self._lock:
                self.calls += 1
            result = None
            try:
                result = provider.complete(system, prompt + feedback, schema)
                jsonschema.validate(result.data, schema)
            except CreditsExhausted as e:
                self.budget.settle(estimate, None)
                self.log_call(purpose, provider, Result({}), error=str(e)[:300])
                self.out_of_credits(provider, e)
                continue  # the same attempt again, on the fallback provider
            except FatalError as e:
                self.budget.settle(estimate, None)
                self.log_call(purpose, provider, Result({}), error=str(e))
                raise
            except (LLMError, jsonschema.ValidationError) as e:
                feedback = self.rejected(purpose, provider, estimate, result, e)
                time.sleep(min(30, 2**attempt))
                attempt += 1
                continue
            self.accepted(purpose, provider, key, estimate, result)
            return result.data
        raise LLMError(f"{purpose}: gave up after {retries + 1} attempts")

    def accepted(self, purpose: str, provider: Provider, key: str, estimate: float | None, result: Result) -> None:
        """Account for an answer that passed the schema, and cache it."""
        usd = self.charge(provider, result)
        self.budget.settle(estimate, usd)
        self.store(key, result.data)
        self.log_call(purpose, provider, result, usd=usd)

    def rejected(self, purpose: str, provider: Provider, estimate, result: Result | None, e: Exception) -> str:
        """Account for a failed or rejected answer (still billed); returns the feedback for a retry."""
        error = str(e).split("\n")[0][:300]
        usd = self.charge(provider, result)
        self.budget.settle(estimate, usd)
        self.log_call(purpose, provider, result or Result({}), error=error, usd=usd)
        return f"\n\nYour previous answer was rejected: {error}\nReturn only JSON matching the schema."

    def store(self, key: str, data: dict) -> None:
        with self._lock:
            self._write(
                "insert or replace into llm_cache values (?,?,?)", (key, json.dumps(redact_json(data)), time.time())
            )

    def out_of_credits(self, failed: Provider, e: CreditsExhausted) -> None:
        """Switch to the fallback provider for the rest of the run, or stop the run cleanly."""
        with self._lock:
            if self.fallback is not None:
                if self.provider is not self.fallback:
                    self.log(f"the API key's credits ran out; carrying on with {self.fallback.label}")
                    self.provider = self.fallback
                    return
                if failed not in (self.fallback, *(self.fallback.for_purpose(p) for p in PURPOSES)):
                    return  # another call switched already: retry there
        raise BudgetExceeded(f"no API credits left ({str(e)[:200]}); the next run carries on") from e

    def estimate(self, provider: Provider, purpose: str, system: str, prompt: str, batch: bool = False) -> float | None:
        """A call's likely cost, held against the dollar budget until its real cost is known."""
        if not provider.priced:
            return None
        tokens_in = (len(system) + len(prompt)) // CHARS_PER_TOKEN
        return provider.cost(Result({}, tokens_in, self._typical_output(provider, purpose), batch=batch))

    def _typical_output(self, provider: Provider, purpose: str) -> int:
        """Mean output tokens of this model's last 100 answers for this purpose."""
        k = (provider.label, purpose.split("-")[0])
        if k not in self._typical:
            with self._lock:
                row = self._db.execute(
                    "select avg(tokens_out), count(*) from (select tokens_out from llm_calls where backend=? and"
                    " purpose like ? and ok=1 and cached=0 and tokens_out>0 order by id desc limit 100)",
                    (k[0], k[1] + "%"),
                ).fetchone()
            self._typical[k] = int(row[0]) if row and row[1] >= 5 else TYPICAL_OUTPUT_TOKENS
        return self._typical[k]

    def charge(self, provider: Provider, result: Result | None) -> float | None:
        if result is None:
            return None
        usd = provider.cost(result)
        with self._lock:
            self.tokens_in += result.input_tokens
            self.tokens_out += result.output_tokens
            if usd is not None:
                self.usd += usd
            elif provider.metered:
                self.unpriced += 1
        return usd

    def log_call(
        self,
        purpose: str,
        provider: Provider,
        result: Result,
        cached: bool = False,
        error: str | None = None,
        usd: float | None = None,
    ) -> None:
        with self._lock:
            self._write(
                "insert into llm_calls(ts, backend, purpose, ok, cached, tokens_in, tokens_out, seconds, error,"
                " usd, batch) values (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    provider.label,
                    purpose,
                    int(error is None),
                    int(cached),
                    result.input_tokens,
                    result.output_tokens,
                    result.seconds,
                    redact(error) if error else error,
                    usd,
                    int(result.batch),
                ),
            )

    def _write(self, sql: str, args: tuple) -> bool:
        """Cache and log writes are best-effort: a busy database must not fail a model call."""
        try:
            self._db.execute(sql, args)
            self._db.commit()
            return True
        except sqlite3.OperationalError:
            return False
