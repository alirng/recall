"""Every model call goes through the Client: schema validation, retries with
backoff, a response cache, a call log, and a call budget."""

import hashlib
import json
import sqlite3
import threading
import time

import jsonschema

from .. import paths
from .base import FatalError, LLMError, Provider, Result


class BudgetExceeded(Exception):
    pass


class Client:
    """Safe to share across worker threads: it owns its own database connection."""

    def __init__(self, provider: Provider, max_calls: int | None = None, db_path: str = paths.DB, cache: bool = True):
        self.provider, self.max_calls, self.cache = provider, max_calls, cache
        self.calls = self.tokens_in = self.tokens_out = 0
        self._lock = threading.Lock()
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

    def json(self, purpose: str, system: str, prompt: str, schema: dict, retries: int = 2) -> dict:
        key = hashlib.sha256(json.dumps([self.provider.label, system, prompt, schema]).encode()).hexdigest()
        with self._lock:
            row = (
                self._db.execute("select response from llm_cache where key=?", (key,)).fetchone()
                if self.cache
                else None
            )
        if row:
            self._log(purpose, Result({}), cached=True)
            return json.loads(row[0])
        feedback = ""
        for attempt in range(retries + 1):
            with self._lock:
                if self.max_calls is not None and self.calls >= self.max_calls:
                    raise BudgetExceeded(f"call budget of {self.max_calls} reached")
                self.calls += 1
            try:
                result = self.provider.complete(system, prompt + feedback, schema)
                jsonschema.validate(result.data, schema)
            except FatalError as e:
                self._log(purpose, Result({}), error=str(e))
                raise
            except (LLMError, jsonschema.ValidationError) as e:
                error = str(e).split("\n")[0][:300]
                self._log(purpose, Result({}), error=error)
                feedback = f"\n\nYour previous answer was rejected: {error}\nReturn only JSON matching the schema."
                time.sleep(min(30, 2**attempt))
                continue
            with self._lock:
                self.tokens_in += result.input_tokens
                self.tokens_out += result.output_tokens
                self._write(
                    "insert or replace into llm_cache values (?,?,?)", (key, json.dumps(result.data), time.time())
                )
            self._log(purpose, result)
            return result.data
        raise LLMError(f"{purpose}: gave up after {retries + 1} attempts")

    def _log(self, purpose: str, result: Result, cached: bool = False, error: str | None = None) -> None:
        with self._lock:
            self._write(
                "insert into llm_calls(ts, backend, purpose, ok, cached, tokens_in, tokens_out, seconds, error)"
                " values (?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    self.provider.label,
                    purpose,
                    int(error is None),
                    int(cached),
                    result.input_tokens,
                    result.output_tokens,
                    result.seconds,
                    error,
                ),
            )

    def _write(self, sql: str, args: tuple) -> None:
        """Cache and log writes are best-effort: a busy database must not fail a model call."""
        try:
            self._db.execute(sql, args)
            self._db.commit()
        except sqlite3.OperationalError:
            pass
