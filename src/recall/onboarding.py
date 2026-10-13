"""`recall setup`: choose agents and a model, then index, connect, schedule and start."""

import os
import subprocess
import sys

from . import config, keys, paths
from .chats import indexer, store
from .chats.sources import agents
from .integrations import agents as integrations
from .integrations import scheduler
from .llm import API_BASE_URLS, LOCAL_SERVERS, Client, FatalError, LLMError, Provider, build
from .llm.agent_cli import ChatGPT, Claude
from .llm.base import http_json

SMALL_MODEL_HINTS = ("luna", "mini", "nano", "haiku", "flash", "small", "lite", "4b", "3b")
API_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


class Prompter:
    """Terminal questions. With assume_yes (or no terminal), every default is taken."""

    def __init__(self, assume_yes: bool = False):
        self.auto = assume_yes or not sys.stdin.isatty()

    def ask(self, question: str, default: str = "") -> str:
        if self.auto:
            return default
        answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = True) -> bool:
        return self.ask(f"{question} (y/n)", "y" if default else "n").lower().startswith("y")

    def choose(self, question: str, options: list[tuple[str, str]], default: int = 0) -> int:
        """options: (label, detail). Returns the chosen index."""
        print(question)
        for i, (label, detail) in enumerate(options, 1):
            print(f"  {i}  {label:<16} {detail}")
        while True:
            answer = self.ask("Choose", str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            print(f"  enter a number from 1 to {len(options)}")


def run(assume_yes: bool = False, model: str | None = None, embeddings: str | None = None) -> None:
    """The guided first run. model (e.g. "claude:haiku", "local:qwen3:4b") and embeddings (an
    Ollama embedding model, e.g. "nomic-embed-text") answer those questions up front, so an
    agent can install recall in one command after asking the person."""
    ask = Prompter(assume_yes)
    cfg = config.load()

    print("\n1. Agents\n")
    found = _detect_agents()
    choice = ask.ask("\nImport from which agents? (comma-separated names, or all)", "all")
    cfg["sources"] = [] if choice == "all" else [a.strip() for a in choice.split(",") if a.strip() in found]
    config.save(cfg)

    print("\n2. Who writes your memories\n")
    if model:
        cfg["model"] = _settings_from_spec(model)
        _test(build(cfg["model"]))
    else:
        cfg["model"] = choose_model(ask, current=cfg["model"])
    if embeddings:
        cfg["embeddings"] = {"provider": "ollama", "name": embeddings}
    config.save(cfg)

    print("\n3. Indexing chats…")
    result = indexer.index(store.connect(), log=lambda _: None)
    print("   " + ", ".join(f"{name}: {r['indexed']} new" for name, r in result.items()))

    print()
    if ask.yes("4. Connect recall to your agents (MCP tools, /recall skill, session-start hooks)?"):
        for line in integrations.install():
            print("   " + line)

    dream = cfg["dream"]
    if ask.yes("\n5. Update memory every night at 03:30?"):
        print("   " + scheduler.enable(3, 30, dream["max_minutes"], dream["max_calls"]))

    if ask.yes("\n6. Start the first memory run now, in the background (newest chats first)?"):
        _start_first_run(dream)
    if embeddings:
        print("\n7. Embedding your chats for semantic search, in the background…")
        _start_background("embed")
    print(f"\nDone. Your memory lives in {paths.MEMORY}; start at MEMORY.md.")


def choose_model(ask: Prompter, current: dict | None = None) -> dict:
    """Ask which model writes memories, test it with a live call, and return its settings."""
    if current and current.get("provider"):
        label = build(current).label
        if ask.yes(f"Keep using {label}?"):
            _test(build(current))
            return current
    chatgpt, claude = ChatGPT(""), Claude()
    local = _running_local_servers()
    options = [
        (
            "Local model",
            "private: nothing leaves this machine · "
            + (", ".join(local) + " running" if local else "start Ollama or LM Studio first"),
        ),
        ("ChatGPT", "uses your ChatGPT plan, through Codex · " + (chatgpt.check() or "ready")),
        ("Claude", "uses your Claude plan, through Claude Code · " + (claude.check() or "ready")),
        ("API key", "OpenAI, Anthropic, OpenRouter, or an OpenAI-compatible server you host"),
    ]
    print("recall reads your chats and distils them with a language model of your choosing.\n")
    # Suggest whatever needs least setup: a signed-in plan, then a running local server.
    ready = {1: chatgpt.check() is None, 2: claude.check() is None, 0: bool(local), 3: True}
    default = next(i for i, ok in ready.items() if ok)
    while True:
        pick = ask.choose("Which one?", options, default)
        try:
            settings = [_local_settings, _chatgpt_settings, _claude_settings, _api_settings][pick](ask, local)
            provider = build(settings)
            _test(provider)
            return settings
        except (FatalError, LLMError, ValueError) as e:
            print(f"   that did not work: {e}\n")
            if ask.auto:
                raise


def _local_settings(ask: Prompter, servers: dict[str, str]) -> dict:
    if not servers:
        raise ValueError("no local model server is running (start Ollama or LM Studio and try again)")
    names = list(servers)
    server = names[ask.choose("Which server?", [(n, servers[n]) for n in names])] if len(names) > 1 else names[0]
    settings = {
        "provider": "local",
        "base_url": servers[server],
        "server": "ollama" if server == "Ollama" else "openai",
    }
    models = build({**settings, "name": ""}).models()
    if not models:
        raise ValueError(f"{server} has no models yet (for Ollama: `ollama pull qwen3:4b`)")
    print("   Smaller models run cooler: a 4B model suits 16 GB of memory, 8B wants 24 GB or more.")
    settings["name"] = ask.ask("Model", _small_first(models))
    return settings


def _chatgpt_settings(ask: Prompter, _servers) -> dict:
    problem = ChatGPT("").check()
    if problem:
        raise ValueError(problem)
    name = ask.ask("Model", _small_first(ChatGPT("").models()) or "")
    return {"provider": "chatgpt", "name": name, "effort": ask.ask("Reasoning effort", "medium")}


def _claude_settings(ask: Prompter, _servers) -> dict:
    problem = Claude().check()
    if problem:
        raise ValueError(problem)
    return {"provider": "claude", "name": ask.ask("Model (haiku, sonnet or opus)", "haiku")}


def _api_settings(ask: Prompter, _servers) -> dict:
    services = [
        ("OpenAI", API_BASE_URLS["openai"]),
        ("Anthropic", "api.anthropic.com"),
        ("OpenRouter", API_BASE_URLS["openrouter"]),
        ("Other", "any OpenAI-compatible URL"),
    ]
    kind = ["openai", "anthropic", "openrouter", "custom"][ask.choose("Which service?", services)]
    settings = {"provider": kind}
    if kind == "custom":
        settings["base_url"] = ask.ask("Base URL (e.g. http://my-server:8000/v1)")
    settings["api_key"] = _store_key(ask, kind)
    provider = build({**settings, "name": ""})
    try:
        models = provider.models()
    except (FatalError, LLMError):
        models = []
    settings["name"] = ask.ask("Model", _small_first(models))
    return settings


def _store_key(ask: Prompter, kind: str) -> str:
    env_name = API_KEY_ENV.get(kind)
    key = os.environ.get(env_name, "") if env_name else ""
    if not (key and ask.yes(f"Use the key in ${env_name}?")):
        key = ask.ask(f"Paste your {kind} API key (leave blank if your server needs none)")
    if not key:
        return ""
    # The nightly run cannot see your shell's environment, so keep a copy in the OS key store.
    ref = keys.save(kind, key)
    print(f"   key saved to {keys.storage_name()}; config.toml only records where it is")
    return ref


def _test(provider: Provider) -> None:
    from .memory import extract

    problem = provider.check()
    if problem:
        raise ValueError(problem)
    print(f"   testing {provider.label}…", end=" ", flush=True)
    sample = (
        "=== CHAT 1\nChat setup-check | agent: claude | date: today\nProject: none\nTitle: check\n"
        "[U1] When I say commit, I always mean commit and push."
    )
    data = Client(provider).json("setup-check", extract.SYSTEM, sample, extract.SCHEMA)
    print("ok")
    where = "nothing leaves this machine" if provider.private else f"chat excerpts go to {provider.destination}"
    print(f"   {where}. Learned from the test chat: {[m['title'] for m in extract.memories(data, 1)[1]]}")


def _detect_agents() -> dict[str, int]:
    found = {}
    for key, (name, detect, _) in agents().items():
        n = detect()
        print(f"  {'✓' if n is not None else '·'} {name:12} {f'{n:,} chats' if n is not None else 'not found'}")
        if n is not None:
            found[key] = n
    if not found:
        sys.exit("\nNo supported agents found.")
    return found


def _running_local_servers() -> dict[str, str]:
    running = {}
    for name, url in LOCAL_SERVERS.items():
        try:
            http_json(f"{url}/api/tags" if name == "Ollama" else f"{url}/models", timeout=1)
            running[name] = url
        except Exception:
            continue
    return running


def _small_first(models: list[str]) -> str:
    """Suggest the smallest-sounding model: memory extraction is easy, cheap work."""
    for hint in SMALL_MODEL_HINTS:
        for m in models:
            if hint in m.lower():
                return m
    return models[0] if models else ""


def _settings_from_spec(spec: str) -> dict:
    """ "claude:haiku" or "local:qwen3:4b" as [model] settings. API providers need a key, which
    only `recall model` asks for."""
    kind, _, name = spec.partition(":")
    if kind in API_BASE_URLS or kind in ("anthropic", "custom"):
        raise SystemExit(f"{kind} needs an API key: run `recall model` to set it up")
    settings = {"provider": kind, **({"name": name} if name else {})}
    if kind == "local":
        settings["server"] = "ollama"
    return settings


def _start_background(*args: str) -> None:
    os.makedirs(paths.LOGS, exist_ok=True)
    log = os.path.join(paths.LOGS, f"{args[0]}.log")
    with open(log, "a") as f:
        subprocess.Popen([sys.argv[0], *args], stdout=f, stderr=f, start_new_session=True)
    print(f"   running; follow along with: tail -f {log}")


def _start_first_run(dream: dict) -> None:
    os.makedirs(paths.LOGS, exist_ok=True)
    log = os.path.join(paths.LOGS, "dream.log")
    with open(log, "a") as f:
        subprocess.Popen(
            [sys.argv[0], "dream", "--max-minutes", str(dream["max_minutes"]), "--max-calls", str(dream["max_calls"])],
            stdout=f,
            stderr=f,
            start_new_session=True,
        )
    print(f"   running; follow along with: tail -f {log}")
