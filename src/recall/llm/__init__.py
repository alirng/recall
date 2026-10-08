"""Model providers. The person picks one at setup; recall assumes no vendor.

    local       a model on this machine: nothing leaves it (Ollama, LM Studio, llama.cpp)
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
    effort = "medium"                     # chatgpt
"""

from .. import keys
from .agent_cli import ChatGPT, Claude
from .anthropic_api import AnthropicAPI
from .base import FatalError, LLMError, Profile, Provider
from .client import BudgetExceeded, Client
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
    "BudgetExceeded",
    "Client",
    "FatalError",
    "LLMError",
    "Profile",
    "Provider",
    "build",
    "from_spec",
]


def build(settings: dict) -> Provider:
    """A provider from the [model] settings table."""
    kind = settings.get("provider")
    name = settings.get("name", "")
    base_url = settings.get("base_url")
    if kind == "local":
        if settings.get("server") == "ollama":
            return Ollama(name, base_url or LOCAL_SERVERS["Ollama"])
        return OpenAICompatible(name, base_url or LOCAL_SERVERS["LM Studio"], provider_id="local", private=True)
    if kind == "chatgpt":
        return ChatGPT(name, settings.get("effort", "medium"))
    if kind == "claude":
        return Claude(name)
    if kind == "anthropic":
        return AnthropicAPI(name, keys.load(settings.get("api_key"), "anthropic"))
    if kind in API_BASE_URLS or kind == "custom":
        if not (base_url or kind in API_BASE_URLS):
            raise FatalError("the custom provider needs a base_url")
        return OpenAICompatible(
            name, base_url or API_BASE_URLS[kind], keys.load(settings.get("api_key"), kind), provider_id=kind
        )
    if not kind:
        raise FatalError("no model is configured yet: run `recall setup`")
    raise FatalError(f"unknown provider {kind!r}")


def from_spec(spec: str | None, settings: dict) -> Provider:
    """A provider for a one-off override such as `--model chatgpt:gpt-6-luna`.
    Anything the spec leaves out (keys, URLs) comes from the configured settings."""
    if not spec:
        return build(settings)
    kind, _, name = spec.partition(":")
    same = settings.get("provider") == kind
    merged = {**(settings if same else {}), "provider": kind}
    if name:
        merged["name"] = name
    return build(merged)
