"""One chat in a common shape, plus the cleaning that keeps only what people and agents said."""

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str
    ts: float | None = None


@dataclass
class Session:
    agent: str
    native_id: str
    source: str  # file path, or "<db path>#<key>" for database-backed chats
    project: str | None = None
    title: str | None = None
    branch: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    # "interactive" for chats a person drove; "headless" for SDK / -p / exec runs.
    # Headless chats stay searchable but are not mined for memories.
    origin: str = "interactive"
    messages: list[Message] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.agent}:{self.native_id}"


# Harness-injected blocks that show up inside "user" turns. They are context the
# tool added, not something the person said, and they swamp search results. Memory
# extraction reads user turns as the developer speaking, so hook output, other agents'
# messages and tool output must never stay in them. Some blocks carry attributes.
_INJECTED = re.compile(
    r"<(system-reminder|environment_context|user_instructions|app-context|recommended_plugins|"
    r"permissions instructions|collaboration_mode|multi_agent_[a-z_]+|local-command-[a-z]+|"
    r"command-[a-z]+|timestamp|image_files|attached_files|additional_data|user_info|rules|"
    r"agent_skills?|open_and_recently_viewed_files|git_status|turn_aborted|task-notification|"
    r"dynamic_tool_catalog|ide_selection|ide_opened_file|external_[a-z_]+|recall-memory|"
    # hook output (Cursor), other sessions and subagents, tool output, harness context
    r"hooks_context|cross-session-message|agent-message|teammate-message|subagent_notification|"
    r"bash-stdout|bash-stderr|codex_internal_context|in-app-browser-context|skill|"
    r"manually_attached_skills|available_skills|available_subagent_types|available_subagent_models|"
    r"mcp_meta_tool_servers|mcp_meta_tools|agent_transcripts|comment-anchor-context|wake)"
    r"(?:\s[^>]*)?>.*?</\1>",
    re.S,
)
_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)

# Sentences the tools type on the user's behalf (Cursor plan mode, background-task
# follow-ups, UI buttons). Removed so memories are only mined from the person's words.
_BOILERPLATE = re.compile(
    r"Implement the plan as specified, it is attached for your reference\.|"
    r"Do NOT edit the plan file itself\.|"
    r"To-do'?s from the plan have already been created\.|"
    r"Do not create them again\.|"
    r"Mark them as in_progress[^.]*\.|"
    r"Don'?t stop until you have completed all the to-?dos\.|"
    r"Briefly inform the user about the task result and perform any follow-up actions(?: \(if needed\))?\.|"
    r"If there'?s no follow-ups needed, don'?t explicitly say that\.|"
    r"Execute the selected diff-tab [a-z-]+ action\.|"
    r"\[Request interrupted by user[^\]]*\]"
)
_EXTERNAL_TOOL = re.compile(r"\[external_agent_tool_call:.*?\[/external_agent_tool_call\]", re.S)


def clean_user_text(text: str) -> str:
    q = _USER_QUERY.findall(text)
    if q:
        text = "\n\n".join(q)
    text = _INJECTED.sub("", text)
    text = _BOILERPLATE.sub("", text).strip()
    if text.startswith(("# AGENTS.md instructions", "Caveat: The messages below")):
        return ""
    return text


# The summary Claude Code puts in a user turn when a session is continued after compaction.
# Parsers before chat schema 3 indexed it as the person's words; the current Claude parser
# skips the entry by its isCompactSummary flag, which a copy in the archive no longer has.
_COMPACTION_SUMMARY = re.compile(
    r"(?:\A|\n\n)(?:<artifact-content-authored-by-others/>\n[^\n]*\n)?"
    r"This session is being continued from a previous conversation that ran out of context\."
)


def drop_compaction_summary(text: str) -> str:
    """A user turn without the compaction summary in it. Older parsers merged consecutive
    user turns, so the summary can open the turn or follow the person's words; whatever
    follows it in the same turn is dropped with it, as there is no telling where it ends."""
    found = _COMPACTION_SUMMARY.search(text)
    return text[: found.start()].strip() if found else text


def clean_assistant_text(text: str) -> str:
    return _EXTERNAL_TOOL.sub("", text).strip()


def conversation_fingerprint(messages: list[Message]) -> str | None:
    """Same opening user turns => same conversation, e.g. a chat imported from one
    agent into another. Used to avoid mining duplicated chats twice."""
    turns = [" ".join(m.text.lower().split())[:300] for m in messages if m.role == "user"][:3]
    if not turns or sum(len(t) for t in turns) < 40:
        return None
    return hashlib.sha1("\n".join(turns).encode()).hexdigest()[:16]
