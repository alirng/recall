"""Connect recall to coding agents: its MCP server, the /recall skill, the session-start
hooks that load memory in Claude Code and Codex, and the edit hooks that apply your
coordination rules when another chat has open changes to a file an agent is about to edit.

Every config file is backed up beside itself (<file>.recall-backup) before it
changes, and `recall uninstall` reverses each step. A run that changes a file twice
keeps the backup of its first state.
"""

import contextlib
import glob
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass

from . import hooks as edit_hooks

NAME = "recall"
COMPONENTS = ("mcp", "skill", "hook")

SKILL_SOURCE = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
SHARED_SKILLS = os.path.expanduser("~/.agents/skills")  # one copy, linked into each agent
SKILL_DIRS = {"Claude Code": "~/.claude/skills", "Codex": "~/.codex/skills", "Cursor": "~/.cursor/skills"}
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")  # the default root's, where retention is set
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
CURSOR_MCP = os.path.expanduser("~/.cursor/mcp.json")
GEMINI_SETTINGS = os.path.expanduser("~/.gemini/settings.json")
OPENCODE_PLUGIN = os.path.expanduser("~/.config/opencode/plugins/recall.js")
OPENCODE_SOURCE = os.path.join(os.path.dirname(__file__), "opencode", "recall.js")
# recall's table in Codex's config.toml, and any table nested under it (Codex may add its own)
_CODEX_BLOCK = re.compile(rf"\n?\[mcp_servers\.{NAME}(?:\.[^\]\n]*)?\]\n(?:(?!\[).*\n?)*")
# Codex's `auto` approval runs a tool without asking when it is marked read-only, or marked neither
# destructive nor open-world, which message_chat is. So message_chat is set to prompt on its own. The
# dotted key sits inside recall's table, so uninstalling removes it with the table.
_CODEX_ASK = 'tools.message_chat.approval_mode = "prompt"'


def install(
    components=COMPONENTS,
    dry_run: bool = False,
    roots: list | None = None,
    repoint: bool = False,
    others: bool = True,
) -> list[str]:
    """Connect every root recall found (roots.discover()), or just `roots`. A root the person turned
    off ([roots.NAME] connect = false) is left alone. Hooks that run another existing recall stay on it
    and are reported; repoint=True (`recall install --repoint`) points them at this one. others: also the
    agents beyond the three (TARGETS), which are not roots and so come with a whole-machine install only."""
    cmd = _recall_bin()
    report = _roots(components, cmd, dry_run, roots, repoint=repoint)
    if others:
        report += _others(components, cmd, dry_run)
    if "hook" in components:
        report.append(f"Gemini CLI   {_hook_gemini(cmd, dry_run, repoint=repoint)}")
        report.append(f"OpenCode     {_hook_opencode(cmd, dry_run)}")
    return report


def uninstall(components=COMPONENTS, roots: list | None = None, whole: bool | None = None) -> list[str]:
    """Remove recall from every root recall found, or just `roots`. A whole-machine uninstall (the
    default when no roots are named) also removes the shared copy of the skill and recall's entries in
    Gemini CLI and OpenCode."""
    whole = roots is None if whole is None else whole
    report = _roots(components, "", False, roots, remove=True)
    if whole:
        report += _others(components, "", False, remove=True)
        if "skill" in components:
            _unshare_skill()
        if "hook" in components:
            report.append(f"Gemini CLI   {_hook_gemini('', remove=True)}")
            report.append(f"OpenCode     {_hook_opencode('', remove=True)}")
    return report


def _roots(
    components, cmd: str, dry_run: bool, roots: list | None, remove: bool = False, repoint: bool = False
) -> list[str]:
    from .. import config
    from .. import roots as harness

    found = harness.discover() if roots is None else roots
    cfg = config.load()
    report = []
    for agent, label in (("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor")):
        mine = [r for r in connectable(found, remove) if r.agent == agent]
        if not mine:
            report.append(f"{label:12} not found, skipped")
        for r in mine:
            if not remove and not harness.option(r, "connect", cfg):
                report.append(f"{r.label:12} skipped: [roots.{r.name}] connect = false")
                continue
            report += connect_root(r, cmd, components, dry_run, remove, repoint=repoint)
    return report


def connectable(roots: list, removal: bool = False) -> list:
    """The roots recall may write to: ones the agent has used (Root.exists), one per place recall
    writes to (Cursor's builds share ~/.cursor), and for Cursor only when ~/.cursor is there. To
    remove recall, any folder that is still there will do."""
    out, seen = [], set()
    for r in roots:
        key = (r.agent, os.path.realpath(r.path), r.by_env)
        if (r.exists or removal) and os.path.isdir(r.path) and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def connect_root(
    root,
    cmd: str,
    components=COMPONENTS,
    dry_run: bool = False,
    remove: bool = False,
    allow: set | None = None,
    repoint: bool = False,
) -> list[str]:
    """Install (or remove) recall's MCP server, skill and hooks in one root. Each config file is
    backed up beside itself first; a part that is already in place is left alone. allow: the hook
    kinds (hooks.consent_key) this call may add, repair or leave alone; None means all of them.
    repoint: also move hooks that run another existing recall onto `cmd`. Only a person's explicit
    `recall install --repoint` sets it; autopilot never does."""
    f = root_files(root)
    lead = f"{root.label:12}"
    out = []
    if "mcp" in components:
        mcp = {"claude": _mcp_claude, "codex": _mcp_codex, "cursor": _mcp_cursor}[root.agent]
        out.append(f"{lead} MCP: {mcp(cmd, dry_run, remove, root)}")
    if "skill" in components:
        out += _skill(dry_run, remove, {root.label: f["skills"]})
    if "hook" in components:
        out.append(f"{lead} {_hook_agent(root.agent, f['hook'], cmd, dry_run, remove, allow, repoint)}")
    return out


def root_files(root) -> dict[str, str]:
    """The three files recall touches in a root: where its MCP server is listed, its hooks, its skills.
    (A Claude root's MCP server may be listed in more than one file: see roots.claude_global_files.)"""
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


def parts(root, cmd: str | None = None, allow: set | None = None) -> dict[str, bool | None]:
    """Which parts of recall are in place in this root: mcp, hook, skill. Reads files only and
    never runs an agent, so it is cheap enough for a hook. None: cannot tell (recall not on PATH).
    allow: the hook kinds that count (None: all). Codex runs its hooks only after a one-time trust
    in /hooks, and Cursor loads an MCP server only after an approval in each project; recall can
    see neither, so a part in place is "written", not "loaded"."""
    f = root_files(root)
    if cmd is None:
        try:
            cmd = _recall_bin()
        except SystemExit:
            cmd = None
    hook = None
    if cmd:
        hook = not _hook_gaps(root, cmd, allow)
    link = os.path.join(f["skills"], NAME)
    return {"mcp": _mcp_registered(root), "hook": hook, "skill": os.path.exists(link)}


def _mcp_registered(root) -> bool:
    from .. import roots as harness

    if root.agent == "codex":
        return f"[mcp_servers.{NAME}]" in _read(root_files(root)["mcp"])
    files = harness.claude_global_files(root) if root.agent == "claude" else [root_files(root)["mcp"]]
    return all(_in_json_servers(f) for f in files)


def _in_json_servers(path: str) -> bool:
    try:
        return NAME in (json.loads(_read(path) or "{}").get("mcpServers") or {})
    except (json.JSONDecodeError, AttributeError):
        return False


def cursor_approval(root) -> dict[str, int]:
    """Cursor loads an MCP server from mcp.json only after the person approves it, once per project:
    ~/.cursor/projects/<slug>/mcp-approvals.json is a JSON list of "<server>-<hash of its entry>".
    How many projects hold an approval for recall (any entry; Cursor asks again when the entry changes).
    Read only, and only for `recall doctor`."""
    approved = total = 0
    for f in glob.glob(os.path.join(root.path, "projects", "*", "mcp-approvals.json")):
        total += 1
        try:
            with open(f, encoding="utf-8") as fh:
                names = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(names, list) and any(str(n).startswith(NAME + "-") for n in names):
            approved += 1
    return {"approved": approved, "projects": total}


# --- MCP server ---------------------------------------------------------------


def _mcp_claude(cmd: str, dry_run: bool = False, remove: bool = False, root=None) -> str:
    """Claude Code's own `mcp add`, run with the root's CLAUDE_CONFIG_DIR (and without one for the
    default root, even when this process was started from inside another root's session). One call per
    file the root's servers can be listed in (roots.claude_global_files)."""
    from .. import roots as harness

    if not shutil.which("claude"):
        return "Claude Code not found, skipped"
    root = root or _default("claude")
    own = os.path.join(root.path, ".claude.json")
    done, failures = [], []
    for where in harness.claude_global_files(root):
        registered = _in_json_servers(where)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        if where == own:  # launched with the variable
            env["CLAUDE_CONFIG_DIR"] = root.path
        if remove:
            if registered and not dry_run:
                _backup_claude(root, where)
                _run(["claude", "mcp", "remove", NAME, "-s", "user"], env)
            done.append("removed" if registered else "not registered")
        elif registered:
            done.append("already registered")
        elif dry_run:
            done.append("added")
        else:
            _backup_claude(root, where)
            failed = _run(["claude", "mcp", "add", "-s", "user", NAME, "--", cmd, "serve"], env)
            if failed:
                failures.append(failed)
            done.append("added")
    if failures:
        return f"could not add: {failures[0]}"
    if remove:
        return "removed" if "removed" in done else "not registered"
    return f"added ({cmd} serve)" if "added" in done else "already registered"


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
        return _codex_ask_first(config, text, dry_run)
    if not dry_run:
        block = (
            f'\n[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd)}\nargs = ["serve"]\n'
            f'default_tools_approval_mode = "auto"\n{_CODEX_ASK}\n'
        )
        _write(config, text + block)
    return f"added to {config}; Codex asks before message_chat runs"


def _codex_unasked(text: str) -> bool | None:
    """Whether recall's entry in this Codex config lets message_chat run without asking, and
    `recall install` would change that. An entry that already says how message_chat is approved, or
    whose server-wide mode the person changed, is the person's choice. None: the config does not parse."""
    try:
        server = tomllib.loads(text).get("mcp_servers", {}).get(NAME)
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(server, dict):
        return False
    tools = server.get("tools")
    tool = tools.get("message_chat") if isinstance(tools, dict) else None
    chosen = isinstance(tool, dict) and "approval_mode" in tool
    return not chosen and server.get("default_tools_approval_mode") in (None, "auto")


def codex_unasked(root) -> bool:
    """For `recall doctor`: a Codex root whose recall entry, written by an earlier release, lets
    message_chat run without asking. Reads only."""
    return root.agent == "codex" and _codex_unasked(_read(root_files(root)["mcp"])) is True


def _codex_ask_first(config: str, text: str, dry_run: bool) -> str:
    """An entry an earlier release wrote lets Codex run message_chat without asking: add the approval
    to it, and leave an entry that is the person's choice as it is."""
    unasked = _codex_unasked(text)
    if unasked is None:
        return f"already registered ({config} is not valid TOML, so message_chat's approval was not checked)"
    if not unasked:
        return "already registered"
    block = re.search(rf"^\[mcp_servers\.{NAME}\][^\n]*\n(?:(?!\[).*\n?)*", text, re.M)
    fixed = text
    if block:  # after the block's last line, before any blank lines that follow it
        body = block[0].rstrip("\n")
        fixed = text[: block.start()] + f"{body}\n{_CODEX_ASK}" + (block[0][len(body) :] or "\n") + text[block.end() :]
    try:
        ok = fixed != text and "message_chat" in tomllib.loads(fixed)["mcp_servers"][NAME]["tools"]
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        return f"already registered; add `{_CODEX_ASK}` under [mcp_servers.{NAME}] in {config} so Codex asks first"
    if not dry_run:
        _write(config, fixed)
    return f"{'would update' if dry_run else 'updated'} {config}: Codex now asks before message_chat runs"


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
    or `folders` (label -> skills folder; created when its agent folder is there). An edited copy of
    SKILL.md is backed up before it is replaced."""
    shared = os.path.join(SHARED_SKILLS, NAME)
    report = []
    for agent, folder in (folders or SKILL_DIRS).items():
        folder = os.path.expanduser(folder)
        link = os.path.join(folder, NAME)
        if remove:
            if os.path.islink(link) and not dry_run:
                os.remove(link)
            report.append(f"{agent:12} skill unlinked")
        elif os.path.isdir(os.path.dirname(folder)) if folders else os.path.isdir(folder):
            if not os.path.lexists(link) and not dry_run:
                os.makedirs(folder, exist_ok=True)
                os.symlink(os.path.relpath(shared, folder), link)
            report.append(f"{agent:12} skill linked at {link}")
    target = os.path.join(shared, "SKILL.md")
    if not remove and not dry_run and _read(target) != _read(SKILL_SOURCE):
        os.makedirs(shared, exist_ok=True)
        _write(target, _read(SKILL_SOURCE))
    return report


def _unshare_skill() -> None:
    shared = os.path.join(SHARED_SKILLS, NAME)
    if os.path.isdir(shared):
        target = os.path.join(shared, "SKILL.md")
        if os.path.exists(target) and _read(target) != _read(SKILL_SOURCE):  # edited, or an older release's
            _backup(target, dest=os.path.join(os.path.dirname(shared), "recall.SKILL.md"))
        shutil.rmtree(shared)


# --- MCP server in the agents beyond Claude Code, Codex and Cursor -------------


@dataclass(frozen=True)
class Target:
    """An agent whose MCP config recall edits. The file's format is the agent's own, documented (docs/agents.md
    cites it for each). `used`: paths that exist once the agent has run, so recall never writes into the
    folder of an agent that is not there. `configs`: the files to list the server in, with the roots.expand
    placeholders; `first` takes only the first that exists (OpenCode accepts .json and .jsonc)."""

    key: str
    label: str
    used: tuple[str, ...]
    configs: tuple[str, ...]
    shape: str = "mcpServers"  # how the entry is written, see _server_entry
    servers: str = "mcpServers"  # the key the entries sit under
    first: bool = False
    binary: str | None = None  # a command whose presence also shows the agent is there
    skills: str | None = None  # the folder of an agent that does not read ~/.agents/skills, to link the skill into


TARGETS = (
    Target("gemini", "Gemini CLI", ("~/.gemini/settings.json",), ("~/.gemini/settings.json",), binary="gemini"),
    Target(
        "antigravity",
        "Antigravity",
        ("~/.gemini/antigravity", "~/.gemini/antigravity-ide", "~/.gemini/antigravity-cli"),
        ("~/.gemini/config/mcp_config.json", "~/.gemini/antigravity/mcp_config.json"),
        skills="~/.gemini/config/skills",
    ),
    Target("qwen", "Qwen Code", ("~/.qwen",), ("~/.qwen/settings.json",)),
    Target(
        "opencode",
        "OpenCode",
        ("{config}/opencode", "{data}/opencode"),
        ("{config}/opencode/opencode.json", "{config}/opencode/opencode.jsonc"),
        shape="opencode",
        servers="mcp",
        first=True,
    ),
    Target(
        "kilo",
        "Kilo Code",
        ("{config}/kilo", "{data}/kilo"),
        ("{config}/kilo/kilo.json", "{config}/kilo/kilo.jsonc"),
        shape="opencode",
        servers="mcp",
        first=True,
    ),
    Target(
        "crush",
        "Crush",
        ("{config}/crush", "{data}/crush"),
        ("{config}/crush/crush.json",),
        shape="crush",
        servers="mcp",
    ),
    Target("copilot", "Copilot CLI", ("~/.copilot",), ("~/.copilot/mcp-config.json",), shape="copilot"),
    Target("droid", "Factory Droid", ("~/.factory",), ("~/.factory/mcp.json",), shape="stdio"),
    Target("kimi", "Kimi Code", ("~/.kimi-code",), ("~/.kimi-code/mcp.json",), shape="stdio"),
    Target("kiro", "Kiro", ("~/.kiro",), ("~/.kiro/settings/mcp.json",), skills="~/.kiro/skills"),
    Target("cline", "Cline", ("~/.cline",), ("~/.cline/data/settings/cline_mcp_settings.json",)),
    Target("devin", "Devin CLI", ("{config}/devin",), ("{config}/devin/mcp_config.json",)),
    Target("windsurf", "Windsurf", ("~/.codeium/windsurf",), ("~/.codeium/windsurf/mcp_config.json",)),
    Target("warp", "Warp", ("~/.warp",), ("~/.warp/.mcp.json",)),
    Target(
        "vscode",
        "VS Code",
        ("{support}/Code/User",),
        ("{support}/Code/User/mcp.json",),
        shape="stdio",
        servers="servers",
    ),
    Target("amp", "Amp", ("{config}/amp",), ("{config}/amp/settings.json",), servers="amp.mcpServers"),
    Target("junie", "Junie", ("~/.junie",), ("~/.junie/mcp/mcp.json",)),
)
# Grok Build lists its servers as TOML tables, like Codex.
GROK_CONFIG = "~/.grok/config.toml"


def _server_entry(shape: str, cmd: str) -> dict:
    if shape == "opencode":
        return {"type": "local", "command": [cmd, "serve"], "enabled": True}
    if shape == "crush":
        return {"type": "stdio", "command": cmd, "args": ["serve"]}
    if shape == "copilot":
        return {"type": "local", "command": cmd, "args": ["serve"], "tools": ["*"]}
    if shape == "stdio":
        return {"type": "stdio", "command": cmd, "args": ["serve"]}
    return {"command": cmd, "args": ["serve"]}


def _others(components, cmd: str, dry_run: bool, remove: bool = False) -> list[str]:
    """Register (or remove) recall's MCP server, and link the skill where an agent needs its own copy,
    in every agent of TARGETS that has been used. `[roots.<key>] connect = false` in config.toml leaves one
    alone. Autopilot does not touch these: only `recall install` does."""
    from .. import config, roots

    cfg = config.load()
    report, skills = [], {}
    for t in TARGETS:
        lead = f"{t.label:13}"
        table = (cfg.get("roots") or {}).get(t.key)
        if not remove and isinstance(table, dict) and table.get("connect") is False:
            report.append(f"{lead} skipped: [roots.{t.key}] connect = false")
            continue
        paths = [p for p in map(roots.expand, t.used) if p]
        if not (any(os.path.exists(p) for p in paths) or (t.binary and shutil.which(t.binary))):
            report.append(f"{lead} not found, skipped")
            continue
        if "mcp" in components:
            report.append(f"{lead} MCP: {_mcp_json(t, cmd, dry_run, remove)}")
        if t.skills and "skill" in components:
            skills[t.label] = t.skills
    if "mcp" in components:
        report.append(f"{'Grok Build':12} MCP: {_mcp_grok(cmd, dry_run, remove)}")
    if skills:
        report += _skill(dry_run, remove, skills)
    return report


def _configs(t: Target) -> list[str]:
    from .. import roots

    files = [p for p in map(roots.expand, t.configs) if p]
    if t.first:
        return [next((f for f in files if os.path.exists(f)), files[0])]
    return files


def _mcp_json(t: Target, cmd: str, dry_run: bool, remove: bool) -> str:
    done = []
    for path in _configs(t):
        existed = os.path.exists(path)
        try:
            config = json.loads(_read(path) or "{}")
        except json.JSONDecodeError:  # comments (JSONC) or damage: recall does not rewrite what it cannot read back
            done.append(f"{path} is not plain JSON, skipped: add the server by hand")
            continue
        if not isinstance(config, dict):
            done.append(f"{path} is not a JSON object, skipped")
            continue
        servers = config.setdefault(t.servers, {})
        if not isinstance(servers, dict):
            done.append(f"{path}: `{t.servers}` is not an object, skipped")
            continue
        registered = NAME in servers
        if remove:
            if registered and not dry_run:
                del servers[NAME]
                if not servers:
                    del config[t.servers]
                _unlisted(path, config)
            done.append("removed" if registered else "not registered")
        elif registered:
            done.append("already registered")
        else:
            if not dry_run:
                servers[NAME] = _server_entry(t.shape, cmd)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                _write(path, json.dumps(config, indent=2) + "\n")
            done.append(f"added to {path}" + ("" if existed else " (new file)"))
    return "; ".join(done)


def _unlisted(path: str, config: dict) -> None:
    """Write a config file back after recall's entry is taken out. When nothing else is left the file is put
    as it was before recall first wrote it: removed if recall made it, else the original (an empty file, or
    an empty `mcpServers` table)."""
    if any(v not in ({}, [], None) for v in config.values()):
        _write(path, json.dumps(config, indent=2) + "\n")
        return
    backup = path + ".recall-backup"
    if not os.path.exists(backup):
        _backup(path)  # recall created this file: nothing of the person's is lost by removing it
        os.remove(path)
        return
    _write(path, _read(backup))


def _mcp_grok(cmd: str, dry_run: bool, remove: bool) -> str:
    from .. import roots

    path = roots.expand(GROK_CONFIG)
    if not os.path.isdir(os.path.dirname(path)):
        return "not found, skipped"
    text = _read(path)
    registered = f"[mcp_servers.{NAME}]" in text
    if remove:
        if registered and not dry_run:
            rest = _CODEX_BLOCK.sub("", text).rstrip("\n")
            if rest or os.path.exists(path + ".recall-backup"):
                _write(path, rest + "\n" if rest else _read(path + ".recall-backup"))
            else:  # recall made the file
                _backup(path)
                os.remove(path)
        return "removed" if registered else "not registered"
    if registered:
        return "already registered"
    if not dry_run:
        _write(path, text + f'\n[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd)}\nargs = ["serve"]\n')
    return f"added to {path}"


# --- hooks ----------------------------------------------------------------------


def _entries(agent: str, cmd: str) -> list[tuple[str, dict]]:
    """recall's entries for one agent, from hooks.INSTALL: every one runs `recall hook`."""
    command = f"{shlex.quote(cmd)} hook --agent {agent}"  # agents run it through a shell
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


def hook_kinds(agent: str) -> list[str]:
    """The hook kinds (hooks.consent_key) this release installs for an agent."""
    return list(dict.fromkeys(edit_hooks.consent_key(agent, ev, e.get("matcher")) for ev, e in _entries(agent, "x")))


def _hook_agent(
    agent: str, path: str, cmd: str, dry_run: bool, remove: bool, allow: set | None = None, repoint: bool = False
) -> str:
    """Claude Code reads SessionStart output into the session's context, so new, resumed and compacted
    sessions all start with the memory block. Codex runs its hooks from hooks.json once you have trusted
    them in /hooks; Cursor reloads hooks.json on save."""
    label = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}[agent]
    if not os.path.isdir(os.path.dirname(path)):
        return f"{label} not found, skipped"
    report = _hook_file(path, agent, cmd, dry_run, remove, agent == "cursor", allow, repoint)
    if agent == "codex" and " written " in report:
        report += f"; trust them {'again' if 'repointed' in report else 'once'} with /hooks in Codex"
    return report


def _hook_gemini(cmd: str, dry_run: bool = False, remove: bool = False, repoint: bool = False) -> str:
    if not (shutil.which("gemini") or os.path.exists(GEMINI_SETTINGS)):
        return "Gemini CLI not found, skipped"
    return _hook_file(GEMINI_SETTINGS, "gemini", cmd, dry_run, remove, repoint=repoint)


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
        plugin = f.read().replace('"__RECALL__"', json.dumps(cmd))
    if installed and _read(OPENCODE_PLUGIN) == plugin:
        return "edit plugin already installed"
    if not dry_run:
        os.makedirs(os.path.dirname(OPENCODE_PLUGIN), exist_ok=True)
        _write(OPENCODE_PLUGIN, plugin)
    return f"edit plugin added at {OPENCODE_PLUGIN}"


def _hook_file(
    path: str,
    agent: str,
    cmd: str,
    dry_run: bool,
    remove: bool,
    flat: bool = False,
    allow: set | None = None,
    repoint: bool = False,
) -> str:
    """Make recall's entries in one agent's hooks file what this release installs, keyed by (event,
    matcher): add the missing, leave a working one exactly as it is, replace one whose recall is gone
    or that is an older kind of entry, leave everyone else's alone. One write, so the backup holds the
    file as it was. Claude Code, Codex and Gemini CLI wrap commands in groups under each event; Cursor
    lists them directly (flat). allow: only these hook kinds are managed (None: all). A hook that runs
    another existing recall is left on it and named in the report, unless repoint is set."""
    try:
        config = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return f"{path} is not valid JSON, skipped"
    elsewhere = [] if remove else _runners(config, agent, cmd, flat)
    changed = _reconcile(config, agent, cmd, remove, flat, allow, repoint)
    if flat and changed:
        config.setdefault("version", 1)
    if changed and not dry_run:
        _write(path, json.dumps(config, indent=2))
    events = ", ".join(dict.fromkeys(k.split(":")[1] for k in changed))
    if remove:
        return f"hooks removed ({events})" if changed else "no hooks installed"
    left = [] if repoint else elsewhere
    note = f"hooks run {', '.join(left)}, not this recall ({cmd}); `recall install --repoint` points them here"
    if not changed:
        return note if left else "hooks already installed"
    text = f"hooks written to {path} ({events})"
    if repoint and elsewhere:
        text += f"; repointed from {', '.join(elsewhere)} to {cmd}"
    return f"{text}; {note}" if left else text


def _hook_gaps(root, cmd: str, allow: set | None = None) -> list[str]:
    """The hook kinds in `allow` (None: all) that are missing from this root or point at a recall that is
    gone; [] when the root is as this release would leave it. Reads only."""
    path = root_files(root)["hook"]
    try:
        config = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return ["unreadable"]
    return _reconcile(config, root.agent, cmd, False, root.agent == "cursor", allow)


def _reconcile(
    config: dict, agent: str, cmd: str, remove: bool, flat: bool, allow: set | None, repoint: bool = False
) -> list[str]:
    """Edit `config` in place; returns the hook kinds that changed (or would). A present entry is
    judged by its command, not by who wrote it: if it runs an existing `recall hook --agent <agent>`
    it stays where it is (Codex ties its trust to the entry, so reordering or rewriting one costs the
    person a re-trust), and only a field that differs is refreshed. It is repointed to `cmd` only when
    the recall it runs is gone, so a recall run from a repo never takes hooks over from the installed
    one; or when `repoint` is set, which only a person's explicit `recall install --repoint` does
    (an entry that already runs `cmd` through a symlink is not rewritten)."""
    wanted = {(ev, en.get("matcher")): en for ev, en in _entries(agent, cmd)}

    def kind(event, matcher):
        return edit_hooks.consent_key(agent, event, matcher)

    def managed(event, matcher):
        return allow is None or kind(event, matcher) in allow

    def ours(e: dict) -> bool:
        return _is_ours(e) if flat else any(_is_ours(h) for h in e.get("hooks", []))

    hooks = config.setdefault("hooks", {})
    changed, satisfied = [], set()
    for event in list(hooks):
        keep = []
        for e in hooks[event]:
            key = (event, e.get("matcher")) if isinstance(e, dict) else None
            if key is None or not ours(e):
                keep.append(e)
            elif remove:
                changed.append(kind(*key))
            elif key not in wanted:  # a kind this release no longer installs, or the older context/status form
                if allow is None:
                    changed.append(kind(*key))
                else:
                    keep.append(e)
            elif not managed(*key):
                keep.append(e)
            elif key in satisfied:  # the same entry twice
                changed.append(kind(*key))
            else:
                satisfied.add(key)
                exe = _runs(e, agent, flat)
                stays = exe and _exists(exe) and (not repoint or _same(exe, cmd))
                target = _rebase(wanted[key], shlex.quote(cmd), shlex.quote(exe)) if stays else wanted[key]
                if e != target:
                    changed.append(kind(*key))
                keep.append(target if e != target else e)
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if not remove:
        for key, entry in wanted.items():
            if key not in satisfied and managed(*key):
                hooks.setdefault(key[0], []).append(entry)
                changed.append(kind(*key))
    return list(dict.fromkeys(changed))


def _runs(entry: dict, agent: str, flat: bool) -> str | None:
    """The executable a hook entry runs, if it is `<recall> hook --agent <agent>`."""
    for h in [entry] if flat else entry.get("hooks", []):
        words = _argv(h.get("command", ""))
        if len(words) >= 4 and os.path.basename(words[0]) == NAME and words[1:3] == ["hook", "--agent"]:
            return words[0] if words[3] == agent else None
    return None


def _argv(command: str) -> list[str]:
    """A hook command's words. Releases up to 0.6.0 wrote recall's path unquoted, so a path with a space in
    it splits in the wrong place (and fails in the agent's shell): that form is read by where it ends, when
    the path it gives is there or its first word is no program at all, so someone's own wrapper around
    recall stays theirs."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if not words or os.path.basename(words[0]) == NAME:
        return words
    head, found, rest = command.rpartition(f"{os.sep}{NAME} ")
    exe = head + os.sep + NAME
    if found and os.path.isabs(head) and (os.path.exists(exe) or not _exists(words[0])):
        with contextlib.suppress(ValueError):
            return [exe, *shlex.split(rest)]
    return words


def _exists(exe: str) -> bool:
    return os.access(exe, os.X_OK) if os.path.isabs(exe) else shutil.which(exe) is not None


def _same(exe: str, cmd: str) -> bool:
    """Whether a hook's executable is the recall at `cmd`, through any symlink."""
    found = exe if os.path.isabs(exe) else shutil.which(exe)
    return bool(found) and os.path.realpath(found) == os.path.realpath(cmd)


def _runners(config: dict, agent: str, cmd: str, flat: bool) -> list[str]:
    """The other recalls this config's recall hooks run: existing ones that are not `cmd`. A recall
    that is gone is not listed: that hook is a gap, and gets repointed without being asked."""
    out = []
    hooks = config.get("hooks")
    for entries in hooks.values() if isinstance(hooks, dict) else ():
        for e in entries if isinstance(entries, list) else ():
            exe = _runs(e, agent, flat) if isinstance(e, dict) else None
            if exe and _exists(exe) and not _same(exe, cmd):
                out.append(exe if os.path.isabs(exe) else shutil.which(exe))
    return list(dict.fromkeys(out))


def other_runners(root, cmd: str) -> list[str]:
    """The recalls other than `cmd` that this root's hooks run, for `recall doctor`. Reads only."""
    try:
        config = json.loads(_read(root_files(root)["hook"]) or "{}")
    except json.JSONDecodeError:
        return []
    return _runners(config, root.agent, cmd, root.agent == "cursor")


def _rebase(entry: dict, old: str, new: str):
    """The entry with the recall it runs swapped for another."""
    if isinstance(entry, dict):
        return {k: _rebase(v, old, new) for k, v in entry.items()}
    if isinstance(entry, list):
        return [_rebase(v, old, new) for v in entry]
    if isinstance(entry, str) and entry.startswith(old + " "):
        return new + entry[len(old) :]
    return entry


def _is_ours(hook: dict) -> bool:
    """recall's own entries: its executable running hook, or the older context/status --hook."""
    words = _argv(hook.get("command", ""))
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


def _backup(path: str, dest: str | None = None) -> None:
    """Copy a file aside before recall (or an agent CLI it runs) changes it, once per file per run.
    The first copy, <file>.recall-backup, is never overwritten: it is the file as it was before recall
    first touched it. Later runs keep theirs as <file>.<time>.recall-backup, unless the file still
    equals the first. dest: name the copies after another path (default: the file itself)."""
    if path in _BACKED_UP:
        return
    _BACKED_UP.add(path)
    if not os.path.exists(path):
        return
    base = dest or path
    first = base + ".recall-backup"
    if not os.path.exists(first):
        shutil.copy2(path, first)
        return
    with open(path, "rb") as a, open(first, "rb") as b:
        if a.read() == b.read():
            return
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
    later = f"{base}.{stamp}.recall-backup"
    while os.path.exists(later):
        n += 1
        later = f"{base}.{stamp}-{n}.recall-backup"
    shutil.copy2(path, later)


def _write(path: str, text: str) -> None:
    """Back the file up, then replace it in one step (a temp file beside it, then rename), so an agent
    that reads the file mid-write never sees half of it. A symlinked dotfile keeps its link and its mode."""
    _backup(path)
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=f".{os.path.basename(real)}.", suffix=".recall-tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
