"""Claude through the Anthropic API with the user's own key (`pip install 'recall[anthropic]'`).

The nightly run sends its calls through the Message Batches API, at half the price, unless
config.toml says `batch = "off"` (see memory/dream.py). Claude Max and Team plans include
monthly API credits that pay for it.
"""

import json
import time
from contextlib import contextmanager

from .base import CREDIT_ERRORS, CreditsExhausted, FatalError, LLMError, Provider, Request, Result

MAX_TOKENS = 16000  # thinking counts toward it


class AnthropicAPI(Provider):
    id = "anthropic"
    title = "Anthropic API"
    destination = "Anthropic's API, billed to your key"

    def __init__(self, model: str, api_key: str | None, effort: str | None = None, price: list | None = None):
        super().__init__(model)
        self.api_key = api_key
        self.effort = effort  # None: the model's default
        self.price_override = price
        self.supports_batch = bool(api_key)
        self._client = None

    @property
    def label(self) -> str:
        return f"{self.id}:{self.model}" + (f"@{self.effort}" if self.effort else "")

    @property
    def metered(self) -> bool:
        return True

    def _sdk(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise FatalError("the Anthropic SDK is not installed: pip install 'recall[anthropic]'") from e
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def check(self) -> str | None:
        if not self.api_key:
            return "no Anthropic API key configured"
        try:
            self._sdk()
        except FatalError as e:
            return str(e)
        return None

    def models(self) -> list[str]:
        return sorted(m.id for m in self._sdk().models.list())

    def _params(self, system: str, prompt: str, schema: dict) -> dict:
        output = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output,
        }

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        start = time.time()
        with _errors():
            response = self._sdk().messages.create(**self._params(system, prompt, schema))
        return _result(response, time.time() - start)

    # --- Message Batches API ------------------------------------------------------------------

    def submit(self, requests: list[Request]) -> str:
        with _errors():
            batch = self._sdk().messages.batches.create(
                requests=[{"custom_id": r.id, "params": self._params(r.system, r.prompt, r.schema)} for r in requests]
            )
        return batch.id

    def poll(self, batch_id: str) -> tuple[bool, str]:
        with _errors():
            batch = self._sdk().messages.batches.retrieve(batch_id)
        c = batch.request_counts
        finished = c.succeeded + c.errored + c.canceled + c.expired
        return batch.processing_status == "ended", f"{finished}/{finished + c.processing} answered"

    def results(self, batch_id: str) -> dict[str, Result | LLMError]:
        with _errors():
            batch = self._sdk().messages.batches.retrieve(batch_id)
            seconds = (batch.ended_at - batch.created_at).total_seconds() if batch.ended_at else 0.0
            out = {}
            for item in self._sdk().messages.batches.results(batch_id):
                r = item.result
                if r.type == "succeeded":
                    try:
                        out[item.custom_id] = _result(r.message, seconds, batch=True)
                    except LLMError as e:
                        out[item.custom_id] = e
                elif r.type == "errored":
                    error = getattr(r.error, "error", r.error)
                    message = f"{getattr(error, 'type', 'error')}: {getattr(error, 'message', error)}"
                    kind = CreditsExhausted if any(m in message for m in CREDIT_ERRORS) else LLMError
                    out[item.custom_id] = kind(f"Anthropic batch request failed: {message}")
                # canceled and expired requests were never answered (nor billed): left out
        return out

    def cancel(self, batch_id: str) -> None:
        with _errors():
            self._sdk().messages.batches.cancel(batch_id)


@contextmanager
def _errors():
    """The SDK's errors as recall's: retryable, fatal, or out of credits."""
    try:
        yield
    except Exception as e:
        try:
            import anthropic
        except ImportError:
            raise e from None
        if isinstance(e, anthropic.APIStatusError) and (
            e.status_code == 402 or any(m in str(e) for m in CREDIT_ERRORS)
        ):
            raise CreditsExhausted(f"Anthropic API: {e}") from e
        fatal = (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        )
        if isinstance(e, fatal):
            raise FatalError(f"Anthropic API: {e}") from e
        if isinstance(e, (anthropic.APIStatusError, anthropic.APIConnectionError)):
            raise LLMError(f"Anthropic API: {e}") from e
        raise


def _result(message, seconds: float, batch: bool = False) -> Result:
    if message.stop_reason in ("refusal", "max_tokens"):
        raise LLMError(f"Anthropic API stopped early: {message.stop_reason}")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Anthropic API returned invalid JSON: {e}") from e
    return Result(data, message.usage.input_tokens, message.usage.output_tokens, seconds, batch)
