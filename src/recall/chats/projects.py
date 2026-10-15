"""Map the folder a chat ran in to a stable project identity.

Agents run in clones, git worktrees, moved folders and throwaway scratch dirs.
A project's key is its git remote (so every clone and worktree of a repo shares
one memory), else its repo root, else the folder itself. Scratch and temp
folders belong to no project: chats there only feed global memories.
"""

import os
import re
from dataclasses import dataclass
from functools import lru_cache

CHAT_SCHEMA = "2026-10-15 parts replace schema 7"

HOME = os.path.expanduser("~")
_SCRATCH = [
    re.compile(p)
    for p in (
        r"/Library/Application Support/Claude/scratch-workspaces/",
        r"^/(private/)?tmp(/|$)",
        r"^/(private/)?var/folders/",
        r"/Documents/Codex/\d{4}-\d{2}-\d{2}/",  # Codex desktop's per-day scratch folders
        r"/\.codex/\.chatgpt-projects/",
        r"^empty-window$",
    )
]


@dataclass(frozen=True)
class Project:
    key: str  # "git:<host/path>" | "path:<abs path>"
    name: str
    root: str | None  # where it lives on disk now, if known


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _git_dir(root: str) -> str | None:
    """The repo's shared .git directory, following worktree indirection."""
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    content = _read(dot_git)
    if not content or not content.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(root, content.split(":", 1)[1].strip()))
    common = _read(os.path.join(gitdir, "commondir"))
    return os.path.normpath(os.path.join(gitdir, common.strip())) if common else gitdir


def _remote_url(git_dir: str) -> str | None:
    config = _read(os.path.join(git_dir, "config")) or ""
    section = None
    remotes = {}
    for line in config.splitlines():
        line = line.strip()
        m = re.match(r'\[remote "([^"]+)"\]', line)
        if m:
            section = m.group(1)
        elif line.startswith("["):
            section = None
        elif section and line.startswith("url"):
            remotes[section] = line.split("=", 1)[1].strip()
    return remotes.get("origin") or next(iter(remotes.values()), None)


def normalize_remote(url: str) -> str:
    """git@github.com:me/x.git, https://me@github.com/me/x -> github.com/me/x"""
    url = url.strip()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)  # credentials / ssh user
    url = re.sub(r"^(ssh\.)?dev\.azure\.com[:/]v3/", "dev.azure.com/", url)
    url = url.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", url) else url
    url = re.sub(r"/_git/", "/", url)
    url = re.sub(r"\.git/?$", "", url).rstrip("/")
    host, _, rest = url.partition("/")
    return f"{host.lower()}/{rest}"


def is_scratch(path: str | None) -> bool:
    if not path or os.path.normpath(path) in (HOME, "/"):
        return True
    return any(p.search(path) for p in _SCRATCH)


@lru_cache(maxsize=4096)
def resolve(path: str | None) -> Project | None:
    if is_scratch(path):
        return None
    if not os.path.isabs(path):
        # e.g. a Cursor folder slug whose folder no longer exists
        return Project(f"path:{path}", path.rsplit("-", 1)[-1], None)
    path = os.path.normpath(path)
    exists = os.path.exists(path)
    # Climb to the enclosing repo. A deleted worktree or subfolder still climbs into
    # its repo; a moved project whose parent is not a repo falls through below.
    root = path
    while root and root not in ("/", HOME):
        git_dir = _git_dir(root) if os.path.exists(root) else None
        if git_dir:
            remote = _remote_url(git_dir)
            main_root = os.path.dirname(git_dir) if git_dir.endswith(".git") else root
            if remote:
                key = normalize_remote(remote)
                return Project(f"git:{key}", key.rsplit("/", 1)[-1], main_root)
            return Project(f"path:{main_root}", os.path.basename(main_root), main_root)
        root = os.path.dirname(root)
    return Project(f"path:{path}", os.path.basename(path), path if exists else None)
