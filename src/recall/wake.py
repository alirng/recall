"""Getting a waiting message (mail.py) to a chat that is sitting idle, without a daemon.

message_chat starts one short-lived `recall wake` after a handoff. What it does depends on the agent:

Claude Code. Off by default (`[messaging] wake_open_sessions`): a message posted to an open session runs as a
turn with that session's own permissions, which may bypass them, so it waits for the session's next turn
unless the person turns this on. An open session is listed in ~/.claude/sessions/<pid>.json with the address
of its message socket, and a line posted there starts a turn in an idle session, or is read between tool calls in a busy
one. Only the .json files are read: the .key files beside them hold auth keys, which macOS and Linux do not
ask for. The line's shape is not in the docs: it is the one Claude Code's own debug log prints. A closed
session is resumed with `claude --resume <id> -p` in manual permission mode, even when the person's own default
bypasses permissions: nobody is watching, and the text came from an agent. Tools the person has already allowed
in Claude Code's settings still run; anything else needing approval is refused. `[messaging]
inherit_permissions = true` lets it keep the person's own mode.

Codex. `codex exec resume`, read-only unless inherit_permissions is on. A thread active in the last
QUIET_SECONDS is left alone, because it may be open in the app.

Cursor. Nothing can resume a chat from the editor, so the message waits for its next edit or turn end.

Either way the message arrives as mail.render writes it, quoted and marked as from another agent, and
take(waking=True) limits how often one chat can wake another. Three more limits keep wakes from running away:
a woken chat's own messages wake nobody (WOKEN_ENV, set for the agent and so for its recall server), at most
WAKES_PER_HOUR wakes run an hour in all, and one chat is woken by one process at a time (a lock in running/).
A message that could not be delivered goes back in the mailbox, and an open session only counts as reached
once the message is in its transcript.
"""

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

from . import config, mail, paths

QUIET_SECONDS = 120
RUN_SECONDS = 900  # a resumed chat's turn
CONFIRM_SECONDS = 30  # for a message posted to an open session to appear in its transcript
WAKES_PER_HOUR = 20  # in all, so a chain of chats waking each other stops
WOKEN_ENV = "RECALL_WOKEN"  # set in a woken agent's environment: its messages wait instead of waking
WAKES = os.path.join(paths.HOME, "cache", "wakes.json")


def enabled() -> bool:
    table = config.load().get("messaging")
    return not isinstance(table, dict) or table.get("wake") is not False


def _setting(key: str) -> bool:
    table = config.load().get("messaging")
    return isinstance(table, dict) and table.get(key) is True


def _inherit() -> bool:
    return _setting("inherit_permissions")


def later(chat_id: str) -> None:
    """Start `recall wake` and return: the agent that sent the message is not kept waiting."""
    if not enabled() or os.environ.get(WOKEN_ENV):
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(
            [sys.executable, "-m", "recall", "wake", chat_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _env() -> dict:
    """The environment for a resumed agent: not the sender's session, and the signed-in plan, as agent_cli."""
    drop = ("CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_THREAD_ID")
    env = {
        k: v for k, v in os.environ.items() if k not in drop and not k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_"))
    }
    return {**env, WOKEN_ENV: "1"}


def _resume(command: list[str], folder: str) -> bool:
    try:
        done = subprocess.run(
            command,
            cwd=folder,
            env=_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RUN_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True  # it ran, so it has seen the message
    except OSError:
        return False
    return done.returncode == 0


def _open_socket(native: str) -> str | None:
    """The message socket of this Claude Code session, if a live process has it open."""
    for path in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            with open(path) as f:
                entry = json.load(f)
            if entry.get("sessionId") != native or not entry.get("messagingSocketPath"):
                continue
            os.kill(int(os.path.basename(path)[: -len(".json")]), 0)
        except (OSError, ValueError, AttributeError):
            continue
        return entry["messagingSocketPath"]
    return None


def _post(path: str, text: str) -> bool:
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(path)
            s.sendall(line.encode())
    except OSError:
        return False
    return True


def _reached(source: str | None, tag: str) -> bool:
    """Whether the message has shown up in the chat's transcript. The socket answers nothing, and an
    open session can hold a message it accepted, so only the transcript shows it arrived."""
    deadline = time.time() + CONFIRM_SECONDS
    while source and time.time() < deadline:
        with contextlib.suppress(OSError), open(source, "rb") as f:
            f.seek(max(0, os.path.getsize(source) - 1_000_000))
            if tag.encode() in f.read():
                return True
        time.sleep(1)
    return False


def _claude(native: str, folder: str | None, source: str | None, text: str, tag: str) -> str | None:
    if path := _open_socket(native):  # never resume a session that is open
        if not _setting("wake_open_sessions"):
            return None
        return "delivered to the open session" if _post(path, f"{text}\n\n({tag})") and _reached(source, tag) else None
    binary = shutil.which("claude")
    limit = [] if _inherit() else ["--permission-mode", "manual", "--allowedTools", "mcp__recall__message_chat"]
    if (
        binary
        and folder
        and os.path.isdir(folder)
        and _resume([binary, "--resume", native, *limit, "-p", text], folder)
    ):
        return "resumed the closed session"
    return None


def _codex(native: str, folder: str | None, updated_at: float | None, text: str) -> str | None:
    from .llm.agent_cli import CODEX_IN_CHATGPT_APP

    if updated_at and time.time() - updated_at < QUIET_SECONDS:
        return None
    binary = shutil.which("codex") or (CODEX_IN_CHATGPT_APP if os.path.exists(CODEX_IN_CHATGPT_APP) else None)
    if not binary or not folder or not os.path.isdir(folder):
        return None
    limit = [] if _inherit() else ["-c", 'sandbox_mode="read-only"']
    command = [binary, "exec", "resume", "--skip-git-repo-check", *limit, native, text]
    return "resumed the thread" if _resume(command, folder) else None


def _target(chat_id: str) -> tuple | None:
    try:
        with contextlib.closing(sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)) as con:
            return con.execute("select source, project, updated_at from sessions where id = ?", (chat_id,)).fetchone()
    except sqlite3.Error:
        return None


def _spent() -> bool:
    """Whether WAKES_PER_HOUR wakes have run in the last hour; if not, count this one."""
    now = time.time()
    with mail._locked():
        try:
            with open(WAKES) as f:
                times = [w for w in json.load(f) if isinstance(w, (int, float)) and now - w < 3600]
        except (OSError, ValueError, TypeError):
            times = []
        if len(times) >= WAKES_PER_HOUR:
            return True
        mail._write_json(WAKES, [*times, now])
    return False


def run(chat_id: str) -> str:
    """Deliver what is waiting for this chat, waking it if the agent allows. Says what happened."""
    agent, _, native = chat_id.partition(":")
    if agent not in ("claude", "codex"):
        return "it stays in the mailbox until the chat's next edit or turn end"
    if native.startswith("agent-"):
        return "a subagent's chat is not woken"
    os.makedirs(os.path.join(paths.HOME, "running"), exist_ok=True)
    name = hashlib.sha1(chat_id.encode()).hexdigest()[:16]
    with open(os.path.join(paths.HOME, "running", f"wake-{name}.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # already being woken: its hooks hand this message over at the end of that turn
            return "it is being woken already; the message waits for that turn's end"
        if _spent():
            return f"{WAKES_PER_HOUR} wakes ran in the last hour; the message waits for the chat's next turn"
        return _deliver(chat_id, agent, native)


def _deliver(chat_id: str, agent: str, native: str) -> str:
    target = _target(chat_id)
    if not target:
        return "no such chat in the index"
    source, folder, updated_at = target
    letters = mail.take(chat_id, source, waking=True)
    if not letters:
        return "nothing waiting"
    token = mail.grant(chat_id, os.path.realpath(source) if source else None, mail.peers(letters))
    text = mail.render(letters, token)
    tag = f"recall message {letters[-1]['id']}"
    done = _claude(native, folder, source, text, tag) if agent == "claude" else _codex(native, folder, updated_at, text)
    if not done:
        mail.put_back(chat_id, [m["id"] for m in letters])
        return "could not wake it: the message waits for the chat's next turn"
    return done
