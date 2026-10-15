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

from .redact import redact

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
        return {name: getattr(self, name) for name in _STORED}

    @classmethod
    def load(cls, d) -> "ToolError | None":
        """A stored error, ignoring fields this version does not know; None if it is not one."""
        if not isinstance(d, dict) or not all(k in d for k in ("call_id", "tool", "signature")):
            return None
        return cls(**{k: d[k] for k in _STORED if k in d})


_STORED = tuple(f.name for f in fields(ToolError))[:8]


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


def anchor(errors: list[ToolError], messages, keep: list[int], stored) -> list[ToolError]:
    """The parse's errors, ready to store. messages: the parse; keep: the indexes of those that
    are stored; stored: their stored (truncated, redacted) form, in the same order. Each error
    gets the identity of its message and of the message where a rerun succeeded, and its next
    reply is cut from the stored message, so a secret redaction removed stays removed."""
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
                e.command,
                e.signature,
                _next_reply(e, k, messages, where, stored),
                identity(stored[k].role, stored[k].text) if k is not None else None,
                identity(stored[r].role, stored[r].text) if r is not None else None,
            )
        )
    return out


def _next_reply(e: ToolError, k: int | None, messages, where: dict[int, int], stored) -> str:
    if e.reply is not None:
        return clean_prose(e.reply)
    if k is None or e.at is None:
        return ""
    # Words the agent added to that same message after the call. The split is where the stored
    # (redacted) text of what came before it ends; if redaction changed that, fall back.
    kept = stored[k].text
    raw = messages[e.at]
    if raw.role == "assistant" and len(raw.text) > e.at_len:
        head = redact(raw.text[: e.at_len]).rstrip()
        if kept.startswith(head) and len(kept) > len(head):
            return clean_prose(kept[len(head) :])
    nxt = next((m.text for m in stored[k + 1 :] if m.role == "assistant"), "")
    return clean_prose(nxt)


# --- redaction -----------------------------------------------------------------

_VALUE = r"""(?:"[^"]*"|'[^']*'|[^\s"']+)"""
_NAME = (
    r"[\w.-]*(?:pass(?:word|wd|phrase)?|pwd|secret|token|auth|cred(?:ential)?s?|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|key|cookie|bearer|salt|dsn)[\w.-]*"
)
_REDACTED = "[REDACTED]"

# A command line, an output or an agent's words can carry a secret the index's redactor was not
# written for. Each pattern keeps what is around the secret so the line still reads.
_COMMAND = [
    # curl -u user:pass, --user user:pass, -u'user:pass'
    (re.compile(r"""(?<!\S)(-u|--user|--proxy-user)(\s*=?\s*["']?)[^\s:"']+:[^\s"']*"""), r"\1\2" + _REDACTED),
    # sshpass -p secret
    (re.compile(rf"(?i)\b(sshpass\s+-p\s*){_VALUE}"), r"\1" + _REDACTED),
    # openssl -passin pass:secret
    (re.compile(r"\bpass:(?=\S)\S+"), "pass:" + _REDACTED),
    # --password x, --token=x, --api-key x
    (
        re.compile(
            rf"""(?i)(--[\w-]*(?:pass(?:word|wd|phrase)?|pwd|secret|token|api-?key|auth|credentials?|cookie|bearer)"""
            rf"""[\w-]*)((?:\s+|=)(?!-)){_VALUE}"""
        ),
        r"\1\2" + _REDACTED,
    ),
    # Authorization: Bearer x, X-Api-Key: x, Cookie: x
    (
        re.compile(
            r"""(?i)\b(authorization|proxy-authorization|x-api-key|x-auth-token|x-access-token|api-key|cookie|set-cookie)"""
            r"""(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s"'][^"'\n]*"""
        ),
        r"\1\2" + _REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 " + _REDACTED),
    # a webhook URL is a credential
    (
        re.compile(
            r"""(?i)https?://[^\s"'<>]*(?:hooks\.slack\.com|discord(?:app)?\.com/api/webhooks|hooks\.zapier\.com|"""
            r"""webhook|outlook\.office\d*\.com/|api\.telegram\.org/bot|events\.pagerduty\.com)[^\s"'<>]*"""
        ),
        _REDACTED,
    ),
    # https://user:pass@host and https://<token>@host
    (re.compile(r"""\b([a-z][a-z0-9+.-]*://)[^\s/@:"']+:[^\s/@"']+@"""), r"\1" + _REDACTED + "@"),
    (re.compile(r"""\b(https?://)[^\s/@:"']{16,}@"""), r"\1" + _REDACTED + "@"),
    # redis-cli -a secret
    (re.compile(rf"(?i)\b(redis-cli\b[^\n]*?\s-a\s+){_VALUE}"), r"\1" + _REDACTED),
    # aws configure set aws_secret_access_key x, npm config set //registry/:_authToken x
    (re.compile(rf"(?i)\b(set\s+\S*(?:secret|token|pass(?:word|wd)?|key|auth)\S*\s+){_VALUE}"), r"\1" + _REDACTED),
    # mysql -pSECRET
    (
        re.compile(r"(?i)\b(mysql\w*|mariadb\w*|psql|mongo\w*)\b([^\n]*?(?<![\w-])-p)(?=[^\s'\"-])[^\s'\"]+"),
        r"\1\2" + _REDACTED,
    ),
    # "the password is hunter2hunter2": a word for a secret, then a value with a digit in it
    (
        re.compile(
            r"""(?i)\b(password|passwd|passphrase|secret|token|api[ _-]?key|credentials?)(\s+(?:is\s+|was\s+)?)"""
            r"""(?=[^\s"']*\d)[^\s"']{8,}"""
        ),
        r"\1\2" + _REDACTED,
    ),
    # a long run of mixed-case letters and digits, such as a key pasted where a command goes
    (
        re.compile(
            r"(?<![A-Za-z0-9/._+-])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*\d)"
            r"[A-Za-z0-9_-]{32,}(?![A-Za-z0-9/._+-])"
        ),
        _REDACTED,
    ),
    # DB_PASS=x, NPM_AUTH=x, ?token=x
    (re.compile(rf"""(?i)(?<![\w.-])({_NAME})(=)(?!\[REDACTED\]){_VALUE}"""), r"\1\2" + _REDACTED),
]
# `-p secret` is also `mkdir -p dir` and `ssh -p 22`: a port or a path stays, `mkdir -p build` does not.
_P_FLAG = re.compile(rf"(?<!\S)(-p\s+)(?!-)({_VALUE})")


def _p_flag(m: re.Match) -> str:
    value = m.group(2).strip("'\"")
    if re.fullmatch(r"\d{1,5}(:\d{1,5})?", value) or "/" in value or value == _REDACTED:
        return m.group(0)  # a port, a mapping or a path
    return m.group(1) + _REDACTED


def redact_command(text: str) -> str:
    """text with what the index's redactor misses taken out: the secrets a command line, the
    output that echoes it or an agent quoting it carry."""
    for pattern, repl in _COMMAND:
        text = pattern.sub(repl, text)
    return _P_FLAG.sub(_p_flag, text)


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


# a private key whose end was cut away
_OPEN_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*\Z", re.S)


def clean(text: str) -> str:
    """text with secrets taken out, whole: the index's redactor, then the command pass. A reply
    quotes commands and an output echoes them, so every field gets the command rules."""
    return redact_command(_OPEN_KEY.sub(_REDACTED, redact(_look(text))))


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
