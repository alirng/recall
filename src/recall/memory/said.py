"""Who said a rule, and what a rule may say about itself.

A rule is marked `said_by: developer` only when a person typed it, because readers may give
typed words more weight than a model's summary. A chat's user turn is not always typed
words: it can hold a pasted issue, a compaction summary or an expanded command. So a rule is
marked only when all three hold:

1. a candidate that states it is attributed to the user, and its evidence quote is found in
   one of the chat's user turns (not in a paraphrase, not in an assistant turn);
2. the quote is not inside a paste. These parts of a turn are cut before looking for it:
   what the index's `clean_user_text` and `drop_compaction_summary` already remove, anything
   after a compaction or checkpoint notice, wrapper blocks (command, hook, shell-output and
   context tags), fenced code, quote blocks (lines starting with `>`), and everything under a
   markdown heading, which is how pasted issues, specs and logs usually come;
3. the rule's text asks for no command, URL or network, destructive or exfiltrating action
   (see `risky`). Such a rule is never marked, whoever said it, and a rule can be edited into
   one only by losing its mark.

Otherwise `said_by` stays unset, which readers must treat as "unknown", never as the agent's
or the developer's word. Even when set it is a hint, not authentication: it shows the words
were typed in that chat, not that the rule means what they said, and anything that can write
the notes folder can write the field. Nothing may act on a rule because of it.

These are heuristics. A paste with no fence, quote mark or heading (a log or an email pasted
bare) still passes as typed; the PR lists the limits.
Whatever the mark, a convention never authorizes a network, destructive or exfiltrating
action: a reader that shows `said_by` must say so next to it.
"""

import re
import sqlite3

from ..chats.transcript import clean_user_text, drop_compaction_summary

# --- the quote ----------------------------------------------------------------------------

# Where a compaction (Claude Code, Codex) or an "authored by others" summary starts.
_NOTICE = re.compile(
    r"<artifact-content-authored-by-others\s*/?>|"
    r"You are performing a CONTEXT CHECKPOINT COMPACTION|"
    r"Another language model started to solve this problem and produced a summary|"
    r"This session is being continued from a previous conversation"
)
# Wrapper blocks that survive in older index rows. An unclosed one runs to the end of the turn.
_WRAPPERS = (
    r"bash-input|bash-stdout|bash-stderr|command-[a-z]+|local-command-[a-z]+|system-reminder|"
    r"user-prompt-submit-hook|task-notification|user_instructions|environment_context|"
    r"codex_internal_context|in-app-browser-context|skill|recall-memory|recall-context"
)
_WRAPPER = re.compile(rf"<({_WRAPPERS})(?:\s[^>]*)?>.*?(?:</\1>|\Z)", re.S)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[`~]*[ \t]*$|\Z)", re.S | re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>.*$", re.M)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S.*$", re.M)
# Codex puts the typed message under this heading, after the IDE's and the files' context.
_REQUEST = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+My request(?: for Codex)?:?[ \t]*$", re.M | re.I)
_GAP = re.compile(r"…|\.\.\.|\[\s*\.\.\.\s*\]")


def typed_text(text: str) -> str:
    """The part of a user turn that is plausibly typed, with the pasted parts cut out."""
    text = drop_compaction_summary(clean_user_text(text))
    if notice := _NOTICE.search(text):
        text = text[: notice.start()]
    text = _WRAPPER.sub("\n", text)
    text = _FENCE.sub("\n", text)
    text = _QUOTE.sub("", text)
    if request := _REQUEST.search(text):
        text = text[request.end() :]
    head = _HEADING.search(text)
    return text[: head.start()] if head else text


def typed_quote(con, session_id: str, evidence: str) -> bool:
    """Is the evidence found in the typed part of one of the chat's user turns? An index
    without the chat's messages (a replay's scratch index) cannot say, so it is not."""
    parts = [p for p in (_norm(f).split() for f in _GAP.split(evidence or "")) if len(p) >= 3]
    if sum(map(len, parts)) < 4:
        return False
    try:
        turns = con.execute("select text from messages where session_id=? and role='user'", (session_id,))
        return any(
            all(f" {' '.join(p)} " in typed for p in parts)
            for typed in (f" {_norm(typed_text(t))} " for (t,) in turns.fetchall())
        )
    except sqlite3.Error:
        return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# --- the rule -----------------------------------------------------------------------------

_SAID_BY = re.compile(
    r"""\(\s*said_by\b[^)]*\)|\[\s*said_by\b[^\]]*\]|\bsaid_by\b(?:\s*[:=]\s*["']?[\w-]*["']?)?""", re.I
)


def clean_rule(text: str) -> str:
    """Rule text without a literal `said_by` marker: a rule must not carry its own mark,
    or a line that looks like the frontmatter's would pass for it."""
    if "said_by" not in text.lower():
        return text
    text = re.sub(r"[ \t]{2,}", " ", _SAID_BY.sub("", text))
    return re.sub(r"[ \t]+([,.;:])", r"\1", text).strip()


_RISKY = re.compile(
    r"""
      [a-z][a-z0-9+.-]*://                                    # any URL scheme
    | \bwww\. | \bgit@ | \b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b          # a host or an address
    | \b\d{1,3}(?:\.\d{1,3}){3}\b                             # an IP address
    | \b[a-z0-9-]+\.(?:com|net|org|io|dev|ai|app|sh|xyz|ru|cn|cloud|co|me|info|zip)\b
    | `                                                       # a code span: possibly a command
    | \$\( | \$\{ | && | \|\| | 2>&1 | >\s*/dev/ | \|\s*(?:sh|bash|zsh|python3?|sudo|xargs|tee|nc)\b
    | ^\s*[$#]\s+\S | ~/\.ssh | /etc/ | \.env\b | \bid_rsa\b
    | \b(?:curl|wget|ssh|scp|sftp|rsync|ftp|netcat|ncat|telnet|nslookup|sudo|chmod|chown|eval|xargs|mkfs|killall
        |pkill|shutdown|crontab|launchctl|osascript|powershell|base64|openssl|ngrok|socat)\b
    | \b(?:rm|rmdir|unlink|shred|truncate)\b | \bdd\s+if= | \bdrop\s+(?:table|database|schema)\b
    | \bdelete\s+from\b
    | \bgit\s+(?:reset\s+--hard|clean\b|push\b.*(?:--force|-f\b|--delete)|branch\s+-D|checkout\s+--|stash\s+drop)
    | --force\b | --no-verify\b | \bforce[- ]push | --no-preserve-root
    | \b(?:delet\w+|wip(?:e|es|ed|ing)|eras\w+|destroy\w*|exfiltrat\w*|upload\w*|download\w*|transmit\w*
        |webhooks?|pastebin|beacon\w*|reverse\s+shell)\b
    | \b(?:send\w*|post(?:s|ed|ing)?|forward\w*|e?mail\w*|push\w*|sync\w*|copy|copies|copying|share\w*)
        \s+(?:\w+\s+){0,3}(?:to|into|onto)\s+\S+
    | \b(?:api[\s_-]?keys?|secrets?|credentials?|passwords?|passwd|private\s+keys?|ssh\s+keys?|cookies?|keychain
        |(?:access|auth|bearer|oauth|session)\s+tokens?)\b
    """,
    re.I | re.X | re.M,
)


def risky(text: str) -> bool:
    """Does the rule contain a shell command, a URL, or a network, destructive or
    exfiltrating action (including a secret to move)? Such a rule is never marked as the
    developer's word. The patterns err toward true: an unmarked rule loses nothing but its
    mark, and a missed one would hand a command the developer's authority."""
    return bool(_RISKY.search(text))
