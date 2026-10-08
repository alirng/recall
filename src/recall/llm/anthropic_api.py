"""Claude through the Anthropic API with the user's own key (`pip install 'recall[anthropic]'`)."""

import json
import time

from .base import FatalError, LLMError, Provider, Result


class AnthropicAPI(Provider):
    id = "anthropic"
    title = "Anthropic API"
    destination = "Anthropic's API, billed to your key"

    def __init__(self, model: str, api_key: str | None):
        super().__init__(model)
        self.api_key = api_key
        self._client = None

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

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        import anthropic

        start = time.time()
        try:
            response = self._sdk().messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        ) as e:
            raise FatalError(f"Anthropic API: {e}") from e
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(f"Anthropic API: {e}") from e
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"Anthropic API stopped early: {response.stop_reason}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Anthropic API returned invalid JSON: {e}") from e
        return Result(data, response.usage.input_tokens, response.usage.output_tokens, time.time() - start)
