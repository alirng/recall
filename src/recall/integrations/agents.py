"""Connect recall to coding agents: its MCP server, the /recall skill, the session-start
hooks that load memory in Claude Code and Codex, and the edit hooks that apply your
coordination rules when another chat has open changes to a file an agent is about to edit.

Every config file is backed up beside itself (<file>.recall-backup) before it
changes, and `recall uninstall` reverses each step. A run that changes a file twice
keeps the backup of its first state.
"""

import json
import os
import re
import shlex
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


def install(components=COMPONENTS, dry_run: bool = False, roots: list | None = None) -> list[str]:
    """Connect every root recall found (roots.discover()), or just `roots`."""
    cmd = _recall_bin()
    report = _roots(components, cmd, dry_run, roots)
    if "hook" in components:
        report.append(f"Gemini CLI   {_hook_gemini(cmd, dry_run)}")
        report.append(f"OpenCode     {_hook_opencode(cmd, dry_run)}")
    return report


def uninstall(components=COMPONENTS, roots: list | None = None) -> list[str]:
    report = _roots(components, "", False, roots, remove=True)
    if "hook" in components:
        report.append(f"Gemini CLI   {_hook_gemini('', remove=True)}")
        report.append(f"OpenCode     {_hook_opencode('', remove=True)}")
    return report


def _roots(components, cmd: str, dry_run: bool, roots: list | None, remove: bool = False) -> list[str]:
    from .. import roots as harness

    found = harness.discover() if roots is None else roots
    report = []
    for agent, label in (("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor")):
        mine = [r for r in connectable(found) if r.agent == agent]
        if not mine:
            report.append(f"{label:12} not found, skipped")
        for r in mine:
            report += connect_root(r, cmd, components, dry_run, remove)
    return report


def connectable(roots: list) -> list:
    """The roots that exist, one per place recall writes to: Cursor's builds share ~/.cursor."""
    out, seen = [], set()
    for r in roots:
        key = (r.agent, os.path.realpath(r.path), r.by_env)
        if r.exists and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def connect_root(root, cmd: str, components=COMPONENTS, dry_run: bool = False, remove: bool = False) -> list[str]:
    """Install (or remove) recall's MCP server, skill and hooks in one root. Each config file is
    backed up beside itself first; a part that is already in place is left alone."""
    f = root_files(root)
    lead = f"{root.label:12}"
    out = []
    if "mcp" in components:
        mcp = {"claude": _mcp_claude, "codex": _mcp_codex, "cursor": _mcp_cursor}[root.agent]
        out.append(f"{lead} MCP: {mcp(cmd, dry_run, remove, root)}")
    if "skill" in components:
        out += _skill(dry_run, remove, {root.label: f["skills"]})
    if "hook" in components:
        hook = {"claude": _hook_claude, "codex": _hook_codex, "cursor": _hook_cursor}[root.agent]
        out.append(f"{lead} {hook(cmd, dry_run, remove, f['hook'])}")
    return out


def root_files(root) -> dict[str, str]:
    """The three files recall touches in a root: where its MCP server is listed, its hooks, its skills."""
    from .. import roots as harness

    if root.agent == "claude":
        return {
            "mcp": harness.claude_global_file(root),
            "hook": os.path.join(root.path, "settings.json"),
            "skills": os.path.join(root.path, "skills"),
        }
    mcp = "config.toml" if root.agent == "codex" else "mcp.json"
    return {
        "mcp": os.path.join(root.path, mcp),
        "hook": os.path.join(root.path, "hooks.json"),
        "skills": os.path.join(root.path, "skills"),
    }


def parts(root, cmd: str | None = None) -> dict[str, bool | None]:
    """Which parts of recall are in place in this root: mcp, hook, skill. Reads files only and
    never runs an agent, so it is cheap enough for a hook. None: cannot tell (recall not on PATH).
    Codex runs its hooks only after a one-time trust in /hooks, which recall cannot see."""
    f = root_files(root)
    if cmd is None:
        try:
            cmd = _recall_bin()
        except SystemExit:
            cmd = None
    hook = None
    if cmd:
        hook = _hook_state(root, cmd)
    link = os.path.join(f["skills"], NAME)
    return {"mcp": _mcp_registered(root), "hook": hook, "skill": os.path.exists(link)}


def _mcp_registered(root) -> bool:
    f = root_files(root)["mcp"]
    if root.agent == "codex":
        return f"[mcp_servers.{NAME}]" in _read(f)
    try:
        return NAME in (json.loads(_read(f) or "{}").get("mcpServers") or {})
    except (json.JSONDecodeError, AttributeError):
        return False


def _hook_state(root, cmd: str) -> bool:
    flat = root.agent == "cursor"
    report = _hook_file(root_files(root)["hook"], _entries(root.agent, cmd), True, False, flat=flat)
    return report == "hooks already installed"


# --- MCP server ---------------------------------------------------------------


def _mcp_claude(cmd: str, dry_run: bool = False, remove: bool = False, root=None) -> str:
    """Claude Code's own `mcp add`, run with the root's CLAUDE_CONFIG_DIR (and without one for the
    default root, even when this process was started from inside another root's session)."""
    from .. import roots as harness

    if not shutil.which("claude"):
        return "Claude Code not found, skipped"
    root = root or _default("claude")
    registered = _mcp_registered(root)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if root.by_env:
        env["CLAUDE_CONFIG_DIR"] = root.path
    where = harness.claude_global_file(root)
    if remove:
        if registered and not dry_run:
            _backup_claude(root, where)
            _run(["claude", "mcp", "remove", NAME, "-s", "user"], env)
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        _backup_claude(root, where)
        failed = _run(["claude", "mcp", "add", "-s", "user", NAME, "--", cmd, "serve"], env)
        if failed:
            return f"could not add: {failed}"
    return f"added ({cmd} serve)"


def _mcp_codex(cmd: str, dry_run: bool = False, remove: bool = False, root=None) -> str:
    config = root_files(root)["mcp"] if root else CODEX_CONFIG
    if not os.path.isdir(os.path.dirname(config)):
        return "Codex not found, skipped"
    text = _read(config)
    registered = f"[mcp_servers.{NAME}]" in text
    if remove:
        if registered and not dry_run:
            _write(config, _CODEX_BLOCK.sub("\n", text))
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        # The tools only read, so Codex may call them without asking each time.
        block = (
            f'\n[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd)}\nargs = ["serve"]\n'
            'default_tools_approval_mode = "auto"\n'
        )
        _write(config, text + block)
    return f"added to {config}"


def _mcp_cursor(cmd: str, dry_run: bool = False, remove: bool = False, root=None) -> str:
    config_path = root_files(root)["mcp"] if root else CURSOR_MCP
    if not os.path.isdir(os.path.dirname(config_path)):
        return "Cursor not found, skipped"
    try:
        config = json.loads(_read(config_path) or "{}")
    except json.JSONDecodeError:
        return f"{config_path} is not valid JSON, skipped"
    servers = config.setdefault("mcpServers", {})
    registered = NAME in servers
    if remove:
        if registered and not dry_run:
            del servers[NAME]
            _write(config_path, json.dumps(config, indent=2))
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        servers[NAME] = {"command": cmd, "args": ["serve"]}
        _write(config_path, json.dumps(config, indent=2))
    return f"added to {config_path}"


def _backup_claude(root, global_file: str) -> None:
    """Claude Code migrates settings.json and .claude.json whenever it runs, `mcp add` included."""
    _backup(global_file)
    _backup(os.path.join(root.path, "settings.json"))


def _default(agent: str):
    from .. import roots as harness

    return harness.Root(agent, agent, harness.default_dir(agent), default=True)


def _run(args: list[str], env: dict) -> str | None:
    """Run an agent's CLI; None on success, else why not."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return None if done.returncode == 0 else (done.stderr or done.stdout).strip()[:200] or f"exit {done.returncode}"


# --- skill --------------------------------------------------------------------


def _skill(dry_run: bool = False, remove: bool = False, folders: dict[str, str] | None = None) -> list[str]:
    """One shared copy of the skill, linked into each agent's skills folder: the default folders,
    or `folders` (label -> skills folder; created when missing)."""
    shared = os.path.join(SHARED_SKILLS, NAME)
    report = []
    for agent, folder in (folders or SKILL_DIRS).items():
        folder = os.path.expanduser(folder)
        link = os.path.join(folder, NAME)
        if remove:
            if os.path.islink(link) and not dry_run:
                os.remove(link)
            report.append(f"{agent:12} skill unlinked")
        elif folders or os.path.isdir(folder):
            if not os.path.lexists(link) and not dry_run:
                os.makedirs(folder, exist_ok=True)
                os.symlink(os.path.relpath(shared, folder), link)
            report.append(f"{agent:12} skill linked at {link}")
    target = os.path.join(shared, "SKILL.md")
    if remove and os.path.isdir(shared) and not dry_run:
        shutil.rmtree(shared)
    elif not remove and not dry_run and _read(target) != _read(SKILL_SOURCE):
        os.makedirs(shared, exist_ok=True)
        shutil.copy2(SKILL_SOURCE, target)
    return report


# --- hooks ----------------------------------------------------------------------


def _entries(agent: str, cmd: str) -> list[tuple[str, dict]]:
    """recall's entries for one agent, from hooks.INSTALL: every one runs `recall hook`."""
    command = f"{cmd} hook --agent {agent}"
    out = []
    for event, matcher, *extras in edit_hooks.INSTALL[agent]:
        if agent == "cursor":  # Cursor lists commands directly under each event
            entry = {"command": command, "timeout": 10, **(extras[0] if extras else {})}
        elif agent == "gemini":  # Gemini CLI wants a name, and its timeouts are in milliseconds
            entry = {"hooks": [{"type": "command", "name": "recall", "command": command, "timeout": 10000}]}
        else:
            hook = {"type": "command", "command": command, "timeout": 10}
            if agent == "codex" and event == "SessionStart":
                hook["statusMessage"] = "Loading memory from recall"
            entry = {"hooks": [hook]}
        out.append((event, {"matcher": matcher, **entry} if matcher else entry))
    return out


def _hook_claude(cmd: str, dry_run: bool = False, remove: bool = False, settings: str = CLAUDE_SETTINGS) -> str:
    """SessionStart output joins the session's context, so new, resumed and compacted
    sessions all start with the memory block; the other entries are in hooks.INSTALL."""
    if not os.path.isdir(os.path.dirname(settings)):
        return "Claude Code not found, skipped"
    return _hook_file(settings, _entries("claude", cmd), dry_run, remove)


def _hook_codex(cmd: str, dry_run: bool = False, remove: bool = False, hooks: str = CODEX_HOOKS) -> str:
    """Codex runs its hooks from ~/.codex/hooks.json once you have trusted them in /hooks."""
    if not os.path.isdir(os.path.dirname(hooks)):
        return "Codex not found, skipped"
    report = _hook_file(hooks, _entries("codex", cmd), dry_run, remove)
    return report + ("; trust them once with /hooks in Codex" if " written " in report else "")


def _hook_cursor(cmd: str, dry_run: bool = False, remove: bool = False, hooks: str = CURSOR_HOOKS) -> str:
    """Cursor reloads ~/.cursor/hooks.json on save."""
    if not os.path.isdir(os.path.dirname(hooks)):
        return "Cursor not found, skipped"
    return _hook_file(hooks, _entries("cursor", cmd), dry_run, remove, flat=True)


def _hook_gemini(cmd: str, dry_run: bool = False, remove: bool = False) -> str:
    if not (shutil.which("gemini") or os.path.exists(GEMINI_SETTINGS)):
        return "Gemini CLI not found, skipped"
    return _hook_file(GEMINI_SETTINGS, _entries("gemini", cmd), dry_run, remove)


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


def _hook_file(path: str, entries: list[tuple[str, dict]], dry_run: bool, remove: bool, flat: bool = False) -> str:
    """Make recall's entries in one agent's hooks file exactly `entries`, keyed by (event,
    matcher): add the missing, replace stale ones (an older command, a dropped matcher),
    leave everyone else's alone. One write, so the backup holds the file as it was. Claude
    Code, Codex and Gemini CLI wrap commands in groups under each event; Cursor lists them
    directly (flat)."""
    try:
        config = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return f"{path} is not valid JSON, skipped"
    hooks = config.setdefault("hooks", {})

    def ours(e: dict) -> bool:
        return _is_ours(e) if flat else any(_is_ours(h) for h in e.get("hooks", []))

    changed = []
    for event in list(hooks):
        keep = [e for e in hooks[event] if not ours(e) or (not remove and (event, e) in entries)]
        if len(keep) != len(hooks[event]):
            changed.append(event)
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if not remove:
        for event, entry in entries:
            if entry not in hooks.get(event, []):
                hooks.setdefault(event, []).append(entry)
                changed.append(event)
    if flat:
        config.setdefault("version", 1)
    changed = list(dict.fromkeys(changed))
    if changed and not dry_run:
        _write(path, json.dumps(config, indent=2))
    if remove:
        return f"hooks removed ({', '.join(changed)})" if changed else "no hooks installed"
    return f"hooks written to {path} ({', '.join(changed)})" if changed else "hooks already installed"


def _is_ours(hook: dict) -> bool:
    """recall's own entries: its executable running hook, or the older context/status --hook."""
    try:
        words = shlex.split(hook.get("command", ""))
    except ValueError:
        return False
    if len(words) < 2 or os.path.basename(words[0]) != NAME:
        return False
    return words[1] == "hook" or (words[1] in ("context", "status") and "--hook" in words)


# --- how long agents keep chats ------------------------------------------------

CLAUDE_KEEP_DAYS = 3650


def retention(ask=None, dry_run: bool = False) -> list[str]:
    """How long each agent keeps its chats. When Claude Code would delete them within a
    year and no organisation sets the period, ask(question) decides whether to raise
    `cleanupPeriodDays`; without ask, nothing changes. uninstall never lowers it again,
    because Claude Code would then delete every older chat at its next start."""
    from .. import retention as policy

    report = []
    if os.path.isdir(os.path.dirname(CLAUDE_SETTINGS)):
        managed, days = policy.claude_managed(), policy.claude_user_days()
        current = policy.CLAUDE_DEFAULT_DAYS if days is None else days
        was = f"{current} (default)" if days is None else str(days)  # the person's own value, even an invalid 0
        if managed:
            report.append(
                f"Claude Code  your organisation keeps chats {managed[0]} days ({managed[1]}); "
                "recall keeps them no longer"
            )
        elif current >= 365:
            report.append(f"Claude Code  keeps chats {current} days")
        elif ask and ask(
            f"Claude Code deletes old chats (cleanupPeriodDays is {was}). "
            f"Keep them {CLAUDE_KEEP_DAYS // 365} years instead (cleanupPeriodDays = {CLAUDE_KEEP_DAYS})?"
        ):
            report.append(f"Claude Code  keeps chats {CLAUDE_KEEP_DAYS} days, was {was}: {_keep_claude(dry_run)}")
        else:
            report.append(
                f"Claude Code  deletes old chats (cleanupPeriodDays is {was}; recall keeps its own copy of each "
                f"chat it indexed); `recall install --only retention` raises it to {CLAUDE_KEEP_DAYS}"
            )
    if os.path.isdir(os.path.dirname(CODEX_CONFIG)):
        report.append("Codex        no setting deletes old sessions")
    if os.path.isdir(os.path.dirname(CURSOR_MCP)):
        report.append("Cursor       no setting deletes old chats")
    return report


def ask_terminal(question: str) -> bool:
    """A yes/no question when a person is at the terminal; no one there means no."""
    return sys.stdin.isatty() and input(f"{question} (y/n) [n]: ").strip().lower() in ("y", "yes")


def _keep_claude(dry_run: bool) -> str:
    try:
        config = json.loads(_read(CLAUDE_SETTINGS) or "{}")
    except json.JSONDecodeError:
        return f"{CLAUDE_SETTINGS} is not valid JSON, skipped"
    config["cleanupPeriodDays"] = CLAUDE_KEEP_DAYS
    if not dry_run:
        _write(CLAUDE_SETTINGS, json.dumps(config, indent=2))
    return f"set in {CLAUDE_SETTINGS}, backed up first"


# --- helpers ------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


_BACKED_UP: set[str] = set()


def _backup(path: str) -> None:
    """Copy a file to <file>.recall-backup before recall (or an agent CLI it runs) changes it. Once per
    file per run, so the backup is the file as it was before the run's first change."""
    if path not in _BACKED_UP and os.path.exists(path):
        shutil.copy2(path, path + ".recall-backup")
    _BACKED_UP.add(path)


def _write(path: str, text: str) -> None:
    _backup(path)
    with open(path, "w") as f:
        f.write(text)


def _recall_bin() -> str:
    """The recall to put in hooks and servers: $RECALL_BIN when set (a background run started by a
    hook passes its own), else the recall running now, else the one on PATH."""
    found = os.environ.get("RECALL_BIN")
    if not found and os.path.basename(sys.argv[0]) == NAME and os.access(sys.argv[0], os.X_OK):
        found = sys.argv[0]
    found = found or shutil.which("recall")
    if not found:
        sys.exit("recall is not on PATH: install it with `uv tool install .` first")
    return os.path.realpath(found)
