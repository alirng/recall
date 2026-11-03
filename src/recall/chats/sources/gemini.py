"""Gemini CLI, Qwen Code (a Gemini CLI fork) and iFlow (a Gemini CLI fork that writes Claude-like JSONL).

Gemini CLI keeps a chat at `~/.gemini/tmp/<project id>/chats/session-*.{json,jsonl}`. Up to v0.38 it was one
JSON object per chat (`sessionId`, `startTime`, `lastUpdated`, `messages`); since v0.39 it is a log: a header
line, then one line per message (a line with an id already seen replaces that message, so a reply is written
again once its `toolCalls` are filled in), `{"$set": ...}` patches (`lastUpdated`, `summary`, a snapshot of
`messages`) and `{"$rewindTo": <id>}` truncations. A message is `user` or `gemini` (`info` and `error` are
notices); its `content` is a string or a list of parts, and the calls it made, with their results and a
`status`, are in `toolCalls`. A subagent's chat is `chats/<parent>/<child>.jsonl` with `kind: "subagent"`. The
folder it ran in is `.project_root` in the project's folder, or the reverse of `projects.json`.

Qwen Code keeps `~/.qwen/projects/<folder>/chats/<session>.jsonl`, one record per line with a Gemini-shaped
`message` (`role` and `parts`: `text`, `thought`, `functionCall`, `functionResponse`) and the `cwd`; a result is
its own `tool_result` record whose `toolCallResult` has a `status`. A subagent is
`projects/<folder>/subagents/<session>/agent-*.jsonl`. Releases before that wrote Gemini CLI's own layout under
`~/.qwen/tmp`.

iFlow keeps `~/.iflow/projects/<folder>/session-<id>.jsonl`: Claude Code's `uuid`/`parentUuid` records with a
`cwd`, `message.content` blocks (`text`, `tool_use`, `tool_result`), where the result's content is an object
holding the tool's `functionResponse`. A streamed reply is written several times under one `parentUuid`.

Format knowledge: deja-vu docs/registry/{gemini,qwen}.md and internal/sources/{gemini,qwen}.go (MIT),
SpecStory's QWEN-FORMAT.md and qwencode provider (Apache-2.0), agentsview's gemini, qwen and iflow parsers
(MIT), franken_agent_detection's qwen and gemini connectors (MIT).
"""

import glob
import json
import os
import re

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

CHAT_SCHEMA = "2026-11-03 new source: Gemini CLI, Qwen Code and iFlow"

# Context the CLI adds to a user turn. A block that is not closed runs to the end of the text.
_CONTEXT = re.compile(r"<(session_context|hook_context)>.*?(?:</\1>|\Z)", re.S)
_EXIT = re.compile(r"^Exit Code: (\d+)\s*$", re.M)
# The lines of a shell result that say nothing about what went wrong.
_SHELL_FRAMING = re.compile(
    r"^(?:Command|Directory|Signal|Background PIDs|Process Group PGID): .*\n?|^\w+: \(none\)\n?", re.M
)
_SHELLS = ("run_shell_command", "shell", "run_command")


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return _d(json.load(f))
    except (OSError, ValueError):
        return {}


def _stat(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def _tmp_chats(key: str) -> list[str]:
    """Gemini CLI's chat files under <root>/tmp, which Qwen's older releases share. A `.json` whose
    `.jsonl` rewrite exists is the stale copy, and a `/chat save` checkpoint is not a chat."""
    out: list[str] = []
    for folder in roots.other_dirs(key):
        base = os.path.join(glob.escape(folder), "tmp", "*", "chats")
        found = set(glob.glob(os.path.join(base, "*.json*")) + glob.glob(os.path.join(base, "*", "*.jsonl")))
        out += sorted(
            p
            for p in found
            if p.endswith((".json", ".jsonl"))
            and not os.path.basename(p).startswith("checkpoint-")
            and not (p.endswith(".json") and p + "l" in found)
        )
    return out


def _qwen_chats() -> list[str]:
    out: list[str] = []
    for folder in roots.other_dirs("qwen"):
        base = os.path.join(glob.escape(folder), "projects", "*")
        out += sorted(
            p
            for p in glob.glob(os.path.join(base, "chats", "*.jsonl"))
            + glob.glob(os.path.join(base, "subagents", "*", "agent-*.jsonl"))
            if not p.endswith(".ledger.jsonl")
        )
    return out + _tmp_chats("qwen")


def _iflow_chats() -> list[str]:
    out: list[str] = []
    for folder in roots.other_dirs("iflow"):
        out += sorted(glob.glob(os.path.join(glob.escape(folder), "projects", "*", "session-*.jsonl")))
    return out


def _detect(key: str, files):
    def detect() -> int | None:
        return len(files()) if roots.other_dirs(key) else None

    return detect


def _items(files, load):
    def items():
        for path in files():
            if fingerprint := _stat(path):
                yield Item(path, fingerprint, lambda p=path: load(p))

    return items


def _context(text: str) -> str:
    """A user turn as the person typed it: the CLI's context blocks and the summary it writes in place of
    a compacted history are not theirs."""
    if text.lstrip().startswith("<state_snapshot>"):
        return ""
    return clean_user_text(_CONTEXT.sub("", text))


def _failure(tool: str, status, body: str) -> str | None:
    """The signature of a failed call, None when it succeeded. A shell command that ran and exited non-zero
    is a success to the CLI, which reports the code in the footer of its output."""
    code = _EXIT.findall(body)
    if status == "error" or (tool in _SHELLS and code and int(code[-1]) > 0):
        return signature(_SHELL_FRAMING.sub("", body)) or "error"
    return None


# --- Gemini CLI -------------------------------------------------------------------------------------------


def _parts_text(content) -> str:
    """The words in a message's content, never a thought or a function call or response."""
    if isinstance(content, str):
        return content
    parts = [p for p in content if isinstance(p, dict) and not p.get("thought")] if isinstance(content, list) else []
    return "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str) and p["text"])


def _replay(path: str) -> tuple[dict, list[dict]]:
    """A JSONL chat's header and its messages as the log leaves them."""
    header: dict = {}
    records: list[dict] = []
    where: dict[str, int] = {}
    calls_of: dict[str, list] = {}  # a reply's toolCalls by id: a later snapshot of the history drops them

    def reindex() -> None:
        where.clear()
        where.update({m["id"]: i for i, m in enumerate(records) if isinstance(m.get("id"), str)})

    for d in read_jsonl(path):
        patch = d.get("$set")
        if "$rewindTo" in d:
            if d["$rewindTo"] in where:
                del records[where[d["$rewindTo"]] :]
                reindex()
        elif isinstance(patch, dict):
            header.update({k: v for k, v in patch.items() if k in ("lastUpdated", "summary")})
            if isinstance(patch.get("messages"), list):
                records = [
                    dict(m, toolCalls=m.get("toolCalls") or calls_of.get(m.get("id")))
                    for m in patch["messages"]
                    if isinstance(m, dict) and m.get("type")
                ]
                reindex()
        elif d.get("type"):
            if d["type"] == "gemini" and d.get("toolCalls") and isinstance(d.get("id"), str):
                calls_of[d["id"]] = d["toolCalls"]
            if d.get("id") in where:
                records[where[d["id"]]] = d
            else:
                if isinstance(d.get("id"), str):
                    where[d["id"]] = len(records)
                records.append(d)
        elif d.get("sessionId"):
            for k, v in d.items():
                if k in ("lastUpdated", "summary") or k not in header:
                    header[k] = v
    return header, records


def _project(path: str, root: str, header: dict) -> str | None:
    """The folder a chat ran in: `.project_root` in its project folder, then the reverse of the root's
    projects.json, then the header's own list of folders. The project folder's name alone (a slug or a hash
    of the path) is never decoded."""
    tmp = os.path.join(root, "tmp")
    rel = os.path.relpath(path, tmp)
    if rel.startswith(os.pardir):
        return None
    project_id = rel.split(os.sep)[0]
    try:
        with open(os.path.join(tmp, project_id, ".project_root"), encoding="utf-8") as f:
            if folder := f.read().strip():
                return folder
    except OSError:
        pass
    for folder, name in _d(_json(os.path.join(root, "projects.json")).get("projects")).items():
        if name == project_id:
            return folder
    directories = header.get("directories")
    return directories[0] if isinstance(directories, list) and directories and isinstance(directories[0], str) else None


def _root_of(path: str, key: str) -> str:
    return next((f for f in roots.other_dirs(key) if path.startswith(f + os.sep)), os.path.dirname(path))


def load_gemini(path: str, key: str = "gemini") -> Session | None:
    if path.endswith(".json"):
        header = _json(path)
        records = [m for m in header.get("messages") or [] if isinstance(m, dict)]
    else:
        header, records = _replay(path)
    sid = header.get("sessionId")
    if not isinstance(sid, str) or not sid:  # not a chat: a checkpoint, or a log with no header
        return None
    s = Session(key, sid, path)
    s.root = key
    s.project = _project(path, _root_of(path, key), header)
    summary = header.get("summary")
    s.title = summary.strip() or None if isinstance(summary, str) else None
    s.started_at, s.updated_at = iso_ts(header.get("startTime")), iso_ts(header.get("lastUpdated"))
    if header.get("kind") == "subagent" and os.path.basename(os.path.dirname(os.path.dirname(path))) == "chats":
        s.origin, s.parent = "subagent", f"{key}:{os.path.basename(os.path.dirname(path))}"

    messages: list[Message] = []
    calls = Calls()
    for m in records:
        ts = iso_ts(m.get("timestamp"))
        text = _parts_text(m.get("content"))
        if m.get("type") == "user":
            append(messages, "user", _context(text), ts)
        elif m.get("type") in ("gemini", "model"):
            append(messages, "assistant", clean_assistant_text(text), ts)
            for c in m.get("toolCalls") or []:
                if isinstance(c, dict) and c.get("status") != "cancelled":
                    _gemini_call(c, messages, calls, ts)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _gemini_call(c: dict, messages: list[Message], calls: Calls, ts) -> None:
    results = [_d(_d(r).get("functionResponse")).get("response") for r in c.get("result") or []]
    body = "\n".join(str(_d(r).get("error") or _d(r).get("output") or "") for r in results)
    tool = c.get("name") or ""
    calls.add(*position(messages), ts, tool, c.get("args"), c.get("id"), _failure(tool, c.get("status"), body))


# --- Qwen Code --------------------------------------------------------------------------------------------


def load_qwen(path: str) -> Session | None:
    if os.path.relpath(path, _root_of(path, "qwen")).startswith(
        "tmp" + os.sep
    ):  # Qwen's older releases wrote Gemini CLI's layout
        return load_gemini(path, "qwen")
    stem = os.path.basename(path)[: -len(".jsonl")]
    parent = os.path.basename(os.path.dirname(path))
    subagent = os.path.basename(os.path.dirname(os.path.dirname(path))) == "subagents"
    s = Session("qwen", f"{stem}-{parent}" if subagent else stem, path)
    s.root = "qwen"
    if subagent:
        s.origin, s.parent = "subagent", f"qwen:{parent}"
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    for d in read_jsonl(path):
        kind, ts = d.get("type"), iso_ts(d.get("timestamp"))
        s.project = s.project or d.get("cwd") or None
        s.branch = d.get("gitBranch") or s.branch
        if kind in ("user", "assistant", "tool_result"):
            s.started_at = s.started_at or ts
            s.updated_at = ts or s.updated_at
        parts = [p for p in _d(d.get("message")).get("parts") or [] if isinstance(p, dict)]
        if kind == "user" and d.get("provenance") in (None, "real_user"):
            append(messages, "user", _context(_parts_text(parts)), ts)
        elif kind == "assistant":
            append(messages, "assistant", clean_assistant_text(_parts_text(parts)), ts)
            for p in parts:
                call = _d(p.get("functionCall"))
                if call.get("id"):
                    pending[call["id"]] = (call.get("name") or "", call.get("args"))
        elif kind == "tool_result":
            result = _d(d.get("toolCallResult"))
            response = _d(next((_d(p.get("functionResponse")) for p in parts if p.get("functionResponse")), {}))
            call_id = result.get("callId") or response.get("id")
            if call_id in pending:
                tool, args = pending.pop(call_id)
                out = _d(response.get("response"))
                body = str(out.get("error") or out.get("output") or "")
                calls.add(*position(messages), ts, tool, args, call_id, _failure(tool, result.get("status"), body))
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


# --- iFlow ------------------------------------------------------------------------------------------------


def _result(block: dict) -> tuple[bool, str]:
    """(failed, text) of a tool_result block. iFlow's content is an object holding the function response
    (`{"callId", "responseParts": {"functionResponse": {"response": {"output" | "error"}}}}`); Claude's is
    a string or a list of text blocks."""
    content = block.get("content")
    if isinstance(content, dict):
        response = _d(_d(_d(content.get("responseParts")).get("functionResponse")).get("response"))
        error = response.get("error")
        return bool(block.get("is_error") or error), str(error or response.get("output") or "")
    return bool(block.get("is_error")), content if isinstance(content, str) else text_blocks(content, ("text",))


def load_iflow(path: str) -> Session | None:
    s = Session("iflow", os.path.basename(path)[len("session-") : -len(".jsonl")], path)
    s.root = "iflow"
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    last: tuple | None = None  # (parentUuid, text) of the last reply: a streamed one is written again
    for d in read_jsonl(path):
        kind, ts = d.get("type"), iso_ts(d.get("timestamp"))
        if kind not in ("user", "assistant") or d.get("isMeta") or d.get("isCompactSummary") or d.get("isSidechain"):
            continue
        s.project = s.project or d.get("cwd") or None
        s.branch = d.get("gitBranch") or s.branch
        s.started_at = s.started_at or ts
        s.updated_at = ts or s.updated_at
        content = _d(d.get("message")).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                pending[b["id"]] = (b.get("name") or "", b.get("input"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                tool, args = pending.pop(b["tool_use_id"])
                failed, body = _result(b)
                calls.add(
                    *position(messages),
                    ts,
                    tool,
                    args,
                    b["tool_use_id"],
                    _failure(tool, "error" if failed else None, body),
                )
        text = text_blocks(content, ("text",))
        if kind == "user":
            append(messages, "user", clean_user_text(text), ts)
        elif (d.get("parentUuid"), text) != last:
            append(messages, "assistant", clean_assistant_text(text), ts)
            last = (d.get("parentUuid"), text)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


AGENTS = {
    "gemini": (
        "Gemini CLI",
        _detect("gemini", lambda: _tmp_chats("gemini")),
        _items(lambda: _tmp_chats("gemini"), load_gemini),
    ),
    "qwen": ("Qwen Code", _detect("qwen", _qwen_chats), _items(_qwen_chats, load_qwen)),
    "iflow": ("iFlow", _detect("iflow", _iflow_chats), _items(_iflow_chats, load_iflow)),
}
