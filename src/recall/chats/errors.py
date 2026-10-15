"""Failed tool calls, kept as short signatures so a new failure can find the chats where
the same error came up before.

The index drops tool output (it would swamp search), so a past error used to be findable
only where someone pasted it. Each source notes every tool call it sees and `Calls.errors`
turns the failed ones into ToolErrors: the tool, the head of its command, one line that says
what went wrong, whether a later run of the identical call in that chat succeeded, and the
start of what the agent said next. They go into their own table (store.tool_errors), never
into messages, so search ranking and memory extraction never see them.

Three rules keep the table honest.

* Where a failure sits is a message's identity (its role and a hash of its stored text), never
  a position: positions move when the shrink guard keeps messages a file dropped, when copies
  of one chat are unioned and when blank messages are dropped. store._write turns the identity
  into a seq against the list it actually writes, or into NULL when it cannot do so exactly.
* A later run "resolves" a failure only when it is the identical call: the same tool and a hash
  of the whole input. The head of a command is for display, and many different commands share
  one.
* Text is redacted whole, then cut: a cut can leave half a secret that no pattern recognises.
"""

import hashlib
import json
import re
from bisect import bisect_right
from dataclasses import dataclass, fields

from .redact import Chat, redact, redact_tool

SIGNATURE_CHARS = 200
COMMAND_CHARS = 120
_LOOK = 6_000  # characters, from each end, of a command, output or reply that redaction and line picking read


@dataclass
class ToolError:
    # What is stored (the archive copy, the tool_errors table).
    call_id: str  # the agent's own id for the call; Cursor's is the bubble id
    ts: float | None
    tool: str
    command: str  # the head of the call, redacted
    signature: str  # redacted, one line
    next_reply: str = ""  # the start of what the agent said after the failure, redacted
    anchor: str | None = None  # identity of the message the failure came after
    resolved_anchor: str | None = None  # identity of the message where a rerun of the call succeeded
    # Known only while a chat is parsed: a source sets them, store.anchor turns them into the above.
    at: int | None = None  # index in the parsed messages (None: the source cannot say)
    at_len: int = 0  # how long that message was then, since the agent's later words join it
    resolved_at: int | None = None
    reply: str | None = None  # the agent's next words, when the source has them apart from messages

    def dump(self) -> dict:
        """What is stored. The words are redacted again here, so no copy of a failure holds what the rules catch."""
        d = {name: getattr(self, name) for name in _STORED}
        for k in TEXT_FIELDS:
            d[k] = reclean(k, d[k])
        return d

    @classmethod
    def load(cls, d) -> "ToolError | None":
        """A stored error, ignoring fields this version does not know; None if it is not one."""
        if not isinstance(d, dict) or not all(k in d for k in ("call_id", "tool", "signature")):
            return None
        return cls(**{k: d[k] for k in _STORED if k in d})


_STORED = tuple(f.name for f in fields(ToolError))[:8]
TEXT_FIELDS = ("command", "signature", "next_reply")  # the words of a failure; the rest are ids and anchors


def reclean(field: str, text, chat: Chat | None = None):
    """A stored field, redacted again (a rule was added, or the chat's values are known now). The command and the
    signature are single lines, as they were when the rules read them. A reply was several lines then and has
    been joined since, which TOOL_RULES that stop at a line end (`Authorization:`, `mysql ... -p`) would read too
    far, so it gets the rules of redact() and the chat's values only: a tool rule added later does not reach it."""
    if not isinstance(text, str) or not text:
        return text
    if field == "next_reply":
        return chat.repeats(redact(text)) if chat else redact(text)
    return chat.tool(text) if chat else redact_tool(text)


@dataclass
class Call:
    """One finished tool call as a source sees it, in chat order."""

    at: int | None
    at_len: int
    ts: float | None
    tool: str
    command: str  # the head, redacted; only kept for a failure
    key: str  # identical calls share it: the tool and a hash of the whole input
    call_id: str
    failure: str | None  # its redacted signature, or None when it succeeded
    reply: str | None = None


class Calls:
    """The tool calls of one chat. Only a failure needs its command head, and only a call after
    the first failure can resolve one, so the rest cost nothing."""

    def __init__(self) -> None:
        self.items: list[Call] = []
        self._failed = False

    @property
    def failed(self) -> bool:
        """Whether a call has failed yet: from then on every call may be the one that fixed it."""
        return self._failed

    def add(self, at, at_len, ts, tool, args, call_id, failure=None, reply=None) -> None:
        self._failed = self._failed or failure is not None
        self.items.append(
            Call(
                at,
                at_len,
                ts,
                tool,
                describe(args) if failure is not None else "",
                fingerprint(tool, args) if self._failed else "",
                str(call_id or ""),
                failure,
                reply,
            )
        )

    def errors(self) -> list[ToolError]:
        """The failed calls. A failure is resolved by the first later call in the chat that is
        identical to it and succeeded."""
        later: dict[str, list[int]] = {}  # key -> the indexes of the successes, in order
        for i, c in enumerate(self.items):
            if c.failure is None and c.key:
                later.setdefault(c.key, []).append(i)
        out = []
        for i, c in enumerate(self.items):
            if c.failure is None:
                continue
            ahead = later.get(c.key, [])
            n = bisect_right(ahead, i) if c.key else len(ahead)
            fix = self.items[ahead[n]] if n < len(ahead) else None
            out.append(
                ToolError(
                    c.call_id,
                    c.ts,
                    c.tool,
                    c.command,
                    c.failure,
                    "",
                    None,
                    None,
                    c.at,
                    c.at_len,
                    fix and fix.at,
                    c.reply,
                )
            )
        return out


def position(messages) -> tuple[int, int]:
    """(index, text length) of the last message so far: where a tool call happens."""
    return len(messages) - 1, len(messages[-1].text) if messages else 0


# --- where a failure sits ----------------------------------------------------


def identity(role: str, text: str) -> str:
    """A message, whichever list it is in: its role and a hash of its words."""
    return role[:1] + hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:12]


def anchor(errors: list[ToolError], messages, keep: list[int], stored, chat: Chat | None = None) -> list[ToolError]:
    """The parse's errors, ready to store. messages: the parse; keep: the indexes of those that
    are stored; stored: their stored (truncated, redacted) form, in the same order. Each error
    gets the identity of its message and of the message where a rerun succeeded, and its next
    reply is cut from the stored message, so a secret redaction removed stays removed.
    chat: what redacted the messages (redact.Chat); the words of the errors are redacted with
    the values it held, as the messages are."""
    where = {i: k for k, i in enumerate(keep)}
    out = []
    for e in errors:
        k = where.get(e.at) if e.at is not None else None
        r = where.get(e.resolved_at) if e.resolved_at is not None else None
        out.append(
            ToolError(
                e.call_id,
                e.ts,
                e.tool,
                chat.tool(e.command) if chat else e.command,
                chat.tool(e.signature) if chat else e.signature,
                _next_reply(e, k, messages, where, stored, chat),
                identity(stored[k].role, stored[k].text) if k is not None else None,
                identity(stored[r].role, stored[r].text) if r is not None else None,
            )
        )
    return out


def _next_reply(e: ToolError, k: int | None, messages, where: dict[int, int], stored, chat: Chat | None) -> str:
    if e.reply is not None:
        return clean_prose(chat.tool(e.reply) if chat else e.reply)
    if k is None or e.at is None:
        return ""
    # Words the agent added to that same message after the call. The split is where the stored
    # (redacted) text of what came before it ends; if redaction changed that, fall back.
    kept = stored[k].text
    raw = messages[e.at]
    if raw.role == "assistant" and len(raw.text) > e.at_len:
        head = redact(raw.text[: e.at_len])
        head = (chat.repeats(head) if chat else head).rstrip()
        if kept.startswith(head) and len(kept) > len(head):
            return clean_prose(kept[len(head) :])
    nxt = next((m.text for m in stored[k + 1 :] if m.role == "assistant"), "")
    return clean_prose(nxt)


# --- redaction -----------------------------------------------------------------


def _look(text: str) -> str:
    """The part of a long text that is read: the start and the end (where tracebacks end), each
    cut at a line so no line is read half."""
    text = text or ""
    if len(text) <= 2 * _LOOK:
        return text
    head, tail = text[:_LOOK], text[-_LOOK:]
    head = head[: head.rfind("\n")] if "\n" in head else head
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    return head + "\n" + tail


def clean(text: str) -> str:
    """text with secrets taken out, whole: the index's redactor, then the rules for a command line
    (redact.TOOL_RULES). A reply quotes commands and an output echoes them, so every field gets both."""
    return redact_tool(_look(text))


def clean_prose(text: str) -> str:
    """Whitespace-collapsed, redacted whole, then cut."""
    return " ".join(clean(text).split())[:SIGNATURE_CHARS]


# --- reading a call --------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_TAGS = re.compile(r"</?(tool_use_error|error)>")
# Lines that frame output rather than say what failed.
_NOISE = re.compile(
    r"^(exit code:? -?\d+|process exited with code -?\d+|wall time:?.*|chunk id:.*|original token count:.*|"
    r"output:|script (failed|completed).*|traceback \(most recent call last\):|file \".*\", line \d+.*|"
    r"\^+|[-=~_*#]{3,}|\(?no output\)?|at .+:\d+(:\d+)?\)?|>.*|\$ .*)$",
    re.I,
)
_ERRORISH = re.compile(
    r"\b(error|errors|exception|failed|failure|fatal|denied|not found|no such|cannot|can't|could not|"
    r"couldn't|unable|invalid|refused|timed? ?out|panic|abort(ed)?|missing|undefined|unknown|unexpected|"
    r"not allowed|permission|conflict|rejected|does not exist|doesn't exist|ENOENT|EACCES|EPERM)\b|"
    r"\w+(Error|Exception)\b",
    re.I,
)
_PY_EXCEPTION = re.compile(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b")
# A line that opens like an error message, which beats one that merely mentions an error.
_LEADS = re.compile(
    r"^(\[?[\w.]*(Error|Exception)\]?(:|\s|$)|(npm )?ERR!|error\b|fatal\b|failed\b|failure\b|panic\b|"
    r"\S+:\d+(:\d+)?:? (error|fatal)\b|\S+\(\d+,\d+\): error\b)",
    re.I,
)
# What Claude Code returns when the person declined a tool call: not a failure of the tool.
_DECLINED = re.compile(
    r"^\s*(\[?Request interrupted by user|The user doesn't want to (proceed|take this action)|"
    r"The user (has )?(rejected|declined|denied))|tool use was rejected",
    re.I,
)


def declined(text: str) -> bool:
    return bool(_DECLINED.search((text or "")[:300]))


def signature(text: str) -> str:
    """The line that says what went wrong: a Python traceback's exception line, else the
    first line that opens like an error, else the first that mentions one, else the first
    line with any content. The output is redacted whole before a line is picked or cut."""
    text = _TAGS.sub("", _ANSI.sub("", clean(text)))
    every = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in every if line and not _NOISE.match(line)]
    if not lines:  # only framing, such as a bare "Exit code 1": that is the signature
        return next((line for line in every if line), "")[:SIGNATURE_CHARS]
    pick = None
    if "Traceback (most recent call last)" in text:
        pick = next((line for line in reversed(lines) if _PY_EXCEPTION.match(line)), None)
    pick = (
        pick
        or next((line for line in lines if _LEADS.match(line)), None)
        or next((line for line in lines if _ERRORISH.search(line)), lines[0])
    )
    if pick.endswith(":") and (i := lines.index(pick)) + 1 < len(lines):
        pick = f"{pick} {lines[i + 1]}"  # a heading such as "Invalid arguments:": add what follows
    return pick[:SIGNATURE_CHARS]


def command_head(command) -> str:
    """The first line of a command, or of whatever argument says what the call was for. The
    command is redacted whole first."""
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    text = clean(str(command or ""))
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:COMMAND_CHARS]


_ARGUMENTS = ("command", "cmd", "file_path", "path", "target_file", "targetFile", "relativeWorkspacePath")
_OTHERS = ("notebook_path", "pattern", "globPattern", "query", "url", "effectiveUri")


def describe(tool_input) -> str:
    """The head of the argument that says what a call was for: its command, file, pattern or URL."""
    if not isinstance(tool_input, dict):
        return command_head(tool_input)
    for key in (*_ARGUMENTS, *_OTHERS):
        if tool_input.get(key):
            return command_head(tool_input[key])
    return ""


# Fields that change between two runs of one call without making it another call: a note on why, a time limit.
_NOT_THE_CALL = {
    "description",
    "explanation",
    "justification",
    "timeout",
    "yield_time_ms",
    "max_output_tokens",
    "run_in_background",
    "is_background",
    "sandbox_permissions",
    "prefix_rule",
    "dangerouslyDisableSandbox",
}


def fingerprint(tool: str, tool_input) -> str:
    """Identical calls share it: the tool and the whole command (and the folder it ran in), or
    for a call that is no command the whole input, whitespace and the notes in _NOT_THE_CALL
    aside. Never stored."""
    if isinstance(tool_input, dict):
        command = next((tool_input[k] for k in ("command", "cmd") if tool_input.get(k)), None)
        if command is not None:
            tool_input = [command, tool_input.get("workdir") or tool_input.get("cwd")]
        else:
            tool_input = {k: v for k, v in tool_input.items() if k not in _NOT_THE_CALL}
    if not isinstance(tool_input, str):
        tool_input = json.dumps(tool_input, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(f"{tool}\0{' '.join(tool_input.split())}".encode()).hexdigest()[:16]
