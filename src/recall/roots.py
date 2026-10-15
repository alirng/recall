"""Harness roots: every place a coding agent keeps its settings and chats on this machine.

One person can run the same agent against several folders: Claude Code with a second
CLAUDE_CONFIG_DIR for a work account, Codex with another CODEX_HOME, Cursor next to Cursor
Nightly. Each folder is a root with a stable name (claude, claude-work, codex, cursor,
cursor-nightly). discover() finds them from the defaults, the environment, the shell's rc
files (assignments that start a statement, `export` lines and aliases) and folders named like a config
dir. It only reads, and from the rc files it keeps just the two variables' values; nothing else in
them is stored or shown. A folder counts as a root once the agent has used it, and never when it is the
home folder or above, or looks like a backup or a copy of the default.

What each agent does with its root (live-checked in October 2026 unless marked):
  claude  CLAUDE_CONFIG_DIR holds settings.json, skills/ and projects/. Without the variable the
          user-scope MCP servers sit in ~/.claude.json; with it, in <dir>/.claude.json, also when the
          variable names ~/.claude itself (see claude_global_files).
  codex   CODEX_HOME holds config.toml (MCP servers), hooks.json, skills/ and sessions/.
  cursor  MCP servers (~/.cursor/mcp.json), hooks, skills and agent transcripts (~/.cursor/projects)
          are one set for every Cursor build: the app hard-codes ".cursor" (read in the stable
          app; a variant such as Nightly is assumed to do the same). A variant has its own
          Application Support/<Name>/User folder with its own state.vscdb, so its chats are
          the only thing that differs.
"""

import contextlib
import glob
import itertools
import json
import os
import re
import shlex
import sqlite3
from dataclasses import dataclass

AGENTS = ("claude", "codex", "cursor")
ENV_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_VAR_AGENT = {v: k for k, v in ENV_VARS.items()}
# What the agent itself writes once it has run in a folder. A bare projects/ or sessions/ folder is not
# enough (other tools have those), and settings.json / config.toml are what recall or a person writes
# before the agent ever starts. Chat files count too (see has_markers).
MARKERS = {
    "claude": (".claude.json", "history.jsonl"),
    "codex": ("auth.json", "installation_id"),
}
# A folder named like a copy is somebody's backup, not an account.
_BACKUP_NAME = re.compile(
    r"(?i)(^|[-_.])(backups?|bak|bk|old|orig|copy|copies|tmp|temp|archives?|archived)([-_.0-9]|$)"
)
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
_PUNCT = ";&|()"
_ASSIGN_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_SOURCE_HEADS = ("source", ".")
_DECLARE_HEADS = ("export", "declare", "typeset", "readonly", "local", "env")


@dataclass(frozen=True)
class Root:
    agent: str  # claude, codex or cursor
    name: str  # stable and unique: claude, claude-work, codex, cursor, cursor-nightly
    path: str  # claude, codex: the config dir; cursor: ~/.cursor, shared by every build
    data: str | None = None  # cursor: this build's Application Support/<Name>/User folder
    via: str = "default"  # default, env, shell (an alias or export), folder or app
    exists: bool = True  # False: named by the environment or a shell rc, but the agent has not used it yet
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


# --- what the person chose for a root (config.toml) ---------------------------------------------
#   [roots.claude-work]
#   connect = false   autopilot and `recall install` leave this root alone
#   index = false     its chats are not read at all
#   mine = true       `recall dream` turns its chats into memory notes (default: the default roots only)


def option(root: Root, key: str, cfg: dict | None = None) -> bool:
    """A per-root setting. connect and index default to true. mine defaults to true for the default
    roots only: an account or install that is not the person's main one (a work account) is indexed so
    its chats can be searched, but what it says does not become memory until they opt in."""
    if cfg is None:
        from . import config

        cfg = config.load()
    table = (cfg.get("roots") or {}).get(root.name)
    value = table.get(key) if isinstance(table, dict) else None
    if isinstance(value, bool):
        return value
    return root.default if key == "mine" else True


def set_option(root: Root, key: str, value: bool) -> None:
    from . import config

    cfg = config.load()
    table = dict(cfg["roots"].get(root.name) or {})
    table[key] = value
    cfg["roots"] = {**cfg["roots"], root.name: table}
    config.save(cfg)


def unmined(found: list[Root] | None = None) -> list[str]:
    """Names of the roots whose chats `recall dream` must not read."""
    from . import config

    cfg = config.load()
    return [r.name for r in found or discover() if not option(r, "mine", cfg)]


def discover() -> list[Root]:
    """Every root, defaults first. Cheap: a few stats, the rc files and two directory listings.

    A folder reached by more than one route (the default also named by CLAUDE_CONFIG_DIR) is one
    root, named by the first route; the default keeps by_env=False. A folder the environment, a
    shell rc or a glob names is live (`exists`) only once the agent has used it, so a typo or an
    alias for an account that has not signed in yet is never written to."""
    found: dict[tuple[str, str], dict] = {}

    def add(agent: str, path: str, via: str, **extra) -> None:
        path = os.path.abspath(path)
        key = (agent, os.path.realpath(path))
        if key in found:
            return
        live = os.path.isdir(path) if extra.get("default") else has_markers(agent, path)
        found[key] = {"agent": agent, "path": path, "via": via, "exists": live, "by_env": False, **extra}

    for agent in ("claude", "codex"):
        d = default_dir(agent)
        if os.path.isdir(d) or (agent == "claude" and os.path.exists(os.path.join(home(), ".claude.json"))):
            add(agent, d, "default", default=True, by_env=False)
        env = os.environ.get(ENV_VARS[agent])
        folder = _expand(env) if env else None
        if folder and usable(folder):
            add(agent, folder, "env", by_env=True)
    for agent, path, via in _declared():
        add(agent, path, via, by_env=True)
    for agent in ("claude", "codex"):
        for path in sorted(
            glob.glob(os.path.join(home(), f".{agent}-*")) + glob.glob(os.path.join(home(), f".{agent}_*"))
        ):
            if (
                os.path.isdir(path)
                and not _BACKUP_NAME.search(os.path.basename(path).lstrip("."))
                and has_markers(agent, path)
                and not _copy_of_default(agent, path)
            ):
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


def usable(folder: str) -> bool:
    """Not a place that holds more than one agent's folder: the home folder, anything above it, or a
    top-level directory such as /Users. A rc line naming one of those would make recall write
    settings files into the person's whole account."""
    real = os.path.realpath(folder)
    h = os.path.realpath(home())
    return not (real == h or h.startswith(real.rstrip(os.sep) + os.sep) or os.path.dirname(real) in ("/", ""))


def has_markers(agent: str, path: str) -> bool:
    """True once the agent itself has used this folder: a file only it writes, or a chat."""
    if not os.path.isdir(path):
        return False
    if any(os.path.exists(os.path.join(path, m)) for m in MARKERS.get(agent, ())):
        return True
    return next(_chat_files(agent, path), None) is not None


def _chat_files(agent: str, path: str):
    """Chat files under an agent folder, as paths relative to it; lazy, so a check stops at the first."""
    if agent == "claude":
        for f in glob.iglob(os.path.join(path, "projects", "*", "*.jsonl")):
            yield os.path.relpath(f, path)
    elif agent == "codex":
        for sub in ("sessions", "archived_sessions"):
            for here, _, files in os.walk(os.path.join(path, sub)):
                for name in files:
                    if name.endswith(".jsonl"):
                        yield os.path.relpath(os.path.join(here, name), path)


def _copy_of_default(agent: str, path: str) -> bool:
    """A folder whose chats are, file for file, the default folder's is a copy of it (a backup, a
    restore), not a second account: a sample of its chats exist at the same place in the default."""
    base = default_dir(agent)
    if os.path.realpath(base) == os.path.realpath(path):
        return False
    sample = list(itertools.islice(_chat_files(agent, path), 20))
    return bool(sample) and 2 * sum(os.path.exists(os.path.join(base, rel)) for rel in sample) >= len(sample)


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
    """(agent, folder, via) for each CLAUDE_CONFIG_DIR or CODEX_HOME the shell rc files set: an
    `export`, an assignment that starts a statement, or one inside an alias. Text that only mentions
    the variable (an echo, a comment, a later word of a command) sets nothing and is skipped, as is a
    value that needs a command to work out or that names a folder holding more than one agent's."""
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
        fish = path.endswith("config.fish")
        for words in _statements(lines):
            if words[0] in _SOURCE_HEADS and len(words) > 1:
                target = _expand(words[1])
                if target and target.startswith(home() + os.sep):
                    queue.append(target)
                continue
            for var, raw, via in _assigned(words, fish):
                folder = _expand(raw)
                if folder and usable(folder):
                    out.append((_VAR_AGENT[var], folder, f"shell: {via} in {os.path.basename(path)}"))
    return out


def _statements(lines: list[str]):
    """The statements in a shell file as lists of words, quotes resolved. A quoted string may run over
    several lines, so a line that leaves a quote open is joined with the next ones; comments are cut
    off where they start; ; && || | and brackets end a statement."""
    i = 0
    while i < len(lines):
        chunk, j, words = lines[i], i + 1, None
        while words is None:
            try:
                lex = shlex.shlex(chunk, posix=True, punctuation_chars=_PUNCT)
                lex.whitespace_split = True
                words = list(lex)
            except ValueError:  # a quote is still open
                if j >= len(lines) or j - i > 40:
                    break
                chunk, j = chunk + "\n" + lines[j], j + 1
        if words is None:
            i += 1  # give up on this line, read on from the next
            continue
        i = j
        cur = []
        for w in words:
            if w and all(c in _PUNCT for c in w):
                if cur:
                    yield cur
                cur = []
            else:
                cur.append(w)
        if cur:
            yield cur


def _assigned(words: list[str], fish: bool = False, via: str = "export") -> list[tuple[str, str, str]]:
    """(variable, value, via) for CLAUDE_CONFIG_DIR / CODEX_HOME set by this statement: the NAME=value
    words it starts with (after export, declare, env ...), the value of fish's `set`, or any of
    those inside an alias."""
    w = list(words)
    while w and w[0] in ("{", "then", "do", "else"):
        w.pop(0)
    if not w:
        return []
    out = []
    if w[0] == "alias":
        args = w[1:]
        for k, arg in enumerate(args):
            name, eq, body = arg.partition("=")
            if not eq and k == 0 and len(args) > 1:  # fish: alias name body...
                name, body = arg, " ".join(args[1:])
            elif not eq:
                continue
            for st in _statements([body]):
                out += _assigned(st, fish, f"alias {name}")
            if not eq:
                break
        return out
    if fish and w[0] == "set":
        rest = [x for x in w[1:] if not x.startswith("-")]
        return [(rest[0], rest[1], via)] if len(rest) > 1 and rest[0] in _VAR_AGENT else []
    if w[0] in _DECLARE_HEADS:
        w.pop(0)
        while w and w[0].startswith("-"):
            w.pop(0)
    for word in w:
        m = _ASSIGN_WORD.match(word)
        if not m:
            break
        if m.group(1) in _VAR_AGENT:
            out.append((m.group(1), m.group(2), via))
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
    """a***@***: the first letter tells two accounts apart; the domain (an employer) is not shown."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@***" if domain else ""


def claude_global_files(root: Root) -> list[str]:
    """Where this root's user-scope settings (MCP servers, sign-in) live; Claude Code reads one file per
    way it is launched (checked live, CLI 2.1.x, in a sandbox HOME):
      CLAUDE_CONFIG_DIR=<dir>  <dir>/.claude.json, and nothing under HOME
      no variable              ~/.claude.json
    A folder reached by the variable only (every non-default root) has the first. The default folder can
    be launched both ways, so its list is ~/.claude.json plus <dir>/.claude.json when that file exists:
    its existence is the evidence that something launches Claude with the variable. recall never creates
    it (a first run with a pre-seeded file is untested)."""
    own = os.path.join(root.path, ".claude.json")
    if root.by_env:
        return [own]
    files = [os.path.join(home(), ".claude.json")]
    if os.path.exists(own) and os.path.realpath(own) != os.path.realpath(files[0]):
        files.append(own)
    return files


def claude_global_file(root: Root) -> str:
    """The first of claude_global_files: the one a plain launch (or the variable alone) uses."""
    return claude_global_files(root)[0]


def account(root: Root) -> str | None:
    """Claude: the masked sign-in email. Codex: how it signs in. Cursor: the masked email. Read
    from one named field each; token fields are never touched."""
    try:
        if root.agent == "claude":
            for path in claude_global_files(root):
                with open(path, encoding="utf-8") as f:
                    who = json.load(f).get("oauthAccount") or {}
                if who.get("emailAddress"):
                    return mask_email(who["emailAddress"])
            return None
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
