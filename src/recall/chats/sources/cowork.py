"""Claude desktop's local Cowork sessions, under its local-agent-mode-sessions folder.

Each session has a sidecar, <account>/<org>/local_<id>.json, and beside it a folder holding the transcript
in Claude Code's own format, local_<id>/.claude/projects/<folder>/<session>.jsonl, so claude.load parses it.
The folder's audit.jsonl records the same session again and is not read.

The sidecar gives the title and the folder the person picked. The transcript's cwd is the session's sandbox
(/sessions/<name>, or a folder inside the app's own), which says nothing about the project, so a session
with no picked folder has no project. The sidecar also holds the account's name and email and the system
prompt: only the fields named here are taken from it.

Sessions under agent/ are the app's own background runs, not a person's, and tasks Cowork ran in the cloud
are not on this machine.
"""

import glob
import json
import os

from ... import roots
from ..transcript import Session
from . import Item, claude, iso_ts

CHAT_SCHEMA = "2026-11-10 new source: Claude desktop's local Cowork sessions; <uploaded_files> stripped"

KEY = "cowork"


def _sidecars() -> list[str]:
    pattern = os.path.join("*", "*", "local_*.json")
    return sorted(p for folder in roots.other_dirs(KEY) for p in glob.glob(os.path.join(glob.escape(folder), pattern)))


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_sidecars())


def items():
    for sidecar in _sidecars():
        pattern = os.path.join(glob.escape(sidecar[: -len(".json")]), ".claude", "projects", "*", "*.jsonl")
        for path in glob.glob(pattern):
            try:
                transcript, meta = os.stat(path), os.stat(sidecar)
            except OSError:  # deleted since the listing; the index and the archive keep its chat
                continue
            fingerprint = f"{transcript.st_mtime_ns}:{transcript.st_size}:{meta.st_mtime_ns}"
            yield Item(path, fingerprint, lambda p=path, c=sidecar: load(p, c))


def _meta(sidecar: str) -> dict:
    try:
        with open(sidecar) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def load(path: str, sidecar: str) -> Session | None:
    s = claude.load(path, KEY)
    if s is None:
        return None
    s.agent = KEY
    meta = _meta(sidecar)
    picked = meta.get("userSelectedFolders")
    s.project = picked[0] if isinstance(picked, list) and picked and isinstance(picked[0], str) else None
    s.branch = None  # the sandbox's, when it has one
    s.title = meta.get("title") or s.title
    s.started_at = s.started_at or iso_ts(meta.get("createdAt"))
    s.updated_at = s.updated_at or iso_ts(meta.get("lastActivityAt"))
    return s


AGENTS = {KEY: ("Claude Cowork", detect, items)}
