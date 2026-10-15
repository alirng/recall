"""Who said a rule, and what a rule may say about itself.

A rule is marked `said_by: developer` only when a person typed it, because readers may give
typed words more weight than a model's summary. A chat's user turn is not always typed
words: it can hold a pasted issue, a compaction summary or an expanded command. So a rule is
marked only when all four hold:

1. a candidate that states it is attributed to the user, and its evidence quote is found in
   one of the chat's user turns (not in a paraphrase, not in an assistant turn). Every piece
   of a quote with "..." gaps must be found, and one piece must run to six words; a quote
   without gaps needs four;
2. the quote is not inside a paste. These parts of a turn are cut before looking for it:
   what the index's `clean_user_text` and `drop_compaction_summary` already remove, anything
   after a compaction or checkpoint notice, HTML comments, wrapper blocks (command, hook,
   shell and tool output, document, pasted text, IDE selection and context tags), fenced and
   indented code, quote blocks (lines starting with `>`), and everything under a markdown
   heading, which is how pasted issues, specs and logs usually come. Codex's "My request"
   heading is the one exception, and only when the turn opens with a heading of its own (the
   IDE context block Codex adds), never inside a paste;
3. the quote supports this rule: most of the rule's content words are in it (see `supports`).
   One typed quote does not vouch for the other rules a model wrote beside it;
4. the rule's text asks for no command, URL or network, destructive or exfiltrating action,
   or for switching off a sandbox, permission or review (see `risky`). Such a rule is never
   marked, whoever said it, and a rule can be edited into one only by losing its mark.

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
    r"codex_internal_context|in-app-browser-context|skill|recall-memory|recall-context|"
    r"documents?|pasted[_-](?:text|content)|function_(?:results|calls)|tool_(?:result|use)|"
    r"ide_selection|ide_opened_file|attached_file|attachment|file_content"
)
_WRAPPER = re.compile(rf"<({_WRAPPERS})(?:\s[^>]*)?>.*?(?:</\1>|\Z)", re.S)
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[`~]*[ \t]*$|\Z)", re.S | re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>.*$", re.M)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S.*$", re.M)
# Codex puts the typed message under this heading, after the IDE's and the files' context.
_REQUEST = re.compile(r"^[ \t]{0,3}#{1,2}[ \t]+My request(?: for Codex)?:?[ \t]*$", re.M | re.I)
_GAP = re.compile(r"…|\.\.\.|\[\s*\.\.\.\s*\]")


def typed_text(text: str) -> str:
    """The part of a user turn that is plausibly typed, with the pasted parts cut out."""
    text = drop_compaction_summary(clean_user_text(text))
    if notice := _NOTICE.search(text):
        text = text[: notice.start()]
    text = _COMMENT.sub("\n", text)
    text = _WRAPPER.sub("\n", text)
    text = _FENCE.sub("\n", text)
    text = _cut_indented(text)
    text = _QUOTE.sub("", text)
    # Codex's own context block opens the turn with a heading. A "My request" heading further
    # into a turn that opens with typed words belongs to something pasted, so it counts for nothing.
    if text.lstrip().startswith("#") and (request := _REQUEST.search(text)):
        text = text[request.end() :]
    head = _HEADING.search(text)
    return text[: head.start()] if head else text


def _cut_indented(text: str) -> str:
    """Markdown indented code: lines indented four spaces or a tab, from the start of the text or
    after a blank line, until the next line that is not indented."""
    out, in_block, after_blank = [], False, True
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            after_blank = True
            continue
        if line.startswith(("    ", "\t")) and (in_block or after_blank):
            in_block = True
            out.append("")
        else:
            in_block = False
            out.append(line)
        after_blank = False
    return "\n".join(out)


WHOLE_QUOTE_WORDS = 4  # a quote without gaps is verified by finding all of it
QUOTE_PIECE_WORDS = 6  # a quote with gaps: every piece is found, and one of them is this long


def typed_quote(con, session_id: str, evidence: str) -> bool:
    """Is the evidence found in the typed part of one of the chat's user turns? Every piece of a
    quote with "..." gaps must be found, and one of them must be long enough to mean something:
    a short piece left unchecked could be the part that matters. An index without the chat's
    messages (a replay's scratch index) cannot say, so it is not."""
    parts = [p for p in (_norm(f).split() for f in _GAP.split(evidence or "")) if p]
    if not parts or max(map(len, parts)) < (WHOLE_QUOTE_WORDS if len(parts) == 1 else QUOTE_PIECE_WORDS):
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

_MARKER = r"""said_by\s*[:=]\s*["']?(?:developer|agent)(?![\w-])["']?"""
_SAID_BY = re.compile(rf"[ \t]*(?:\(\s*{_MARKER}\s*\)|\[\s*{_MARKER}\s*\]|\b{_MARKER})", re.I)


def has_marker(text: str) -> bool:
    return bool(_SAID_BY.search(text))


def clean_rule(text: str) -> str:
    """Text without a literal `said_by: developer|agent` marker, bracketed or bare, and nothing
    else: a rule must not carry its own mark, or a line that looks like the frontmatter's would
    pass for it. Used where text enters from outside (a model's answer, a hand edit), never on
    text already in a note, so a note that talks about said_by keeps its words."""
    if not _SAID_BY.search(text):
        return text
    return _SAID_BY.sub("", text).strip()


_STOP = frozenset(
    "a an the and or but to of in on at for from with by as is are was were be been it its this that these "
    "those i you we they my your our their me us them please just also then so do does did will would should "
    "can could".split()
)


def _content(text: str) -> set[str]:
    """The words of a text that carry its meaning, plurals folded."""
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _norm(text).split() if w not in _STOP}


def supports(quote: str, rule: str) -> bool:
    """Do the quote's words cover most of what the rule says (not counting its "Why:" clause)?
    The rule needs two content words in common with the quote, and half of its own. A model may
    write several rules from one candidate, and a typed quote about one says nothing of the rest."""
    wanted = _content(re.split(r"\s+Why:", rule, maxsplit=1)[0])
    shared = wanted & _content(quote)
    return len(shared) >= 2 and 2 * len(shared) >= len(wanted)


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
    | \bgit\s+(?:reset\s+--hard|clean\b|push\b|branch\s+-D|checkout\s+--|stash\s+drop)
    | --force\b | --no-verify\b | \bforce[- ]push | --no-preserve-root
    | \bnc\b | \bmount\w*\b | (?<![\w/.])\./\S+                 # netcat, a mount, a script run by path
    | \bsource\s+(?:[~./$]|\S*/|\S+\.(?:sh|bash|zsh|env|profile|rc)\b)
    | \bmake\s+(?:-\S+\s+)*(?:deploy|release|publish|push|install|prod\w*)\b
    | \b(?:npm|npx|pnpm|yarn|bun|pip3?|pipx|uv|poetry|conda|brew|apt(?:-get)?|apk|gem|cargo|go)
        \s+(?:(?:-\S+|tool|pip|global)\s+)*(?:install|add|get|i)\b      # a package install
    | --dangerously[\w-]* | --yolo\b | --privileged\b | --no-sandbox\b | --allow[\w-]* | --trust[\w-]* | --insecure\b
    | \bauto[\s-]?(?:approv\w*|accept\w*|merge\w*|confirm\w*|allow\w*) | \b(?:yolo|dangerously)\b
    | \b(?:disabl\w*|bypass\w*|skip\w*|circumvent\w*|ignor\w*|override\w*|without|turn(?:ing)?\s+off)
        \s+(?:\w+\s+){0,3}(?:sandbox\w*|permissions?|review\w*|approvals?|confirm\w*|safeguards?|guardrails?
        |hooks?|verification|authenticat\w*)                         # switching a check off
    | \b(?:never|don'?t|do\s+not|stop|no\s+need\s+to)\s+(?:ask|prompt|confirm|check\s+with)\w*
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
    exfiltrating action (including a secret to move, a push, a package install or a deploy),
    or ask to switch off a sandbox, permission, confirmation or review? Such a rule is never
    marked as the developer's word. The patterns err toward true: an unmarked rule loses nothing
    but its mark, and a missed one would hand a command the developer's authority."""
    return bool(_RISKY.search(text))
