"""Any server that speaks the OpenAI chat-completions API.

That covers OpenAI and OpenRouter with an API key, and LM Studio, llama.cpp, vLLM
or any endpoint you host yourself, with or without one. With an OpenAI key the nightly run
uses OpenAI's Batch API, at half the price.
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

from .base import (
    CREDIT_ERRORS,
    CreditsExhausted,
    FatalError,
    LLMError,
    Profile,
    Provider,
    Request,
    Result,
    SendUnconfirmed,
    http_json,
    parse_json,
)

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
        price: list | None = None,
    ):
        super().__init__(model)
        self.price_override = price
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.id = provider_id or self.id
        self.private = private
        self.destination = "this machine" if private else urlparse(self.base_url).hostname or self.base_url
        if private:
            self.profile = LOCAL_PROFILE
        self._strict_schema = True  # switched off for servers without json_schema support
        # OpenAI's own Batch API; OpenRouter and self-hosted servers have none.
        self.supports_batch = self.id == "openai" and bool(api_key)

    @property
    def metered(self) -> bool:
        return not self.private and bool(self.api_key)

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
            if not self._strict_schema or "response_format" not in str(e) or isinstance(e, CreditsExhausted):
                raise
            self._strict_schema = False  # this server cannot constrain output to a schema
            out = http_json(url, self._body(system, prompt, schema), self._headers())
        return _result(out, time.time() - start)

    # --- OpenAI Batch API: a JSONL file of chat-completion requests ---------------------------

    def submit(self, requests: list[Request]) -> str:
        lines = [
            {
                "custom_id": r.id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": self._body(r.system, r.prompt, r.schema),
            }
            for r in requests
        ]
        upload = self._upload("\n".join(json.dumps(line) for line in lines).encode())
        try:
            batch = http_json(
                f"{self.base_url}/batches",
                {"input_file_id": upload["id"], "endpoint": "/v1/chat/completions", "completion_window": "24h"},
                self._headers(),
            )
        except FatalError:
            raise
        except LLMError as e:
            if str(e).startswith("HTTP 429"):
                raise  # rejected, not created
            # may have been created: the Batcher records it as unconfirmed
            raise SendUnconfirmed(str(e)) from e
        return batch["id"]

    def poll(self, batch_id: str) -> tuple[bool, str]:
        batch = http_json(f"{self.base_url}/batches/{batch_id}", headers=self._headers())
        counts = batch.get("request_counts") or {}
        done = counts.get("completed", 0) + counts.get("failed", 0)
        ended = batch["status"] in ("completed", "failed", "expired", "cancelled")
        return ended, f"{done}/{counts.get('total', '?')} answered ({batch['status']})"

    def results(self, batch_id: str) -> dict[str, Result | LLMError]:
        batch = http_json(f"{self.base_url}/batches/{batch_id}", headers=self._headers())
        if batch["status"] == "failed":
            errors = (batch.get("errors") or {}).get("data") or [{}]
            message = str(errors[0].get("message", "the batch failed validation"))
            kind = CreditsExhausted if any(m in message for m in CREDIT_ERRORS) else LLMError
            raise kind(f"OpenAI batch failed: {message}")
        seconds = float((batch.get("completed_at") or batch.get("created_at") or 0) - (batch.get("created_at") or 0))
        out = {}
        for file_key in ("output_file_id", "error_file_id"):
            if not batch.get(file_key):
                continue
            for line in self._download(batch[file_key]).splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                response = item.get("response") or {}
                if item.get("error") or response.get("status_code") != 200:
                    error = item.get("error") or response.get("body", {}).get("error") or {}
                    if error.get("code") == "batch_expired":
                        continue  # never answered (nor billed)
                    message = f"{error.get('code')}: {error.get('message')}"
                    kind = CreditsExhausted if any(m in message for m in CREDIT_ERRORS) else LLMError
                    out[item["custom_id"]] = kind(f"OpenAI batch request failed: {message}")
                    continue
                try:
                    out[item["custom_id"]] = _result(response["body"], seconds, batch=True)
                except (LLMError, KeyError, IndexError) as e:
                    out[item["custom_id"]] = LLMError(f"OpenAI batch answer unreadable: {e}")
        return out

    def cancel(self, batch_id: str) -> None:
        http_json(f"{self.base_url}/batches/{batch_id}/cancel", {}, self._headers())

    def _upload(self, content: bytes) -> dict:
        """POST /files as multipart form data, purpose=batch."""
        boundary = uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recall.jsonl"\r\n'
                "Content-Type: application/jsonl\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        headers = {**self._headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        return json.loads(self._send(urllib.request.Request(f"{self.base_url}/files", data=body, headers=headers)))

    def _download(self, file_id: str) -> str:
        url = f"{self.base_url}/files/{file_id}/content"
        return self._send(urllib.request.Request(url, headers=self._headers())).decode()

    def _send(self, req: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if any(m in body for m in CREDIT_ERRORS):
                raise CreditsExhausted(f"HTTP {e.code} from {req.full_url}: {body}") from e
            raise (FatalError if 400 <= e.code < 500 and e.code not in (408, 429) else LLMError)(
                f"HTTP {e.code} from {req.full_url}: {body}"
            ) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"cannot reach {req.full_url}: {e}") from e


def _result(out: dict, seconds: float, batch: bool = False) -> Result:
    usage = out.get("usage") or {}
    return Result(
        parse_json(out["choices"][0]["message"]["content"] or ""),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        seconds,
        batch,
    )
