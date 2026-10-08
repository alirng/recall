"""Any server that speaks the OpenAI chat-completions API.

That covers OpenAI and OpenRouter with an API key, and LM Studio, llama.cpp, vLLM
or any endpoint you host yourself, with or without one.
"""

import json
import time
from urllib.parse import urlparse

from .base import FatalError, Profile, Provider, Result, http_json, parse_json

LOCAL_PROFILE = Profile(batch_chars=24_000, batch_chats=3, workers=1)


class OpenAICompatible(Provider):
    id = "openai-compatible"
    title = "OpenAI-compatible endpoint"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        *,
        provider_id: str | None = None,
        private: bool = False,
    ):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.id = provider_id or self.id
        self.private = private
        self.destination = "this machine" if private else urlparse(self.base_url).hostname or self.base_url
        if private:
            self.profile = LOCAL_PROFILE
        self._strict_schema = True  # switched off for servers without json_schema support

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def models(self) -> list[str]:
        data = http_json(f"{self.base_url}/models", headers=self._headers(), timeout=10)
        return sorted(m["id"] for m in data.get("data", []))

    def check(self) -> str | None:
        try:
            available = self.models()
        except Exception as e:
            return f"cannot list models at {self.base_url}: {e}"
        if self.model and available and self.model not in available:
            return f"{self.model!r} is not offered by {self.base_url}"
        return None

    def _body(self, system: str, prompt: str, schema: dict) -> dict:
        if self._strict_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "recall", "strict": True, "schema": schema},
            }
        else:
            response_format = {"type": "json_object"}
            system += f"\n\nReply with JSON matching this schema:\n{json.dumps(schema)}"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": response_format,
        }
        if self.private:
            # Small local models can loop under constrained decoding; bound the reply.
            body.update(temperature=0, max_tokens=4096)
        return body

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        start = time.time()
        url = f"{self.base_url}/chat/completions"
        try:
            out = http_json(url, self._body(system, prompt, schema), self._headers())
        except FatalError as e:
            if not self._strict_schema or "response_format" not in str(e):
                raise
            self._strict_schema = False  # this server cannot constrain output to a schema
            out = http_json(url, self._body(system, prompt, schema), self._headers())
        usage = out.get("usage") or {}
        return Result(
            parse_json(out["choices"][0]["message"]["content"] or ""),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            time.time() - start,
        )
