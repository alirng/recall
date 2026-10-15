"""Harness roots: every place a coding agent keeps its settings and chats on this machine.

One person can run the same agent against several folders: Claude Code with a second
CLAUDE_CONFIG_DIR for a work account, Codex with another CODEX_HOME, Cursor next to Cursor
Nightly. Each folder is a root with a stable name (claude, claude-work, codex, cursor,
cursor-nightly). discover() finds them from the defaults, the environment, the shell's rc
files (aliases and exports) and folders named like a config dir. It only reads, and from the
rc files it keeps just the two variables' values; nothing else in them is stored or shown.

What each agent does with its root (live-checked in October 2026 unless marked):
  claude  CLAUDE_CONFIG_DIR holds settings.json, skills/ and projects/. Without the variable the
          user-scope MCP servers sit in ~/.claude.json; with it, in <dir>/.claude.json.
  codex   CODEX_HOME holds config.toml (MCP servers), hooks.json, skills/ and sessions/.
  cursor  MCP servers (~/.cursor/mcp.json), hooks, skills and agent transcripts (~/.cursor/projects)
          are one set for every Cursor build: the app hard-codes ".cursor" (read in the stable
          app; a variant such as Nightly is assumed to do the same). A variant has its own
          Application Support/<Name>/User folder with its own state.vscdb, so its chats are
          the only thing that differs.
"""

import contextlib
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass

AGENTS = ("claude", "codex", "cursor")
ENV_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
# A folder named ~/.claude-work counts as a config dir only if it holds what the agent itself writes
# once used (other tools keep a settings.json in folders with similar names, so that alone is not enough).
MARKERS = {
    "claude": ("projects", ".claude.json", "history.jsonl"),
    "codex": ("sessions", "auth.json", "archived_sessions"),
}
RC_FILES = (
    ".zshenv",
    ".zprofile",
    ".zshrc",
    ".zlogin",
    ".bash_profile",
    ".bash_login",
    ".bashrc",
    ".profile",
    ".aliases",
    ".exports",
    ".config/fish/config.fish",
)
MAX_RC_BYTES = 512_000
_ASSIGN = re.compile(r"(?<![A-Za-z0-9_])(CLAUDE_CONFIG_DIR|CODEX_HOME)=(?:\"([^\"]*)\"|'([^']*)'|([^\s;&|)\"']+))")
_FISH_SET = re.compile(r"^\s*set\s+(?:-\w+\s+)*(CLAUDE_CONFIG_DIR|CODEX_HOME)\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
_ALIAS = re.compile(r"^\s*alias\s+([\w.-]+)=")
_SOURCE = re.compile(r"^\s*(?:source|\.)\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))")


@dataclass(frozen=True)
class Root:
    agent: str  # claude, codex or cursor
    name: str  # stable and unique: claude, claude-work, codex, cursor, cursor-nightly
    path: str  # claude, codex: the config dir; cursor: ~/.cursor, shared by every build
    data: str | None = None  # cursor: this build's Application Support/<Name>/User folder
    via: str = "default"  # default, env, shell (an alias or export), folder or app
    exists: bool = True  # False: named in a shell rc but not created yet (first launch makes it)
    default: bool = False  # the folder the agent uses when nothing is set
    by_env: bool = False  # the agent must be started with the variable set to use this folder

    @property
    def label(self) -> str:
        return {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}[self.agent] + (
            "" if self.default else f" ({self.name})"
        )


def home() -> str:
    return os.path.expanduser("~")


def default_dir(agent: str) -> str:
    return os.path.join(home(), {"claude": ".claude", "codex": ".codex", "cursor": ".cursor"}[agent])


def discover() -> list[Root]:
    """Every root, defaults first. Cheap: a few stats, the rc files and two directory listings."""
    found: dict[tuple[str, str], dict] = {}

    def add(agent: str, path: str, via: str, **extra) -> None:
        path = os.path.abspath(path)
        key = (agent, os.path.realpath(path))
        if key in found:
            found[key]["by_env"] = found[key]["by_env"] or extra.get("by_env", False)
            return
        found[key] = {"agent": agent, "path": path, "via": via, "exists": os.path.isdir(path), **extra}

    for agent in ("claude", "codex"):
        d = default_dir(agent)
        if os.path.isdir(d) or (agent == "claude" and os.path.exists(os.path.join(home(), ".claude.json"))):
            add(agent, d, "default", default=True)
        env = os.environ.get(ENV_VARS[agent])
        if env:
            add(agent, _expand(env) or env, "env", by_env=True)
    for agent, path, via in _declared():
        add(agent, path, via, by_env=True)
    for agent in ("claude", "codex"):
        for path in sorted(
            glob.glob(os.path.join(home(), f".{agent}-*")) + glob.glob(os.path.join(home(), f".{agent}_*"))
        ):
            if os.path.isdir(path) and any(os.path.exists(os.path.join(path, m)) for m in MARKERS[agent]):
                add(agent, path, "folder", by_env=True)
    cursor = default_dir("cursor")
    for variant in _cursor_variants():
        found[("cursor", variant)] = {
            "agent": "cursor",
            "path": cursor,
            "data": variant,
            "via": "default" if variant == _cursor_user("Cursor") else "app",
            "exists": True,
            "default": variant == _cursor_user("Cursor"),
        }
    if os.path.isdir(cursor) and not any(r["agent"] == "cursor" for r in found.values()):
        found[("cursor", cursor)] = {
            "agent": "cursor",
            "path": cursor,
            "via": "default",
            "exists": True,
            "default": True,
        }
    return _named(found)


def _named(found: dict) -> list[Root]:
    """Stable names: the agent for its default folder, else the folder's own name (.claude-work ->
    claude-work). A clash gets -2, -3 in a fixed order, so a name never changes between runs."""
    roots, used = [], set()
    ordered = sorted(found.values(), key=lambda r: (r["agent"], not r.get("default"), r.get("data") or r["path"]))
    for r in ordered:
        base = _name(r)
        name, n = base, 1
        while name in used:
            n += 1
            name = f"{base}-{n}"
        used.add(name)
        roots.append(Root(name=name, **r))
    return roots


def _name(r: dict) -> str:
    agent = r["agent"]
    if r.get("default"):
        return agent
    source = os.path.basename(os.path.dirname(r["data"])) if r.get("data") else os.path.basename(r["path"])
    base = re.sub(r"[^a-z0-9]+", "-", source.lstrip(".").lower()).strip("-")
    if base.startswith(agent):
        return base
    return f"{agent}-{base}" if base else agent


def _expand(value: str) -> str | None:
    """$HOME, ${HOME} and ~ and any variable the environment sets; nothing else, and nothing is run."""
    h = home()
    value = value.replace("${HOME}", h).replace("$HOME", h)
    if value == "~" or value.startswith("~/"):
        value = h + value[1:]
    value = os.path.expandvars(value)
    if re.search(r"[$`(]", value) or not os.path.isabs(value):
        return None
    return value.rstrip("/") or None


def _declared() -> list[tuple[str, str, str]]:
    """(agent, folder, via) for each CLAUDE_CONFIG_DIR or CODEX_HOME assignment in the
    shell rc files, aliases and exports alike. A value that needs a command to work out is skipped."""
    out, seen_files = [], set()
    queue = [os.path.join(home(), rc) for rc in RC_FILES]
    while queue and len(seen_files) < 24:
        path = queue.pop(0)
        if path in seen_files:
            continue
        seen_files.add(path)
        try:
            if os.path.getsize(path) > MAX_RC_BYTES:
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            inc = _SOURCE.match(line)
            if inc:
                target = _expand(next(g for g in inc.groups() if g))
                if target and target.startswith(home() + os.sep):
                    queue.append(target)
                continue
            for m in list(_ASSIGN.finditer(line)) + list(_FISH_SET.finditer(line)):
                var = m.group(1)
                raw = next((g for g in m.groups()[1:] if g), "")
                folder = _expand(raw)
                if folder:
                    alias = _ALIAS.match(line)
                    via = f"alias {alias.group(1)}" if alias else "export"
                    agent = "claude" if var == "CLAUDE_CONFIG_DIR" else "codex"
                    out.append((agent, folder, f"shell: {via} in {os.path.basename(path)}"))
    return out


def _cursor_user(app: str) -> str:
    base = (
        os.path.join(home(), "Library", "Application Support")
        if os.uname().sysname == "Darwin"
        else os.path.join(home(), ".config")
    )
    return os.path.join(base, app, "User")


def _cursor_variants() -> list[str]:
    """Each Cursor build's User folder that has a chat database: Cursor, Cursor Nightly, ..."""
    base = os.path.dirname(os.path.dirname(_cursor_user("Cursor")))
    out = []
    for user in sorted(glob.glob(os.path.join(base, "Cursor*", "User"))):
        if os.path.exists(os.path.join(user, "globalStorage", "state.vscdb")):
            out.append(user)
    return out


# --- who is signed in (a hint to tell accounts apart; never a token) -------------------------


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else ""


def claude_global_file(root: Root) -> str:
    """Where this root's user-scope settings (MCP servers, sign-in) live."""
    if root.by_env:
        return os.path.join(root.path, ".claude.json")
    return os.path.join(home(), ".claude.json")


def account(root: Root) -> str | None:
    """Claude: the masked sign-in email. Codex: how it signs in. Cursor: the masked email. Read
    from one named field each; token fields are never touched."""
    try:
        if root.agent == "claude":
            with open(claude_global_file(root), encoding="utf-8") as f:
                who = json.load(f).get("oauthAccount") or {}
            return mask_email(who.get("emailAddress") or "") or None
        if root.agent == "codex":
            with open(os.path.join(root.path, "auth.json"), encoding="utf-8") as f:
                mode = json.load(f).get("auth_mode")
            return {"chatgpt": "ChatGPT sign-in", "apikey": "API key"}.get(mode) if mode else None
        if root.agent == "cursor" and root.data:
            db = os.path.join(root.data, "globalStorage", "state.vscdb")
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            with contextlib.closing(con):
                row = con.execute("select value from ItemTable where key = 'cursorAuth/cachedEmail'").fetchone()
            return mask_email(row[0]) if row and row[0] else None
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        return None
    return None
