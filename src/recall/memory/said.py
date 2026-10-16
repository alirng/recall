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

Otherwise a rule is marked `said_by: agent` when only the agent has stated it, and
`said_by: unverified` when a user turn stated it but no typed quote supports it (a paste, a
paraphrase, a risky rule). Unset is left on rules written before speakers were recorded, which
readers must treat as "unknown", never as the agent's or the developer's word. The mark is a
hint, not authentication: it shows the words were typed in that chat, not that the rule means
what they said, and anything that can write the notes folder can write the field. Nothing may
act on a rule because of it; recall uses it only to hold back (notes.pushed): a rule only the
agent stated is never handed to an agent unasked.

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


# --- what may be handed to an agent unasked ---------------------------------------------------

# What an agent could act on: a place to fetch from or send to, a command to run, a
# destructive or exfiltrating step, a check switched off. Unlike _RISKY it leaves out what
# guardrails and project facts are made of: a one-word code span (a file, a model, a flag
# name), secret or credential words with nowhere to send them, a push, a delete in passing.
_HAZARD = re.compile(
    r"""
      [a-z][a-z0-9+.-]*://                                    # any URL scheme
    | \bwww\. | \bgit@ | \b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b          # a host or an address
    | \b\d{1,3}(?:\.\d{1,3}){3}\b                             # an IP address
    | \b[a-z0-9-]+\.(?:com|net|org|io|dev|ai|xyz|ru|cn|cloud|info)\b
    | \$\( | \$\{ | && | \|\| | 2>&1 | >\s*/dev/ | \|\s*(?:sh|bash|zsh|python3?|node|sudo|xargs|tee|nc)\b
    | ^\s*[$#]\s+\S | ~/\.ssh | \bid_rsa\b | (?<![\w/.])\./\S+  # a prompt, a key, a script run by path
    | \b(?:run|execute|source|launch)\s+(?:the\s+)?[\w./-]+\.(?:sh|bash|zsh|py|js|mjs|ts|rb|pl|ps1|bat|cmd|command)\b
    | \b(?:python3?|node|bash|sh|zsh|ruby|perl|deno)\s+[\w./-]+\.(?:py|js|mjs|ts|sh|rb|pl)\b   # a script run by name
    | (?-i:\b(?:curl|wget|ssh|scp|sftp|rsync|ftp|netcat|ncat|telnet|nslookup|sudo|chmod|chown|mkfs|killall
        |pkill|shutdown|crontab|launchctl|osascript|powershell|base64|openssl|ngrok|socat)\b)  # as typed: not "SSH"
    | \brm\s+-\w | \bdd\s+if= | \bdrop\s+(?:table|database|schema)\b | \bdelete\s+from\b
    | \bgit\s+(?:reset\s+--hard|clean\b|push\s+(?:-f\b|--force)|branch\s+-D|checkout\s+--|stash\s+drop)
    | --force\b | --no-verify\b | \bforce[- ]push\w* | --no-preserve-root
    | \b(?:npm|npx|pnpm|yarn|bun|pip3?|pipx|uv|poetry|conda|brew|apt(?:-get)?|apk|gem|cargo|go)
        \s+(?:(?:-\S+|tool|pip|global)\s+)*(?:install|add|get|i)\b      # a package install
    | --dangerously[\w-]* | --yolo\b | --privileged\b | --no-sandbox\b | --allow[\w-]* | --trust[\w-]* | --insecure\b
    | \bauto[\s-]?(?:approv\w*|accept\w*|merge\w*|confirm\w*|allow\w*) | \b(?:yolo|dangerously)\b
    | \b(?:disabl\w*|bypass\w*|skip\w*|circumvent\w*|ignor\w*|override\w*|turn(?:ing)?\s+off)
        \s+(?:\w+\s+){0,3}(?:sandbox\w*|permissions?|review\w*|approvals?|confirm\w*|safeguards?|guardrails?
        |hooks?|verification|authenticat\w*)                         # switching a check off
    | \bwithout\s+(?:any\s+|a\s+)?(?:review\w*|approvals?|confirm\w*|asking)\b   # or going around one
    | \b(?:exfiltrat\w*|transmit\w*|pastebin|beacon\w*|reverse\s+shell)\b
    | \b(?:send\w*|post(?:s|ed|ing)?|forward\w*|e?mail\w*|upload\w*|share\w*|copy|copies|copying)
        \s+(?:\S+\s+){0,3}(?:secrets?|keys?|tokens?|credentials?|passwords?|cookies?|\.env\S*|history
        |transcripts?|chats?|logs?|notes|memory|files?)\b(?:\s+\S+){0,4}?\s+(?:to|into|onto)\s+\S  # sent away
    | (?:^|[.;:!?]\s+|\b(?:always|first|then|must|should)\s+)(?:delete|wipe|erase|destroy|purge)\b  # an order
    """,
    re.I | re.X | re.M,
)
_SPAN = re.compile(r"`([^`\n]*)`")  # code spans, paired left to right
# A code span is a command when it starts with a program people run, or a path, and goes on, or
# has a flag in it; `weekly report` or `workflow_type: 'cv-pack'` is a name or a value.
_COMMAND = re.compile(
    r"""^\s*(?:sudo\s+)?(?:[~./][\w./-]*|(?:git|gh|npm|npx|pnpm|yarn|bun|uv|uvx|pip3?|pipx|python3?|node|deno
    |ruby|perl|bash|sh|zsh|make|cargo|go|docker|kubectl|helm|terraform|aws|gcloud|az|vercel|wrangler|supabase
    |firebase|heroku|brew|apt(?:-get)?|xcodebuild|xcrun|swift|rm|mv|cp|ln|chmod|chown|tar|unzip|open|kill
    |killall|pkill|launchctl|defaults|sed|awk|find|xargs|tee|cat|echo|export|source|eval|exec|env|sqlite3|psql
    |mysql|recall|claude|codex|cursor)\b)\s+\S|\s-{1,2}\w""",
    re.X,
)
# Never excused by a "never" in front: it is one.
_UNCHECKED = re.compile(
    r"\b(?:never|don'?t|do\s+not|stop|no\s+need\s+to)\s+(?:ask|prompt|confirm|check\s+with)\w*", re.I
)
# A prohibition right before a hazard excuses it: "never force-push", "do not run `rm -rf build`".
# Only a few words may stand between, none of them across a clause, so "don't wait; run `…`" or
# "never hesitate to run `…`" still counts.
_FORBIDS = re.compile(
    r"\b(?:never|not|don'?t|avoid)\s+(?:(?:a|an|the|any|your|ever|(?:run|us|call|execut|commit|push|merg|deploy"
    r"|install|delet|remov|shar|send|upload|past|print|log|stor|hardcod|expos|includ|add|put|typ)\w*)\s+){0,2}$",
    re.I,
)


def dangerous(text: str) -> bool:
    """Would handing this rule to an agent unasked (session start, jit.py) give it something to
    act on: a URL or host, a command (a code span that runs a program, shell syntax, a network
    or system tool, a destructive command or flag, a package install), a check switched off, or
    private data to send somewhere? A hazard that a prohibition governs ("never force-push",
    "don't run `rm -rf build`") is a guardrail, not an order, and does not count. Narrower than
    risky(), which decides the developer's mark and errs further toward true."""
    if _UNCHECKED.search(text):
        return True
    spans = list(_SPAN.finditer(text))

    def start(at: int) -> int:  # a hazard inside a code span is governed by what precedes the span
        return next((s.start() for s in spans if s.start() < at < s.end()), at)

    hazards = [m.start() for m in _HAZARD.finditer(text)] + [s.start() for s in spans if _COMMAND.search(s.group(1))]
    return any(not _FORBIDS.search(text[max(0, start(at) - 60) : start(at)]) for at in hazards)
