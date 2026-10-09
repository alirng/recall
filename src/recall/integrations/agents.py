"""Connect recall to coding agents: its MCP server, the /recall skill, and the
session-start hooks that load memory in Claude Code and Codex.

Every config file is backed up beside itself (<file>.recall-backup) before it
changes, and `recall uninstall` reverses each step.
"""

import json
import os
import re
import shutil
import subprocess
import sys

NAME = "recall"
COMPONENTS = ("mcp", "skill", "hook")

SKILL_SOURCE = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
SHARED_SKILLS = os.path.expanduser("~/.agents/skills")  # one copy, linked into each agent
SKILL_DIRS = {"Claude Code": "~/.claude/skills", "Codex": "~/.codex/skills", "Cursor": "~/.cursor/skills"}
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
CODEX_HOOKS = os.path.expanduser("~/.codex/hooks.json")
CURSOR_MCP = os.path.expanduser("~/.cursor/mcp.json")
_CODEX_BLOCK = re.compile(rf"\n?\[mcp_servers\.{NAME}\]\n(?:(?!\[).*\n?)*")


def install(components=COMPONENTS, dry_run: bool = False) -> list[str]:
    cmd = _recall_bin()
    report = []
    if "mcp" in components:
        report += [
            f"Claude Code  MCP: {_mcp_claude(cmd, dry_run)}",
            f"Codex        MCP: {_mcp_codex(cmd, dry_run)}",
            f"Cursor       MCP: {_mcp_cursor(cmd, dry_run)}",
        ]
    if "skill" in components:
        report += _skill(dry_run)
    if "hook" in components:
        report.append(f"Claude Code  {_hook_claude(cmd, dry_run)}")
        report.append(f"Codex        {_hook_codex(cmd, dry_run)}")
    return report


def uninstall(components=COMPONENTS) -> list[str]:
    report = []
    if "mcp" in components:
        report += [
            f"Claude Code  MCP: {_mcp_claude('', remove=True)}",
            f"Codex        MCP: {_mcp_codex('', remove=True)}",
            f"Cursor       MCP: {_mcp_cursor('', remove=True)}",
        ]
    if "skill" in components:
        report += _skill(remove=True)
    if "hook" in components:
        report.append(f"Claude Code  {_hook_claude('', remove=True)}")
        report.append(f"Codex        {_hook_codex('', remove=True)}")
    return report


# --- MCP server ---------------------------------------------------------------


def _mcp_claude(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    if not shutil.which("claude"):
        return "Claude Code not found, skipped"
    registered = _claude_registered()
    if remove:
        if registered and not dry_run:
            subprocess.run(["claude", "mcp", "remove", NAME, "-s", "user"], check=True, capture_output=True)
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        subprocess.run(
            ["claude", "mcp", "add", "-s", "user", NAME, "--", cmd, "serve"], check=True, capture_output=True
        )
    return f"added ({cmd} serve)"


def _mcp_codex(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    if not os.path.isdir(os.path.dirname(CODEX_CONFIG)):
        return "Codex not found, skipped"
    text = _read(CODEX_CONFIG)
    registered = f"[mcp_servers.{NAME}]" in text
    if remove:
        if registered and not dry_run:
            _write(CODEX_CONFIG, _CODEX_BLOCK.sub("\n", text))
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        # The tools only read, so Codex may call them without asking each time.
        block = (
            f'\n[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd)}\nargs = ["serve"]\n'
            'default_tools_approval_mode = "auto"\n'
        )
        _write(CODEX_CONFIG, text + block)
    return f"added to {CODEX_CONFIG}"


def _mcp_cursor(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    if not os.path.isdir(os.path.dirname(CURSOR_MCP)):
        return "Cursor not found, skipped"
    config = json.loads(_read(CURSOR_MCP) or "{}")
    servers = config.setdefault("mcpServers", {})
    registered = NAME in servers
    if remove:
        if registered and not dry_run:
            del servers[NAME]
            _write(CURSOR_MCP, json.dumps(config, indent=2))
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        servers[NAME] = {"command": cmd, "args": ["serve"]}
        _write(CURSOR_MCP, json.dumps(config, indent=2))
    return f"added to {CURSOR_MCP}"


# --- skill --------------------------------------------------------------------


def _skill(dry_run: bool = False, remove: bool = False) -> list[str]:
    shared = os.path.join(SHARED_SKILLS, NAME)
    report = []
    for agent, folder in SKILL_DIRS.items():
        folder = os.path.expanduser(folder)
        link = os.path.join(folder, NAME)
        if remove:
            if os.path.islink(link) and not dry_run:
                os.remove(link)
            report.append(f"{agent:12} skill unlinked")
        elif os.path.isdir(folder):
            if not os.path.lexists(link) and not dry_run:
                os.symlink(os.path.relpath(shared, folder), link)
            report.append(f"{agent:12} skill linked at {link}")
    if remove and os.path.isdir(shared) and not dry_run:
        shutil.rmtree(shared)
    elif not remove and not dry_run:
        os.makedirs(shared, exist_ok=True)
        shutil.copy2(SKILL_SOURCE, os.path.join(shared, "SKILL.md"))
    return report


# --- session-start hook -------------------------------------------------------


def _hook_claude(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """Claude Code adds a SessionStart hook's output to the session's context, so new,
    resumed and compacted sessions all start with the memory block."""
    if not os.path.isdir(os.path.dirname(CLAUDE_SETTINGS)):
        return "Claude Code not found, skipped"
    try:
        settings = json.loads(_read(CLAUDE_SETTINGS) or "{}")
    except json.JSONDecodeError:
        return f"{CLAUDE_SETTINGS} is not valid JSON, skipped"
    groups = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    ours = _our_hooks(settings)
    if remove:
        if ours and not dry_run:
            settings["hooks"]["SessionStart"] = [g for g in groups if g not in ours]
            if not settings["hooks"]["SessionStart"]:
                del settings["hooks"]["SessionStart"]
            _write(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
        return "session-start hook removed" if ours else "no session-start hook installed"
    if ours:
        return "session-start hook already installed"
    if not dry_run:
        groups.append({"hooks": [{"type": "command", "command": f"{cmd} context --hook", "timeout": 10}]})
        _write(CLAUDE_SETTINGS, json.dumps(settings, indent=2))
    return f"session-start hook added to {CLAUDE_SETTINGS}"


def _hook_codex(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """Codex runs SessionStart hooks from ~/.codex/hooks.json and adds their output to the
    session as developer context, once you have trusted the hook in Codex's /hooks."""
    if not os.path.isdir(os.path.dirname(CODEX_HOOKS)):
        return "Codex not found, skipped"
    try:
        config = json.loads(_read(CODEX_HOOKS) or "{}")
    except json.JSONDecodeError:
        return f"{CODEX_HOOKS} is not valid JSON, skipped"
    groups = config.setdefault("hooks", {}).setdefault("SessionStart", [])
    ours = _our_hooks(config)
    if remove:
        if ours and not dry_run:
            config["hooks"]["SessionStart"] = [g for g in groups if g not in ours]
            if not config["hooks"]["SessionStart"]:
                del config["hooks"]["SessionStart"]
            _write(CODEX_HOOKS, json.dumps(config, indent=2))
        return "session-start hook removed" if ours else "no session-start hook installed"
    if ours:
        return "session-start hook already installed"
    if not dry_run:
        hook = {
            "type": "command",
            "command": f"{cmd} context --hook --agent codex",
            "timeout": 10,
            "statusMessage": "Loading memory from recall",
        }
        groups.append({"hooks": [hook]})
        _write(CODEX_HOOKS, json.dumps(config, indent=2))
    return f"session-start hook added to {CODEX_HOOKS}; trust it once with /hooks in Codex"


def _our_hooks(settings: dict | None = None) -> list[dict]:
    if settings is None:
        settings = _json(CLAUDE_SETTINGS)
    groups = settings.get("hooks", {}).get("SessionStart", [])
    return [g for g in groups if any(_is_ours(h) for h in g.get("hooks", []))]


def _claude_registered() -> bool:
    return subprocess.run(["claude", "mcp", "get", NAME], capture_output=True).returncode == 0


def _is_ours(hook: dict) -> bool:
    command = hook.get("command", "")
    return NAME in command and " context" in command


# --- helpers ------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _json(path: str) -> dict:
    try:
        return json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return {}


def _write(path: str, text: str) -> None:
    if os.path.exists(path):
        shutil.copy2(path, path + ".recall-backup")
    with open(path, "w") as f:
        f.write(text)


def _recall_bin() -> str:
    found = shutil.which("recall")
    if not found:
        sys.exit("recall is not on PATH: install it with `uv tool install .` first")
    return os.path.realpath(found)
