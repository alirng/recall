"""Claude Code history: one JSONL file per session under ~/.claude/projects/<folder>/."""

import glob
import os

from ..projects import is_scratch
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

ROOT = os.path.expanduser("~/.claude/projects")


def detect() -> int | None:
    if not os.path.isdir(ROOT):
        return None
    return len(glob.glob(os.path.join(ROOT, "*", "*.jsonl")))


def items():
    # Only top-level session files; subagent transcripts live in nested folders.
    for path in glob.glob(os.path.join(ROOT, "*", "*.jsonl")):
        st = os.stat(path)
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path: load(p))


def load(path: str) -> Session | None:
    s = Session("claude", os.path.basename(path)[:-6], path)
    messages: list[Message] = []
    summary = None
    for d in read_jsonl(path):
        kind = d.get("type")
        if kind == "custom-title":
            s.title = d.get("customTitle") or s.title
        elif kind == "summary":
            summary = summary or d.get("summary")
        att = d.get("attachment") or {}
        if kind == "attachment" and att.get("type") == "queued_command" and att.get("commandMode") == "prompt":
            # A message the person typed while the agent was mid-turn.
            ts = iso_ts(att.get("timestamp") or d.get("timestamp"))
            append(messages, "user", clean_user_text(str(att.get("prompt") or "")), ts)
            continue
        if kind not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
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
        text = text_blocks(content, ("text",))
        text = clean_user_text(text) if kind == "user" else clean_assistant_text(text)
        append(messages, kind, text, ts)
    s.title = s.title or summary
    s.messages = messages
    return s
