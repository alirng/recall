"""Connect recall to coding agents: its MCP server, the /recall skill, the session-start
hooks that load memory in Claude Code and Codex, and the edit hooks that apply your
coordination rules when another chat has open changes to a file an agent is about to edit.

Every config file is backed up beside itself (<file>.recall-backup) before it
changes, and `recall uninstall` reverses each step.
"""

import json
import os
import re
import shutil
import subprocess
import sys

from . import hooks as edit_hooks

NAME = "recall"
COMPONENTS = ("mcp", "skill", "hook")

SKILL_SOURCE = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
SHARED_SKILLS = os.path.expanduser("~/.agents/skills")  # one copy, linked into each agent
SKILL_DIRS = {"Claude Code": "~/.claude/skills", "Codex": "~/.codex/skills", "Cursor": "~/.cursor/skills"}
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
CODEX_HOOKS = os.path.expanduser("~/.codex/hooks.json")
CURSOR_MCP = os.path.expanduser("~/.cursor/mcp.json")
CURSOR_HOOKS = os.path.expanduser("~/.cursor/hooks.json")
GEMINI_SETTINGS = os.path.expanduser("~/.gemini/settings.json")
OPENCODE_PLUGIN = os.path.expanduser("~/.config/opencode/plugins/recall.js")
OPENCODE_SOURCE = os.path.join(os.path.dirname(__file__), "opencode", "recall.js")
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
        report.append(f"Cursor       {_hook_cursor(cmd, dry_run)}")
        report.append(f"Gemini CLI   {_hook_gemini(cmd, dry_run)}")
        report.append(f"OpenCode     {_hook_opencode(cmd, dry_run)}")
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
        report.append(f"Cursor       {_hook_cursor('', remove=True)}")
        report.append(f"Gemini CLI   {_hook_gemini('', remove=True)}")
        report.append(f"OpenCode     {_hook_opencode('', remove=True)}")
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


# --- hooks ----------------------------------------------------------------------


def _hook_claude(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """SessionStart output joins the session's context, so new, resumed and compacted
    sessions all start with the memory block. The PreToolUse hook applies the coordination
    rules before each edit (hooks.py)."""
    if not os.path.isdir(os.path.dirname(CLAUDE_SETTINGS)):
        return "Claude Code not found, skipped"
    wanted = {
        "SessionStart": {"hooks": [{"type": "command", "command": f"{cmd} context --hook", "timeout": 10}]},
        "PreToolUse": {
            "matcher": edit_hooks.EDIT_TOOLS["claude"],
            "hooks": [{"type": "command", "command": f"{cmd} status --hook --agent claude", "timeout": 10}],
        },
    }
    return _hook_file(CLAUDE_SETTINGS, wanted, dry_run, remove)


def _hook_codex(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """Codex adds SessionStart output to the session as developer context, and runs the
    PreToolUse hook before each apply_patch, once you have trusted them in Codex's /hooks."""
    if not os.path.isdir(os.path.dirname(CODEX_HOOKS)):
        return "Codex not found, skipped"
    start = {"type": "command", "command": f"{cmd} context --hook --agent codex", "timeout": 10}
    edit = {"type": "command", "command": f"{cmd} status --hook --agent codex", "timeout": 10}
    wanted = {
        "SessionStart": {"hooks": [start | {"statusMessage": "Loading memory from recall"}]},
        "PreToolUse": {"matcher": edit_hooks.EDIT_TOOLS["codex"], "hooks": [edit]},
    }
    report = _hook_file(CODEX_HOOKS, wanted, dry_run, remove)
    return report + ("; trust them once with /hooks in Codex" if " added " in report else "")


def _hook_cursor(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """Cursor's preToolUse can refuse an edit and its postToolUse can add context, so the edit
    hook goes in both (hooks.py). Cursor reloads the file on save."""
    if not os.path.isdir(os.path.dirname(CURSOR_HOOKS)):
        return "Cursor not found, skipped"
    edit = {"command": f"{cmd} status --hook --agent cursor", "matcher": edit_hooks.EDIT_TOOLS["cursor"], "timeout": 10}
    return _hook_file(CURSOR_HOOKS, {"preToolUse": edit, "postToolUse": edit}, dry_run, remove, flat=True)


def _hook_gemini(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """Gemini CLI's BeforeTool can refuse an edit and AfterTool can add context; its timeouts
    are in milliseconds."""
    if not (shutil.which("gemini") or os.path.exists(GEMINI_SETTINGS)):
        return "Gemini CLI not found, skipped"
    edit = {
        "type": "command",
        "name": "recall-edit",
        "command": f"{cmd} status --hook --agent gemini",
        "timeout": 10000,
    }
    group = {"matcher": edit_hooks.EDIT_TOOLS["gemini"], "hooks": [edit]}
    return _hook_file(GEMINI_SETTINGS, {"BeforeTool": group, "AfterTool": group}, dry_run, remove)


def _hook_opencode(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    """OpenCode takes hooks as plugins: recall's is one file in its global plugins folder."""
    if not os.path.isdir(os.path.dirname(os.path.dirname(OPENCODE_PLUGIN))):
        return "OpenCode not found, skipped"
    installed = os.path.exists(OPENCODE_PLUGIN)
    if remove:
        if installed and not dry_run:
            os.remove(OPENCODE_PLUGIN)
        return "edit plugin removed" if installed else "no edit plugin installed"
    with open(OPENCODE_SOURCE) as f:
        plugin = f.read().replace("__RECALL__", cmd)
    if installed and _read(OPENCODE_PLUGIN) == plugin:
        return "edit plugin already installed"
    if not dry_run:
        os.makedirs(os.path.dirname(OPENCODE_PLUGIN), exist_ok=True)
        _write(OPENCODE_PLUGIN, plugin)
    return f"edit plugin added at {OPENCODE_PLUGIN}"


def _hook_file(path: str, wanted: dict, dry_run: bool, remove: bool, flat: bool = False) -> str:
    """Add or remove recall's entries in one agent's hooks file, in one write so the backup
    holds the file as it was. Claude Code and Codex wrap commands in groups under each event;
    Cursor lists them directly (flat)."""
    try:
        config = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return f"{path} is not valid JSON, skipped"
    hooks = config.setdefault("hooks", {})
    changed = []
    for event, entry in wanted.items():
        entries = hooks.get(event, [])
        ours = [e for e in entries if (_is_ours(e) if flat else any(_is_ours(h) for h in e.get("hooks", [])))]
        if remove and ours:
            hooks[event] = [e for e in entries if e not in ours]
            if not hooks[event]:
                del hooks[event]
            changed.append(event)
        elif not remove and not ours:
            hooks.setdefault(event, []).append(entry)
            changed.append(event)
    if flat:
        config.setdefault("version", 1)
    if changed and not dry_run:
        _write(path, json.dumps(config, indent=2))
    kinds = list(dict.fromkeys("session-start" if e == "SessionStart" else "edit" for e in changed))
    what = " and ".join(kinds) + (" hooks" if len(changed) > 1 else " hook")
    if remove:
        return f"{what} removed" if changed else "no hooks installed"
    return f"{what} added to {path}" if changed else "hooks already installed"


def _claude_registered() -> bool:
    return subprocess.run(["claude", "mcp", "get", NAME], capture_output=True).returncode == 0


def _is_ours(hook: dict) -> bool:
    command = hook.get("command", "")
    return NAME in command and (" context" in command or " status --hook" in command)


# --- helpers ------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


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
