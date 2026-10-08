"""The contract every model provider implements."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(Exception):
    """A call failed in a way that may succeed on retry: timeout, rate limit, bad output."""


class FatalError(LLMError):
    """A call cannot succeed as configured: bad key, unknown model, missing CLI."""


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


def http_json(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 600) -> dict:
    """GET (no payload) or POST JSON. Raises FatalError for 4xx other than 408/429."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if 400 <= e.code < 500 and e.code not in (408, 429):
            raise FatalError(f"HTTP {e.code} from {url}: {body}") from e
        raise LLMError(f"HTTP {e.code} from {url}: {body}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError(f"cannot reach {url}: {e}") from e
