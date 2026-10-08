"""Sign in with ChatGPT or Claude: run the user's own Codex or Claude Code CLI
headlessly, so calls are covered by their plan and no API key is needed.

Both run so that recall's calls never show up in the user's chat history, never
load the user's MCP servers, hooks or plugins, and cannot write to disk.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

from .base import FatalError, LLMError, Profile, Provider, Result, parse_json

CODEX_IN_CHATGPT_APP = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


def plan_env(*api_key_vars: str) -> dict:
    """These CLIs prefer an API key found in the environment over the signed-in plan,
    which would bill an API account instead. Drop those variables."""
    env = dict(os.environ)
    for name in api_key_vars:
        env.pop(name, None)
    return env


class ChatGPT(Provider):
    id = "chatgpt"
    title = "ChatGPT plan, through Codex"
    destination = "OpenAI, under your ChatGPT plan"
    # Every `codex exec` carries ~16k tokens of agent harness, so batch generously.
    profile = Profile(batch_chars=100_000, batch_chats=12, workers=3)
    BRIEF = (
        "You are a JSON extraction function. Read the input and answer directly with JSON "
        "matching the requested schema. Do not run commands or use tools."
    )

    def __init__(self, model: str, effort: str = "medium"):
        super().__init__(model)
        self.effort = effort
        self.bin = shutil.which("codex") or (CODEX_IN_CHATGPT_APP if os.path.exists(CODEX_IN_CHATGPT_APP) else None)

    @property
    def label(self) -> str:
        return f"{self.id}:{self.model}@{self.effort}"

    def check(self) -> str | None:
        if not self.bin:
            return "Codex is not installed (it ships with the ChatGPT desktop app, or `npm i -g @openai/codex`)"
        if not os.path.exists(os.path.join(CODEX_HOME, "auth.json")):
            return "sign in to Codex first: run `codex login`"
        return None

    def models(self) -> list[str]:
        """The models the signed-in plan offers, from Codex's own catalogue cache."""
        try:
            with open(os.path.join(CODEX_HOME, "models_cache.json")) as f:
                catalogue = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        entries = catalogue.get("models", []) if isinstance(catalogue, dict) else catalogue
        return [m["slug"] for m in entries if isinstance(m, dict) and m.get("slug")]

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        with tempfile.TemporaryDirectory() as tmp:
            brief, schema_path, out_path = (os.path.join(tmp, n) for n in ("brief.md", "schema.json", "out.json"))
            with open(brief, "w") as f:
                f.write(self.BRIEF)
            with open(schema_path, "w") as f:
                json.dump(schema, f)
            overrides = {
                "model_reasoning_effort": json.dumps(self.effort),
                "model_instructions_file": json.dumps(brief),
                "project_doc_max_bytes": "0",
                "include_apps_instructions": "false",
                "include_permissions_instructions": "false",
                "include_environment_context": "false",
                "skills.enabled": "false",
                "tools.web_search": "false",
            }
            cmd = [
                self.bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "--output-schema",
                schema_path,
                "-o",
                out_path,
                *(["-m", self.model] if self.model else []),
                *(arg for k, v in overrides.items() for arg in ("-c", f"{k}={v}")),
                "-",
            ]
            start = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    input=f"{system}\n\n{prompt}",
                    capture_output=True,
                    text=True,
                    timeout=900,
                    cwd=tmp,
                    env=plan_env("OPENAI_API_KEY", "CODEX_API_KEY"),
                )
            except subprocess.TimeoutExpired as e:
                raise LLMError("codex timed out") from e
            if proc.returncode != 0 or not os.path.exists(out_path):
                detail = proc.stderr.strip()[-300:]
                if "login" in detail.lower() or "unauthorized" in detail.lower():
                    raise FatalError(f"Codex is not signed in: {detail}")
                raise LLMError(f"codex exited {proc.returncode}: {detail}")
            with open(out_path) as f:
                data = parse_json(f.read())
        usage = {}
        for line in proc.stdout.splitlines():
            if '"turn.completed"' in line:
                usage = json.loads(line).get("usage", {})
        return Result(data, usage.get("input_tokens", 0), usage.get("output_tokens", 0), time.time() - start)


class Claude(Provider):
    id = "claude"
    title = "Claude plan, through Claude Code"
    destination = "Anthropic, under your Claude plan"
    profile = Profile(batch_chars=60_000, batch_chats=8, workers=3)

    def __init__(self, model: str = "haiku"):
        super().__init__(model or "haiku")
        self.bin = shutil.which("claude")

    def check(self) -> str | None:
        return None if self.bin else "Claude Code is not installed (https://claude.com/claude-code)"

    def models(self) -> list[str]:
        return ["haiku", "sonnet", "opus"]

    def complete(self, system: str, prompt: str, schema: dict) -> Result:
        # Not --bare: bare mode accepts API keys only, never a signed-in plan.
        cmd = [
            self.bin,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "project",
            "--no-session-persistence",
        ]
        # Extended thinking adds 3-5x the output tokens to extraction and no quality.
        env = {**plan_env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "MAX_THINKING_TOKENS": "0"}
        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=tempfile.gettempdir(),
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError("claude timed out") from e
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}") from e
        if out.get("is_error"):
            message = str(out.get("result"))[:300]
            if "not logged in" in message.lower():
                raise FatalError(f"Claude Code is not signed in: {message}")
            raise LLMError(f"claude: {message}")
        data = out.get("structured_output") or parse_json(str(out.get("result", "")))
        usage = out.get("usage", {})
        tokens_in = sum(
            usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return Result(data, tokens_in, usage.get("output_tokens", 0), time.time() - start)
