"""Claude Code history: one JSONL file per session under ~/.claude/projects/<folder>/, and one per
subagent under <folder>/<session>/subagents/ (a workflow's under subagents/workflows/<run>/)."""

import glob
import json
import os
import textwrap

from ...roots import Root
from ..errors import Calls, declined, position, signature
from ..projects import is_scratch
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

CHAT_SCHEMA = "2026-10-15 parts replace schema 7"

ROOT = os.path.expanduser("~/.claude/projects")  # the default root's; roots.discover() finds the rest


def _folders(roots) -> list[tuple[str | None, str]]:
    """(root name, chats folder) for each root whose folder exists, once per real folder."""
    if roots is None:
        roots = [Root("claude", "claude", os.path.dirname(ROOT), default=True)]
    out, seen = [], set()
    for r in roots:
        folder = os.path.join(r.path, "projects")
        real = os.path.realpath(folder)
        if os.path.isdir(folder) and real not in seen:
            seen.add(real)
            out.append((r.name, folder))
    return out


def detect(roots=None) -> int | None:
    folders = _folders(roots)
    if not folders:
        return None
    return sum(len(glob.glob(os.path.join(f, "*", "*.jsonl"))) for _, f in folders)


SUBAGENTS = (("*", "*", "subagents", "agent-*.jsonl"), ("*", "*", "subagents", "workflows", "*", "agent-*.jsonl"))


def items(roots=None):
    for name, folder in _folders(roots):
        files = [(p, False) for p in glob.glob(os.path.join(folder, "*", "*.jsonl"))]
        files += [(p, True) for pattern in SUBAGENTS for p in glob.glob(os.path.join(folder, *pattern))]
        for path, subagent in files:
            try:
                st = os.stat(path)
            except OSError:  # deleted since the listing; the index and the archive keep its chat
                continue
            yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path, n=name, a=subagent: load(p, n, a))


def load(path: str, root: str | None = None, subagent: bool = False) -> Session | None:
    """subagent: the file is a subagent's, whose every entry Claude Code marks a sidechain. Its
    user turn is the brief its parent agent wrote, and its assistant turn is kept to the report."""
    s = Session("claude", os.path.basename(path)[:-6], path)
    s.root = root
    messages: list[Message] = []
    report = _Report(messages) if subagent else None
    pending: dict[str, tuple[str, object]] = {}  # tool_use id -> (tool, its input)
    calls = Calls()
    summary = None
    for d in read_jsonl(path):
        kind = d.get("type")
        if kind == "custom-title":
            s.title = d.get("customTitle") or s.title
        elif kind == "summary":
            summary = summary or d.get("summary")
        att = d.get("attachment") or {}
        if kind == "attachment" and att.get("type") == "queued_command" and att.get("commandMode") == "prompt":
            # A message that arrived while the agent was mid-turn: the person's, unless
            # another session or a background task sent it.
            if _by_person(att) and _by_person(d):
                ts = iso_ts(att.get("timestamp") or d.get("timestamp"))
                if report is not None:
                    report.flush()
                append(messages, "user", clean_user_text(str(att.get("prompt") or "")), ts)
            continue
        if kind not in ("user", "assistant") or (d.get("isSidechain") and not subagent) or d.get("isMeta"):
            continue
        if kind == "user" and (d.get("isCompactSummary") or not _by_person(d)):
            continue  # a compaction summary, a peer message or a task notification
        if not s.project and str(d.get("entrypoint", "")).startswith("sdk"):
            s.origin = "headless"  # Agent SDK / `claude -p` run, not a person typing
        cwd = d.get("cwd")
        if cwd and (not s.project or (is_scratch(s.project) and not is_scratch(cwd))):
            s.project = cwd  # sessions can move out of a scratch folder into a project
        s.branch = d.get("gitBranch") or s.branch
        ts = iso_ts(d.get("timestamp"))
        s.started_at = s.started_at or ts
        s.updated_at = ts or s.updated_at
        content = (d.get("message") or {}).get("content")
        _tool_calls(content, pending, calls, messages, ts)
        text = text_blocks(content, ("text",))
        text = clean_user_text(text) if kind == "user" else clean_assistant_text(text)
        if report is None:
            append(messages, kind, text, ts)
        elif kind == "assistant":
            report.add("\n\n".join(t for t in (text, _structured(content)) if t), ts)
        elif not text:  # tool results
            report.stale = True
        elif text := _brief(text):  # the brief, or a follow-up its parent sent
            report.flush()
            append(messages, kind, text, ts)
    s.title = s.title or summary
    if report is not None:
        report.flush()
        s.origin = "subagent"
        s.title = _description(path) or s.title
        s.parent = f"claude:{os.path.basename(path[: path.rindex(os.sep + 'subagents' + os.sep)])}"
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


class _Report:
    """A subagent's report: the last stretch of assistant text in each of its turns. What it
    wrote before a tool call ran is narration ("let me check the config") and is dropped, which
    also keeps the report inside the length a stored message is cut to."""

    def __init__(self, messages: list[Message]):
        self.messages, self.parts, self.ts, self.stale = messages, [], None, False

    def add(self, text: str, ts: float | None) -> None:
        if not text:
            return
        if self.stale:  # a tool ran since the text so far
            self.parts, self.ts, self.stale = [], None, False
        self.parts.append(text)
        self.ts = self.ts or ts

    def flush(self) -> None:
        append(self.messages, "assistant", "\n\n".join(self.parts), self.ts)
        self.parts, self.ts, self.stale = [], None, False


# How Claude Code frames a workflow subagent's brief: first the person's request that started the
# run, which the parent's chat already holds, then the task the workflow's script computed, indented.
_RELAYED = "[Workflow harness — user request]"
_COMPUTED = "[Workflow harness — computed task]"


def _brief(text: str) -> str:
    """A subagent's brief without the workflow harness's framing: without the relayed request,
    which every subagent of a run repeats, and with the computed task as the script wrote it."""
    if text.startswith(_RELAYED):
        return ""
    if text.startswith(_COMPUTED):
        return textwrap.dedent(text.partition("\n")[2]).strip()
    return text


def _description(path: str) -> str | None:
    """What the parent agent called the subagent's task, from the file Claude Code keeps beside the transcript."""
    try:
        with open(path[: -len(".jsonl")] + ".meta.json") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta.get("description") if isinstance(meta, dict) else None


def _structured(content) -> str:
    """A workflow subagent's report, which it returns as a StructuredOutput call rather than as text."""
    calls = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return "\n\n".join(
        _plain(b.get("input")) for b in calls if b.get("type") == "tool_use" and b.get("name") == "StructuredOutput"
    )


def _plain(value) -> str:
    """A tool input as text with its strings as written: JSON would escape their line breaks into the words."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_plain(v)}" for k, v in value.items() if v not in (None, "", [], {}))
    if isinstance(value, list):
        return "\n".join(_plain(v) for v in value)
    return json.dumps(value)


# What Claude Code says a user-turn entry is when the person did not write it: another
# session's message, a background task's result. Other kinds are taken as the person's, so
# a kind this list does not know yet loses nothing.
NOT_THE_PERSON = ("peer", "task-notification")


def _by_person(entry: dict) -> bool:
    """False for what Claude Code delivers in the user's turn on someone else's behalf,
    tagged with an origin."""
    origin = entry.get("origin")
    return not (isinstance(origin, dict) and origin.get("kind") in NOT_THE_PERSON)


def _tool_calls(content, pending: dict, calls: Calls, messages: list, ts) -> None:
    """Note each tool call, and each result: failed when Claude Code marked it is_error. A call
    the person declined is neither a failure nor a success, so it is not noted at all."""
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            pending[b.get("id")] = (b.get("name") or "", b.get("input"))
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
            tool, args = pending.pop(b.get("tool_use_id"))
            failure = None
            if b.get("is_error"):
                body = b.get("content")
                body = body if isinstance(body, str) else text_blocks(body, ("text",))
                if declined(body):
                    continue
                failure = signature(body) or "error"
            calls.add(*position(messages), ts, tool, args, b.get("tool_use_id"), failure)
