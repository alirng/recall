"""Ollama through its native API.

Ollama's OpenAI-compatible endpoint cannot raise the context window from its
4k-token default, which silently truncates chat digests; the native API can.
"""

import time
from urllib.parse import urlparse

from .base import Provider, Result, http_json, on_this_machine, parse_json
from .openai_compat import LOCAL_PROFILE

DEFAULT_URL = "http://localhost:11434"


class Ollama(Provider):
    id = "local"
    title = "Ollama"
    profile = LOCAL_PROFILE

    def __init__(self, model: str, base_url: str = DEFAULT_URL):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.private = on_this_machine(self.base_url)
        if not self.private:
            self.destination = urlparse(self.base_url).hostname or self.base_url

    def models(self) -> list[str]:
        return sorted(m["name"] for m in http_json(f"{self.base_url}/api/tags", timeout=5)["models"])

    def check(self) -> str | None:
        try:
            available = self.models()
        except Exception:
            return f"Ollama is not running at {self.base_url} (start it with `ollama serve`)"
        if self.model not in available:
            return f"model {self.model!r} is not pulled (run `ollama pull {self.model}`)"
        return None

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        start = time.time()
        out = http_json(
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "format": schema,
                "stream": False,
                "think": False,
                # A bounded reply: small models can loop under schema-constrained decoding.
                "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096, "repeat_penalty": 1.1},
            },
        )
        return Result(
            parse_json(out["message"]["content"]),
            out.get("prompt_eval_count", 0),
            out.get("eval_count", 0),
            time.time() - start,
        )
