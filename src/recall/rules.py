"""What each agent is told about a repo, and closing the gaps between them.

Claude Code, Codex and Cursor each read their own instruction files, so a repo set up well
for one is a stranger to the others. In practice:

    AGENTS.md                Codex and Cursor; Claude Code when there is no CLAUDE.md, or
                             when CLAUDE.md imports it with a line reading @AGENTS.md
    CLAUDE.md                Claude Code and Cursor
    CLAUDE.local.md          Claude Code (personal, not committed)
    .claude/rules/*.md       Claude Code
    .cursor/rules/*.mdc      Cursor: always, for matching files, when relevant, or on request
    .cursorrules             Cursor (legacy)

`check()` reports which agent sees what. `plan()` proposes the smallest change that gives
every agent the same always-on rules: committed Cursor rules are copied into a marked block
in AGENTS.md, which all three read, and CLAUDE.md gains the @AGENTS.md import when it lacks
it. Personal rules (gitignored) never go into a committed file; recall's session hook names
them for Claude Code and Codex instead (see `pointers()`). Nothing is written unless asked.
"""

import difflib
import os
import re
import subprocess
from dataclasses import dataclass, field

import yaml

from . import inject

AGENTS = ("cursor", "claude", "codex")
LABELS = {"cursor": "Cursor", "claude": "Claude Code", "codex": "Codex"}
BLOCK_START = "<!-- recall:rules start"
BLOCK_END = "<!-- recall:rules end -->"
BLOCK_NOTE = (
    "<!-- recall:rules start · copied from the files named below by `recall rules` so every agent reads "
    "them; edit those files, then run `recall rules --write` again -->"
)
IMPORT_LINE = "@AGENTS.md"


@dataclass
class Source:
    path: str  # relative to the repo root
    kind: str  # agents | claude | claude-local | claude-rule | cursor-rule | cursorrules
    apply: str  # always | files | relevant | manual
    description: str
    lines: int
    personal: bool
    seen_by: set[str] = field(default_factory=set)
    globs: str = ""
    body: str = ""


@dataclass
class Repo:
    root: str
    sources: list[Source]
    mcp: dict[str, list[str]]  # agent -> server names
    skills: dict[str, list[str]]  # agent -> skill names
    synced: set[str]  # rule paths already copied into AGENTS.md


def scan(root: str) -> Repo:
    """Read every instruction file in the repo, and which agents load each."""
    sources: list[Source] = []
    agents_md = os.path.join(root, "AGENTS.md")
    claude_files = [p for p in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md") if _exists(root, p)]
    imports_agents = any(_imports_agents(root, p) for p in claude_files)
    agents_text = _read(agents_md)
    synced = set(re.findall(r"^_From `([^`]+)`_", _block(agents_text), re.M))

    # Whoever reads AGENTS.md also reads the rules recall copied into it.
    agents_seen = {"codex", "cursor"} | ({"claude"} if imports_agents or not claude_files else set())
    if agents_text:
        sources.append(_source(root, "AGENTS.md", "agents", "always", agents_seen))
    for p in claude_files:
        if os.path.islink(os.path.join(root, p)) and os.path.realpath(os.path.join(root, p)) == os.path.realpath(
            agents_md
        ):
            continue  # a link to AGENTS.md: counted above
        kind = "claude-local" if p == "CLAUDE.local.md" else "claude"
        sources.append(_source(root, p, kind, "always", {"claude", "cursor"} if p == "CLAUDE.md" else {"claude"}))
    for p in _glob(root, ".claude/rules", ".md"):
        meta, body = _frontmatter(_read(os.path.join(root, p)))
        scoped = meta.get("paths")
        s = _source(root, p, "claude-rule", "files" if scoped else "always", {"claude"}, body=body)
        s.globs = ", ".join(scoped) if isinstance(scoped, list) else str(scoped or "")
        sources.append(s)
    for p in _glob(root, ".cursor/rules", ".mdc"):
        meta, body = _frontmatter(_read(os.path.join(root, p)))
        if meta.get("alwaysApply"):
            apply = "always"
        elif meta.get("globs"):
            apply = "files"
        elif meta.get("description"):
            apply = "relevant"
        else:
            apply = "manual"
        seen = {"cursor"} | (agents_seen if p in synced else set())
        s = _source(root, p, "cursor-rule", apply, seen, body=body, description=str(meta.get("description") or ""))
        globs = meta.get("globs") or ""
        s.globs = ", ".join(globs) if isinstance(globs, list) else str(globs)
        sources.append(s)
    if _exists(root, ".cursorrules"):
        seen = {"cursor"} | (agents_seen if ".cursorrules" in synced else set())
        sources.append(_source(root, ".cursorrules", "cursorrules", "always", seen, body=_read(root + "/.cursorrules")))
    _mark_personal(root, sources)
    return Repo(root, sources, _mcp(root), _skills(root), synced)


def gaps(repo: Repo) -> dict[str, list[Source]]:
    """For each agent, the always-on rules another agent gets that it does not."""
    always = [s for s in repo.sources if s.apply == "always" and s.kind not in ("claude-local",)]
    return {a: [s for s in always if a not in s.seen_by] for a in AGENTS}


def plan(repo: Repo) -> dict[str, tuple[str, str]]:
    """The files to change and their (old, new) text: committed always-on Cursor rules into
    AGENTS.md's recall block, and the @AGENTS.md import where Claude Code would skip it."""
    changes: dict[str, tuple[str, str]] = {}
    shared = [s for s in repo.sources if s.kind in ("cursor-rule", "cursorrules") and not s.personal]
    agents_path = os.path.join(repo.root, "AGENTS.md")
    old = _read(agents_path)
    block = _render_block(shared)
    new = _replace_block(old, block)
    if new != old:
        changes["AGENTS.md"] = (old, new)
    will_have_agents = bool(new.strip())
    claude_files = [p for p in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md") if _exists(repo.root, p)]
    if will_have_agents and claude_files and not any(_imports_agents(repo.root, p) for p in claude_files):
        # CLAUDE.md if there is one; a personal CLAUDE.local.md alone gets the line instead
        target = next((p for p in claude_files if p != "CLAUDE.local.md"), claude_files[0])
        path = os.path.join(repo.root, target)
        if not os.path.islink(path):
            text = _read(path)
            note = "<!-- recall: Claude Code reads AGENTS.md only through this import when CLAUDE.md exists -->"
            changes[target] = (text, text.rstrip("\n") + f"\n\n{note}\n{IMPORT_LINE}\n")
    return changes


def diff(changes: dict[str, tuple[str, str]]) -> str:
    out = []
    for path, (old, new) in changes.items():
        out += difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null",
            tofile=f"b/{path}",
        )
    return "".join(out)


def write(root: str, changes: dict[str, tuple[str, str]]) -> list[str]:
    """Apply a plan. Each file is checked against what the plan read, so an edit made in the
    meantime is never overwritten."""
    written = []
    for path, (old, new) in changes.items():
        full = os.path.join(root, path)
        if _read(full) != old:
            raise RuntimeError(f"{path} changed since it was read; run recall rules again")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(new)
        written.append(path)
    return written


def pointers(folder: str | None, agent: str) -> list[str]:
    """One line for each always-on rule in the folder's repo that `agent` does not load
    itself, for the session hook. Cursor rules carry a one-line description, which is
    usually the rule in brief; the agent can open the file for the rest."""
    root = repo_root(folder)
    if not root or agent not in AGENTS:
        return []
    out = []
    for s in gaps(scan(root))[agent]:
        # The description is the rule file's own text: one line, capped, with nothing in it that
        # could close the block it is shown in or the code span around the path.
        what = inject.line((s.description or _first_line(s.body)).replace("`", ""), 160)
        path = inject.line(s.path.replace("`", ""), 160)
        out.append(f"- {what.rstrip('.') or 'always-on rule'} (read `{path}`)")
    return out


def repo_root(folder: str | None) -> str | None:
    """The working tree a folder is in: the nearest folder with a .git, so a worktree
    counts as its own checkout."""
    if not folder or not os.path.isdir(folder):
        return None
    path, home = os.path.abspath(folder), os.path.expanduser("~")
    while path not in ("/", home):
        if os.path.exists(os.path.join(path, ".git")):
            return path
        path = os.path.dirname(path)
    return None


# --- the report ---------------------------------------------------------------------


def report(repo: Repo, name: str | None = None) -> str:
    home = os.path.expanduser("~")
    where = repo.root.replace(home, "~", 1)
    lines = [f"{name} · {where}" if name else where, ""]
    width = max([len(_label(s)) for s in repo.sources] + [12]) + 2
    lines.append("Instructions".ljust(width) + "".join(LABELS[a].ljust(13) for a in AGENTS) + "applies")
    for s in repo.sources:
        marks = "".join(("✓" if a in s.seen_by else "·").ljust(13) for a in AGENTS)
        applies = {
            "always": "always",
            "files": f"files: {s.globs}",
            "relevant": "when relevant",
            "manual": "on request",
        }
        lines.append(_label(s).ljust(width) + marks + applies[s.apply])
    if not repo.sources:
        lines.append("(no instruction files)")
    for title, table in (("MCP servers", repo.mcp), ("Skills", repo.skills)):
        rows = {a: names for a, names in table.items() if names}
        if rows:
            lines += ["", title]
            lines += [f"  {LABELS[a]:12} {', '.join(names)}" for a, names in rows.items()]
    lines += ["", *summary(repo)]
    return "\n".join(lines)


def summary(repo: Repo) -> list[str]:
    missing = gaps(repo)
    out = []
    for a in AGENTS:
        if missing[a]:
            n = sum(s.lines for s in missing[a])
            out.append(f"{LABELS[a]} misses {len(missing[a])} always-on rules ({n} lines).")
    shared = [s for s in repo.sources if s.kind in ("cursor-rule", "cursorrules") and not s.personal]
    if any(s.path not in repo.synced for s in shared if s.apply == "always"):
        out.append("`--write` copies the committed Cursor rules into AGENTS.md, which every agent reads.")
    personal = [s for s in repo.sources if s.personal and s.kind in ("cursor-rule", "cursorrules")]
    if personal:
        out.append(
            f"{len(personal)} Cursor rules are personal (gitignored), so they stay out of committed files; "
            "recall's session hook names them for Claude Code and Codex."
        )
    for s in repo.sources:
        own = [ln for ln in s.body.splitlines() if ln.strip() and ln.strip() != IMPORT_LINE and "recall:" not in ln]
        if s.kind == "claude" and "codex" not in s.seen_by and own:
            out.append(
                f"{s.path} has rules of its own that Codex never reads: move them into AGENTS.md "
                f"and leave {IMPORT_LINE} in {s.path}."
            )
    tools = [a for a in AGENTS if repo.mcp.get(a)]
    if 0 < len(tools) < len(AGENTS):
        out.append(f"MCP servers are set up for {', '.join(LABELS[a] for a in tools)} only; recall leaves those alone.")
    return out or ["Every agent reads the same always-on rules."]


def _label(s: Source) -> str:
    return f"{s.path} ({s.lines})" + (" personal" if s.personal else "")


# --- reading files ------------------------------------------------------------------


def _source(root, path, kind, apply, seen_by, body=None, description="") -> Source:
    text = body if body is not None else _read(os.path.join(root, path))
    return Source(
        path=path,
        kind=kind,
        apply=apply,
        description=description,
        lines=len(_read(os.path.join(root, path)).splitlines()),
        personal=False,
        seen_by=set(seen_by),
        body=text,
    )


def _mark_personal(root: str, sources: list[Source]) -> None:
    """Gitignored files are personal: they stay on this machine."""
    if not sources:
        return
    r = subprocess.run(
        ["git", "-C", root, "check-ignore", "--stdin"],
        input="\n".join(s.path for s in sources),
        capture_output=True,
        text=True,
    )
    ignored = set(r.stdout.split("\n")) if r.returncode in (0, 1) else set()
    for s in sources:
        s.personal = s.path in ignored or s.kind == "claude-local"


def _frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        # Cursor writes globs unquoted (globs: *.ts), which YAML rejects; read it by line
        meta = dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), re.M))
        meta = {k: (v.lower() == "true" if v.lower() in ("true", "false") else v) for k, v in meta.items()}
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def _imports_agents(root: str, path: str) -> bool:
    full = os.path.join(root, path)
    if os.path.islink(full) and os.path.realpath(full) == os.path.realpath(os.path.join(root, "AGENTS.md")):
        return True
    text = re.sub(r"```.*?```", "", _read(full), flags=re.S)
    target = os.path.relpath(os.path.join(root, "AGENTS.md"), os.path.dirname(full))
    return any(line.strip() in (f"@{target}", f"@./{target}") for line in text.splitlines())


def _mcp(root: str) -> dict[str, list[str]]:
    import json
    import tomllib

    out = {}
    for agent, path in (("claude", ".mcp.json"), ("cursor", ".cursor/mcp.json")):
        try:
            out[agent] = sorted(json.loads(_read(os.path.join(root, path)) or "{}").get("mcpServers", {}))
        except (json.JSONDecodeError, AttributeError):
            out[agent] = []
    try:
        out["codex"] = sorted(tomllib.loads(_read(os.path.join(root, ".codex/config.toml"))).get("mcp_servers", {}))
    except tomllib.TOMLDecodeError:
        out["codex"] = []
    return out


def _skills(root: str) -> dict[str, list[str]]:
    folders = {"claude": ".claude/skills", "cursor": ".cursor/skills", "codex": ".agents/skills"}
    out = {}
    for agent, folder in folders.items():
        path = os.path.join(root, folder)
        out[agent] = sorted(n for n in os.listdir(path) if not n.startswith(".")) if os.path.isdir(path) else []
    return out


def _render_block(rules: list[Source]) -> str:
    if not rules:
        return ""
    always = [s for s in rules if s.apply == "always"]
    others = [s for s in rules if s.apply in ("files", "relevant")]
    if not always and not others:
        return ""
    out = [BLOCK_NOTE, "", "## Rules from .cursor/rules", ""]
    for s in always:
        out += [_demote(s.body.strip()), "", f"_From `{s.path}`_", ""]
    if others:
        out += ["### Rules for some tasks", ""]
        for s in others:
            when = f"when working on `{s.globs}`" if s.apply == "files" else "when relevant"
            what = s.description or _first_line(s.body)
            out.append(f"- {what.rstrip('.')}: read `{s.path}` {when}.")
        out.append("")
    out.append(BLOCK_END)
    return "\n".join(out)


def _replace_block(text: str, block: str) -> str:
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start != -1 and end != -1:
        before, after = text[:start].rstrip("\n"), text[end + len(BLOCK_END) :].lstrip("\n")
        joined = "\n\n".join(p for p in (before, block, after.rstrip("\n")) if p)
        return joined + "\n" if joined else ""
    if not block:
        return text
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"


def _block(text: str) -> str:
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    return text[start:end] if start != -1 and end != -1 else ""


def _demote(markdown: str) -> str:
    """Push every heading two levels down, so a rule's title sits under the block's heading."""
    out, fenced = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        out.append("##" + line if not fenced and re.match(r"^#{1,4} ", line) else line)
    return "\n".join(out)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:160]
    return ""


def _glob(root: str, folder: str, ext: str) -> list[str]:
    base = os.path.join(root, folder)
    out = []
    for dirpath, _, filenames in os.walk(base):
        out += [os.path.relpath(os.path.join(dirpath, n), root) for n in filenames if n.endswith(ext)]
    return sorted(out)


def _exists(root: str, path: str) -> bool:
    return os.path.isfile(os.path.join(root, path))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""
