"""The contract every model provider implements."""

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from . import prices


class LLMError(Exception):
    """A call failed in a way that may succeed on retry: timeout, rate limit, bad output."""


class SendUnconfirmed(LLMError):
    """A batch's send failed in a way that may still have reached the server (timeout, dropped
    connection, 5xx): it may exist and bill. A rejected send (4xx, no credits) is not this."""


class FatalError(LLMError):
    """A call cannot succeed as configured: bad key, unknown model, missing CLI."""


class CreditsExhausted(FatalError):
    """The API account has no credits left. Not a fault: the run falls back to the
    configured subscription CLI, or stops cleanly and carries on next night."""


# The low-balance replies of the metered APIs, as their docs give them.
CREDIT_ERRORS = ("credit balance is too low", "insufficient_quota", "billing_error")


@dataclass(frozen=True)
class Profile:
    """How a provider wants work packed into calls.

    Agent CLIs carry a large fixed prompt on every call, so they take big batches.
    Local models have small context windows and share one GPU, so they take small
    batches and a single worker.
    """

    batch_chars: int = 60_000
    batch_chats: int = 8
    workers: int = 4


@dataclass
class Result:
    data: dict
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    batch: bool = False  # answered through a batch API, at the batch price


@dataclass(frozen=True)
class Request:
    """One call in a batch. id is unique within the batch (letters, digits, - and _)."""

    id: str
    system: str
    prompt: str
    schema: dict


class Provider:
    id = "base"
    title = "base"
    destination = "this machine"  # where prompts are sent, in words people understand
    private = False  # True when prompts never leave this machine
    profile = Profile()

    def __init__(self, model: str):
        self.model = model

    @property
    def label(self) -> str:
        """Names the exact model and settings; used as part of the response cache key."""
        return f"{self.id}:{self.model}"

    def check(self) -> str | None:
        """None when ready to use; otherwise a sentence saying what to fix."""
        return None

    def models(self) -> list[str]:
        """Model names this provider offers, for people choosing one."""
        return []

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        """One call: return JSON matching `schema`. Validation happens in the client."""
        raise NotImplementedError

    def for_purpose(self, purpose: str) -> "Provider":
        """The provider that serves calls made for `purpose` ("extract", "consolidate", ...)."""
        return self

    # --- cost: metered providers only -----------------------------------------------------

    price_override: list | None = None  # [input, output] USD per million tokens, from config

    def cost(self, result: Result) -> float | None:
        """USD for one answered call; None for plans, local models and unpriced models."""
        if not self.metered:
            return None
        return prices.cost(self.model, result.input_tokens, result.output_tokens, result.batch, self.price_override)

    @property
    def metered(self) -> bool:
        """Billed per token to an API key (as opposed to a plan or this machine)."""
        return False

    @property
    def priced(self) -> bool:
        return self.metered and prices.lookup(self.model, self.price_override) is not None

    # --- batch API: providers that have one override these ----------------------------------

    supports_batch = False  # True when this provider, as configured, can send batches

    def submit(self, requests: list[Request]) -> str:
        """Send requests as one batch; returns the batch id."""
        raise NotImplementedError

    def poll(self, batch_id: str) -> tuple[bool, str]:
        """(ended, progress in words) for a batch."""
        raise NotImplementedError

    def results(self, batch_id: str) -> dict[str, "Result | LLMError"]:
        """An ended batch's answers by request id; a request missing here was never answered."""
        raise NotImplementedError

    def cancel(self, batch_id: str) -> None:
        raise NotImplementedError

    def find_batch(self, count: int, since: float) -> str | None:
        """After a send that may have gone through: the id of the one batch of `count` requests
        created since `since` (epoch seconds), or None if there is none. Raise NotImplementedError
        or LLMError when this cannot be told."""
        raise NotImplementedError


class Split(Provider):
    """One model reads chats and another folds memories into notes: a cheap model for the
    many extraction calls, a stronger one for consolidation's judgement calls."""

    def __init__(self, extract: Provider, consolidate: Provider):
        super().__init__(extract.model)
        self.extract, self.consolidate = extract, consolidate
        self.id, self.title, self.profile = extract.id, extract.title, extract.profile
        self.private = extract.private and consolidate.private
        self.destination = extract.destination
        if consolidate.destination != extract.destination:
            self.destination += f" and {consolidate.destination}"

    @property
    def label(self) -> str:
        return f"{self.extract.label}+{self.consolidate.label}"

    def for_purpose(self, purpose: str) -> Provider:
        return self.consolidate if purpose.startswith("consolidate") else self.extract

    def check(self) -> str | None:
        return self.extract.check() or self.consolidate.check()

    def models(self) -> list[str]:
        return self.extract.models()

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        return self.extract.complete(system, prompt, schema)


def parse_json(text: str) -> dict:
    """The JSON object in a model reply, tolerating code fences and stray prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError(f"no JSON object in reply: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"reply is not valid JSON: {e}") from e


def on_this_machine(url: str) -> bool:
    """Whether a server at this URL runs on this machine: localhost, a loopback address
    (127.0.0.1, ::1) or a unix socket. Any other host is another machine, whatever the kind of
    server."""
    parts = urlparse(url)
    if parts.scheme in ("unix", "http+unix", "https+unix"):
        return True
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_json(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 600) -> dict:
    """GET (no payload) or POST JSON. Raises FatalError for 4xx other than 408/429."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if any(m in body for m in CREDIT_ERRORS):
            raise CreditsExhausted(f"HTTP {e.code} from {url}: {body}") from e
        if 400 <= e.code < 500 and e.code not in (408, 429):
            raise FatalError(f"HTTP {e.code} from {url}: {body}") from e
        raise LLMError(f"HTTP {e.code} from {url}: {body}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError(f"cannot reach {url}: {e}") from e
