"""Model providers. The person picks one at setup; recall assumes no vendor.

    local       a model you serve yourself (Ollama, LM Studio, llama.cpp): nothing leaves this machine
                when its base_url is localhost or a loopback address
    chatgpt     your ChatGPT plan, through the Codex CLI
    claude      your Claude plan, through the Claude Code CLI
    openai      OpenAI API key
    anthropic   Anthropic API key
    openrouter  OpenRouter API key
    custom      any OpenAI-compatible endpoint, such as a server you host

Settings come from the [model] table of config.toml:

    provider = "local"
    name = "qwen3:4b"
    server = "ollama"                     # local: "ollama", or "openai" for OpenAI-compatible servers
    base_url = "http://localhost:11434"   # local and custom
    api_key = "keychain"                  # API providers: env:NAME, keychain or file
    effort = "medium"                     # chatgpt and anthropic
    batch = "auto"                        # API providers: "auto" sends the nightly run through
                                          # the provider's batch API (half price), "off" never
    consolidate = "anthropic:claude-sonnet-5-5"  # optional: another model folds memories into notes
    fallback = "claude:haiku"             # optional: used once the API key's credits run out
    price = [0.10, 0.50]                  # optional: USD per million tokens in and out

A one-off `--model` spec names a provider and model, optionally an effort after "@", and
optionally a second spec for consolidation after "+":
"anthropic:claude-haiku-5-5+anthropic:claude-sonnet-5-5@medium".
"""

from .. import keys
from .agent_cli import ChatGPT, Claude
from .anthropic_api import AnthropicAPI
from .base import CreditsExhausted, FatalError, LLMError, Profile, Provider, Split
from .client import Budget, BudgetExceeded, Client
from .ollama import Ollama
from .openai_compat import OpenAICompatible

API_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
LOCAL_SERVERS = {  # common local servers, probed in this order
    "Ollama": "http://localhost:11434",
    "LM Studio": "http://localhost:1234/v1",
    "llama.cpp": "http://localhost:8080/v1",
}

__all__ = [
    "Budget",
    "BudgetExceeded",
    "Client",
    "CreditsExhausted",
    "FatalError",
    "LLMError",
    "Profile",
    "Provider",
    "batching",
    "build",
    "fallback",
    "from_spec",
]

# Settings that describe how a run uses models, not one model: from_spec never carries them over.
RUN_SETTINGS = ("consolidate", "fallback", "batch")


def build(settings: dict) -> Provider:
    """A provider from the [model] settings table (with its consolidation model, if any)."""
    provider = _one(settings)
    if settings.get("consolidate"):
        return Split(provider, from_spec(settings["consolidate"], settings))
    return provider


def _one(settings: dict) -> Provider:
    kind = settings.get("provider")
    name = settings.get("name", "")
    base_url = settings.get("base_url")
    if kind == "local":
        if settings.get("server") == "ollama":
            return Ollama(name, base_url or LOCAL_SERVERS["Ollama"])
        return OpenAICompatible(name, base_url or LOCAL_SERVERS["LM Studio"], provider_id="local", local=True)
    if kind == "chatgpt":
        return ChatGPT(name, settings.get("effort", "medium"))
    if kind == "claude":
        return Claude(name)
    if kind == "anthropic":
        return AnthropicAPI(name, _key(settings, kind), settings.get("effort"), settings.get("price"))
    if kind in API_BASE_URLS or kind == "custom":
        if not (base_url or kind in API_BASE_URLS):
            raise FatalError("the custom provider needs a base_url")
        return OpenAICompatible(
            name,
            base_url or API_BASE_URLS[kind],
            _key(settings, kind),
            provider_id=kind,
            price=settings.get("price"),
        )
    if not kind:
        raise FatalError("no model is configured yet: run `recall setup`")
    raise FatalError(f"unknown provider {kind!r}")


def _key(settings: dict, kind: str) -> str | None:
    if settings.get("api_key"):
        return keys.load(settings["api_key"], kind)
    return keys.find(kind) if kind != "custom" else None


def from_spec(spec: str | None, settings: dict) -> Provider:
    """A provider for a one-off override such as `--model chatgpt:gpt-6-luna`.
    Anything the spec leaves out (keys, URLs) comes from the configured settings."""
    if not spec:
        return build(settings)
    spec, _, consolidate = spec.partition("+")
    kind, _, name = spec.partition(":")
    same = settings.get("provider") == kind
    merged = {k: v for k, v in (settings if same else {}).items() if k not in RUN_SETTINGS}
    merged["provider"] = kind
    if name:
        name, _, effort = name.partition("@") if kind in ("chatgpt", "anthropic") else (name, "", "")
        merged["name"] = name
        if effort:
            merged["effort"] = effort
    provider = _one(merged)
    return Split(provider, from_spec(consolidate, settings)) if consolidate else provider


def fallback(settings: dict) -> Provider | None:
    """The provider a run switches to once the API key's credits run out, if one is set."""
    spec = settings.get("fallback")
    return from_spec(spec, {k: v for k, v in settings.items() if k not in RUN_SETTINGS}) if spec else None


def batching(settings: dict, provider: Provider) -> bool:
    """Should a nightly run send its calls as batches? Only API providers that have a batch
    API, with a key, unless config.toml says batch = "off"."""
    if str(settings.get("batch", "auto")).lower() in ("off", "false", "no"):
        return False
    return any(provider.for_purpose(p).supports_batch for p in ("extract", "consolidate"))
