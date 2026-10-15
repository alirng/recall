"""How long each agent keeps its chats, and the limit recall's archive honours.

recall keeps a copy of every chat it indexes (chats/archive.py), past the point where an
agent deletes its own file. That is the point when the deletion is the agent's default or
the person's own setting. It is not when an organisation sets the period: then recall
keeps those chats no longer than the policy allows.

Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30, minimum 1).
An organisation can set it through managed settings, which override the user's. They come
from, highest first (code.claude.com/docs/en/managed-settings):

1. server-managed settings, cached at ~/.claude/remote-settings.json (the path is in Claude
   Code's documentation; the format of the file is not, so reading `cleanupPeriodDays` from
   it as a top-level key of a settings object is an assumption, unverified against a real
   organisation's cache);
2. an MDM profile: the com.anthropic.claudecode managed preferences domain on macOS;
3. managed-settings.json, then managed-settings.d/*.json, in the system directory.

Claude Code applies only the highest source that delivers any policy key, unless the policy
asks to merge them. recall takes the shortest period any of them sets: never longer than
what Claude Code applies, and sometimes shorter.

Codex and Cursor have no setting that deletes chats on a schedule, managed or not (Codex's
history.max_bytes trims only history.jsonl, the prompt history recall does not read).
"""

import contextlib
import getpass
import glob
import json
import os
import plistlib
import time

CLAUDE_USER_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_REMOTE_CACHE = os.path.expanduser("~/.claude/remote-settings.json")
CLAUDE_SYSTEM_DIRS = ("/Library/Application Support/ClaudeCode", "/etc/claude-code")
CLAUDE_DEFAULT_DAYS = 30


def claude_managed() -> tuple[int, str] | None:
    """(days, where) when an organisation sets Claude Code's cleanupPeriodDays."""
    found = []
    for where, settings in _managed_sources():
        days = settings.get("cleanupPeriodDays")
        if isinstance(days, int) and not isinstance(days, bool) and days >= 1:
            found.append((days, where))
    return min(found) if found else None


def claude_user_days() -> int | None:
    """The person's own cleanupPeriodDays, or None when unset (Claude Code then uses 30)."""
    days = _read_json(CLAUDE_USER_SETTINGS).get("cleanupPeriodDays")
    return days if isinstance(days, int) and not isinstance(days, bool) else None


def cutoffs() -> dict[str, float]:
    """agent -> the oldest last-activity time recall may keep a chat from, for agents whose
    retention an organisation sets."""
    managed = claude_managed()
    return {"claude": time.time() - managed[0] * 86400} if managed else {}


def apply(con) -> int:
    """Forget chats older than an organisation's retention period, everywhere recall holds
    them. Their files are left to the agent's own cleanup, so a chat that is still on disk
    is not read again unless it changes (and is then recent). Returns how many."""
    from . import forget
    from .chats import archive

    if not archive.enabled(con):
        return 0
    total = 0
    for agent, cutoff in cutoffs().items():
        rows = con.execute(
            """select id from (select id, max(coalesce(updated_at, 0)) last from chat_archive
                 where agent = ? group by id) where last < ?
               union select id from sessions where agent = ? and coalesce(updated_at, 0) < ?""",
            (agent, cutoff, agent, cutoff),
        )
        ids = [r[0] for r in rows]
        if ids:
            forget.purge(con, ids, remember=False)
            total += len(ids)
    if total:
        archive.log(f"forgot {total} chat(s) older than the retention period your organisation sets")
    return total


def claude_mdm_plists() -> list[str]:
    """A machine-wide profile, and one scoped to this user (macOS puts it in a folder per user)."""
    plists = ["/Library/Managed Preferences/com.anthropic.claudecode.plist"]
    with contextlib.suppress(KeyError, OSError):  # no user name for this uid (a container, a stripped account)
        plists.append(f"/Library/Managed Preferences/{getpass.getuser()}/com.anthropic.claudecode.plist")
    return plists


def _managed_sources():
    yield CLAUDE_REMOTE_CACHE, _read_json(CLAUDE_REMOTE_CACHE)
    for path in claude_mdm_plists():
        try:
            with open(path, "rb") as f:
                yield path, plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
    for folder in CLAUDE_SYSTEM_DIRS:
        merged = _read_json(os.path.join(folder, "managed-settings.json"))
        for path in sorted(glob.glob(os.path.join(folder, "managed-settings.d", "*.json"))):
            merged |= _read_json(path)  # later files replace single values
        yield folder, merged


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
