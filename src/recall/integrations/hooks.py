"""recall's hooks, for every agent: one command per agent event, `recall hook --agent X`.

`parse` turns what an agent sends into an Event of one kind. The handlers for that kind
(HANDLERS: functions in their own modules, each taking the Event) return Replies, and
`render` merges them into the one answer that agent accepts, as (stdout, exit code). A
failing handler is skipped; the hook itself never breaks the session. INSTALL lists the
(event, matcher) entries `recall install` writes for each agent.

A new feature adds its kind's handler to HANDLERS and its entries to INSTALL. A kind whose
reply format is not confirmed for an agent is parsed but answered with silence there.

Kinds, and what a Reply can carry for them:
  session_start, compact   context (memory at the start, and again after compaction)
  edit                     before an edit: deny (refuse once, the reason is the agent's
                           next instruction) or context; Cursor, Gemini and OpenCode can
                           only refuse before an edit and inform after it (Event.acts)
  read, tool_failure       context after the tool ran
  prompt                   context with the person's prompt
  stop                     keep_going: the agent continues its turn with that text
  subagent_start/_stop     context

Formats, confirmed against each agent's docs or source, or live (October 2026):
  claude  PreToolUse on Edit/Write: additionalContext, or deny. SessionStart (also after
          compaction, source "compact"): plain text. PostToolUse (Read), PostToolUseFailure,
          UserPromptSubmit, SubagentStart: hookSpecificOutput.additionalContext. Stop: block.
          The subagent's prompt is only in PreToolUse on Agent/Task, whose updatedInput
          reaches the subagent (tested live). PostCompact and SubagentStop cannot inform.
  codex   Claude's event names and JSON, but any field it does not know makes the reply
          invalid. Its edit tool is apply_patch, whose input is the patch text; a patch
          applied through a shell command is not seen. It has no read tool (reads are shell
          commands, parsed here) and no failure event (a shell call always succeeds).
          SubagentStart takes context, without the prompt. A hook runs once trusted in /hooks.
  cursor  preToolUse on Write can only allow or deny, and must always print JSON or the edit
          is blocked; postToolUse (Write, Read, or a Shell call that exited non-zero) and
          postToolUseFailure carry additional_context; stop takes followup_message.
          beforeSubmitPrompt takes additional_context too: undocumented, but 3.23's
          validator accepts it and sends it as a system reminder (read from the app bundle,
          not live-tested). preCompact and subagentStart cannot inform: they are observed.
          preToolUse on its subagent tool takes updated_input (merged into the call; from
          the 3.23 app code, not live-tested), which carries a subagent_start context.
          Its edit fires Read then Write under one tool_use_id (Event.call). Cursor also
          runs Claude Code's hooks (with cursor_version set); the claude adapter stands down.
  gemini  BeforeTool on write_file/replace can deny; AfterTool carries context.
  opencode  recall's plugin (opencode/recall.js) sends its own JSON: "before" can refuse
          (the plugin throws), "after" can add a note to the tool's result.
"""

import importlib
import json
import os
import shlex
import sys
from dataclasses import dataclass, field

from ..status import content_lines, edit_lines, patch_files, patch_hunks

EDIT_TOOLS = {
    "claude": "Edit|Write|MultiEdit|NotebookEdit",
    "codex": "^apply_patch$",
    "cursor": "Write",
    "gemini": "^(write_file|replace)$",
}

# Handlers per kind, as "module:function"; each gets the Event and returns a Reply or None.
HANDLERS: dict[str, list[str]] = {
    "session_start": ["recall.memory.context:on_session_start", "recall.autopilot:on_session_start"],
    "compact": ["recall.memory.context:on_session_start", "recall.jit:on_compact"],
    "edit": ["recall.status:on_edit", "recall.mail:on_edit"],
    # mail first: a message sent to this chat comes before what recall remembers
    "prompt": ["recall.mail:on_turn", "recall.jit:on_prompt"],
    "read": ["recall.jit:on_read"],
    "tool_failure": ["recall.jit:on_tool_failure"],
    "stop": ["recall.mail:on_turn"],
    "subagent_start": ["recall.explore:on_start"],
    "subagent_stop": ["recall.explore:on_stop"],
}

# Exploration subagents (explore.py): where each agent's subagent's prompt and report reach a
# hook. Codex only records: its SubagentStart comes before the child's task exists. Cursor's
# subagent tool is named Task, Subagent or mcp_task depending on the model.
EXPLORE = {
    "claude": [("PreToolUse", "Agent|Task"), ("SubagentStop", "[Ee]xplor")],
    "codex": [("SubagentStop", "explorer")],
    "cursor": [("preToolUse", "Task|Subagent|mcp_task"), ("subagentStop", "explor")],
}

# The entries `recall install` writes: (event, matcher) per agent, all answered by one command,
# with an optional third element of settings for that entry. One entry per (event, matcher):
# two would run the command twice. UserPromptSubmit serves mail and jit alike.
INSTALL: dict[str, list[tuple]] = {
    "claude": [
        ("SessionStart", None),
        ("PreToolUse", EDIT_TOOLS["claude"]),
        ("UserPromptSubmit", None),  # mail; jit: notes and chats the prompt matches
        ("PostToolUse", "Read"),  # jit: what is anchored to the file
        ("PostToolUseFailure", "Bash"),  # jit: past chats with the same error
        ("Stop", None),
        *EXPLORE["claude"],
    ],
    "codex": [
        ("SessionStart", None),
        ("PreToolUse", EDIT_TOOLS["codex"]),
        ("UserPromptSubmit", None),
        ("PostToolUse", "^Bash$"),  # jit: reads are shell commands; every one costs a hook call
        ("Stop", None),
        *EXPLORE["codex"],
    ],
    "cursor": [
        ("preToolUse", EDIT_TOOLS["cursor"]),
        ("postToolUse", EDIT_TOOLS["cursor"]),
        ("beforeSubmitPrompt", None),  # jit: additional_context, read from the app bundle (3.23)
        ("postToolUse", "Read"),
        # Failed Shell calls (postToolUse with exitCode, postToolUseFailure) wait for the index
        # to keep tool errors: until then every Shell call would pay a hook for nothing.
        ("preCompact", None),
        ("stop", None, {"loop_limit": 3}),  # a follow-up at most three times in a row
        *EXPLORE["cursor"],
    ],
    "gemini": [("BeforeTool", EDIT_TOOLS["gemini"]), ("AfterTool", EDIT_TOOLS["gemini"])],
}


def consent_key(agent: str, event: str, matcher: str | None = None) -> str:
    """The unit a person consents to when autopilot may install hooks: an agent's hook event
    ("claude:Stop"), with the exploration subagent hooks (EXPLORE), which rewrite a subagent's prompt,
    as their own ("claude:PreToolUse:explore") beside the edit hook on the same event. It does not
    depend on matcher text, so widening a matcher later does not turn a consented hook into a new one."""
    return f"{agent}:{event}" + (":explore" if (event, matcher) in EXPLORE.get(agent, []) else "")


DELIVERS = ("worktree", "ask", "tell")


@dataclass
class Event:
    agent: str
    kind: str
    name: str  # the agent's own event name, for the reply
    session: str | None
    cwd: str | None = None
    transcript: str | None = None  # where the agent writes this chat, to tell it from others
    tool: str | None = None
    files: list[str] = field(default_factory=list)
    acts: tuple[str, ...] = DELIVERS  # edit: what this call can carry out (refuse, tell)
    observe: bool = False  # the agent cannot be answered at this event; handlers may only note it
    prompt: str | None = None  # the person's prompt, or a subagent's
    error: str | None = None  # tool_failure: what went wrong
    call: str | None = None  # the tool call's id (Cursor's edit fires Read then Write under one id)
    subagent: str | None = None  # subagent_start/_stop: the subagent's type
    report: str | None = None  # subagent_stop: what the subagent handed back
    source: str | None = None  # session_start: startup, resume, clear or compact
    olds: list[str] = field(default_factory=list)  # edit: the text it replaces, when it says
    news: list[str] = field(default_factory=list)
    content: str | None = None  # edit: the whole new file, for a write
    raw: dict = field(default_factory=dict)

    def lines(self, path: str) -> list[list[int]] | None:
        """The lines an edit changes in `path`, when it is the only file and they can be told:
        from the replaced text, the patch's hunks, or the whole new file against the old."""
        if self.kind != "edit" or len(self.files) != 1:
            return None
        full = path if os.path.isabs(path) or not self.cwd else os.path.join(self.cwd, path)
        if self.content is not None:
            return content_lines(full, self.content)
        return edit_lines(full, self.olds, self.news) if self.olds else None


def _replaced(args: dict, old: str, new: str) -> tuple[list[str], list[str]]:
    edits = args.get("edits") if isinstance(args.get("edits"), list) else [args]
    pairs = [(e.get(old), e.get(new) or "") for e in edits if isinstance(e, dict) and isinstance(e.get(old), str)]
    return [o for o, _ in pairs], [n for _, n in pairs]


@dataclass
class Reply:
    context: str | None = None  # for the model
    deny: str | None = None  # refuse this tool call, with the reason (before a tool only)
    keep_going: str | None = None  # continue the turn with this text (stop only)
    notice: str | None = None  # one line for the person


# --- parsing ---------------------------------------------------------------------------

# Claude Code and Codex share event names; Cursor's are camelCase.
_KINDS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "stop": "stop",
    "PostToolUseFailure": "tool_failure",
    "postToolUseFailure": "tool_failure",
    "SubagentStart": "subagent_start",
    "subagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "subagentStop": "subagent_stop",
    # Neither can carry context; the next SessionStart (source compact) delivers after it.
    "PostCompact": "compact",
    "preCompact": "compact",
    "beforeSubmitPrompt": "prompt",
}
_OBSERVE = {"PostCompact", "preCompact"}
_SUBAGENT_TOOLS = ("Agent", "Task", "Subagent", "mcp_task")
_READERS = {"cat", "head", "tail", "sed", "nl", "less", "more", "bat", "awk", "grep", "rg"}
# The events and tools that are an edit, per agent: anything else is not parsed as one.
_EDITS = {
    "claude": ({"PreToolUse"}, {"Edit", "Write", "MultiEdit", "NotebookEdit"}),
    "codex": ({"PreToolUse"}, {"apply_patch", "Edit", "Write"}),
    "cursor": ({"preToolUse", "postToolUse"}, {"Write"}),
    "gemini": ({"BeforeTool", "AfterTool"}, {"write_file", "replace"}),
    "opencode": ({"before", "after"}, {"edit", "write", "apply_patch"}),
}


def shell_reads(command, cwd: str | None) -> list[str]:
    """The files a shell command reads with cat, sed, head and the like: Codex has no read
    tool. Only arguments that name an existing file count."""
    if isinstance(command, list):  # ["bash", "-lc", "cat x"] or the words themselves
        command = command[-1] if len(command) >= 3 and command[-2] in ("-c", "-lc") else " ".join(command)
    try:
        words = shlex.split(str(command or ""))
    except ValueError:
        return []
    out, head, base, skip = [], True, cwd, False
    reader = cd = False
    for w in words:
        if w in ("|", "||", "&&", ";"):
            head = True
            continue
        if head:
            name = os.path.basename(w)
            reader, cd, head = name in _READERS, name == "cd", False
            continue
        if skip or w in (">", ">>", "1>", "2>", "&>"):  # a redirect's target is written, not read
            skip = not skip
            continue
        if cd:  # later commands run in the new folder
            base = w if os.path.isabs(w) or not base else os.path.join(base, w)
            continue
        if w == "-i" or w.startswith("-i."):  # sed -i edits its files in place
            reader = False
        if reader and not w.startswith("-"):
            full = w if os.path.isabs(w) or not base else os.path.join(base, w)
            if os.path.isfile(full):
                out.append(full)
    return list(dict.fromkeys(out))


def parse(agent: str, raw: dict) -> Event | None:
    if agent == "claude" and raw.get("cursor_version"):
        return None  # Cursor running Claude Code's hooks; Cursor's own hooks answer
    args = raw.get("tool_input") or {}
    cwd = raw.get("cwd") or next(iter(raw.get("workspace_roots") or []), None)
    session = raw.get("conversation_id") if agent == "cursor" else raw.get("session_id")
    name = str(raw.get("hook_event_name") or "")
    tool = raw.get("tool_name") or raw.get("tool")
    e = Event(agent, "", name, session, cwd, raw.get("transcript_path"), tool, raw=raw)
    e.call = raw.get("tool_use_id")

    if name == "SessionStart":
        e.source = raw.get("source")
        e.kind = "compact" if e.source == "compact" else "session_start"
    elif name in _KINDS:
        e.kind = _KINDS[name]
        e.prompt = raw.get("prompt") or raw.get("task")
        e.error = raw.get("error") or raw.get("error_message")
        e.subagent = raw.get("agent_type") or raw.get("subagent_type")
        e.report = raw.get("last_assistant_message") or raw.get("summary")
        # Cursor drops context at subagentStart; a subagent's end is for handlers to note.
        e.observe = name in _OBSERVE or e.kind == "subagent_stop" or (agent == "cursor" and e.kind == "subagent_start")
    elif name in ("PreToolUse", "preToolUse") and tool in _SUBAGENT_TOOLS:
        # The subagent's prompt is only in its Agent/Task call, and rewriting it reaches the
        # subagent: Claude Code (tested live) and Cursor (from its 3.23 app code, not live).
        e.kind, e.prompt, e.subagent = "subagent_start", args.get("prompt"), args.get("subagent_type")
        e.observe = agent not in ("claude", "cursor")
    elif name in ("PostToolUse", "postToolUse") and tool == "Read":
        e.kind, e.files = "read", [args["file_path"]] if args.get("file_path") else []
    elif agent == "codex" and name == "PostToolUse" and tool == "Bash":
        e.kind, e.files = "read", shell_reads(args.get("command"), cwd)
        if not e.files:
            return None
    elif agent == "cursor" and name == "postToolUse" and tool == "Shell":
        # Cursor's non-streaming shell reports a failed command here, with its exit code.
        try:
            output = json.loads(raw.get("tool_output") or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {}
        code = output.get("exitCode") if isinstance(output, dict) else None
        if not code:
            return None
        e.kind, e.error = "tool_failure", str(output.get("output") or output.get("stderr") or f"exit code {code}")
    elif agent not in _EDITS or name not in _EDITS[agent][0] or tool not in _EDITS[agent][1]:
        return None
    elif agent == "claude":
        target = args.get("file_path") or args.get("notebook_path")
        e.kind, e.files = "edit", [target] if target else []
        e.olds, e.news = _replaced(args, "old_string", "new_string")
        e.content = args.get("content") if isinstance(args.get("content"), str) else None
    elif agent == "codex":
        patch = str(args.get("command") or args.get("input") or "")
        e.kind, e.files = "edit", patch_files(patch, cwd)
        e.olds, e.news = patch_hunks(patch)
    else:
        # These can refuse only before an edit and add a note only after it.
        if agent == "opencode":
            args = raw.get("args") or {}
            files = patch_files(str(args.get("patchText") or ""), cwd)
            files += [args["filePath"]] if args.get("filePath") else []
            e.olds, e.news = (
                patch_hunks(str(args.get("patchText") or ""))
                if args.get("patchText")
                else _replaced(args, "oldString", "newString")
            )
        else:
            target = args.get("file_path") or args.get("path") or args.get("target_file")
            files = [target] if target else []
            e.olds, e.news = _replaced(args, "old_string", "new_string")
        e.content = next((args[k] for k in ("content", "contents") if isinstance(args.get(k), str)), None)
        after = name.lower().startswith(("post", "after"))
        e.kind, e.files, e.acts = "edit", files, ("tell",) if after else ("worktree", "ask")
    if e.kind == "edit":
        e.files = list(dict.fromkeys(e.files))
        if not e.files:
            return None
    return e if e.session or e.kind in ("session_start", "compact") else None


# --- answering ----------------------------------------------------------------------------


def _handler(ref: str):
    module, func = ref.split(":")
    return getattr(importlib.import_module(module), func)


def _join(parts) -> str | None:
    return "\n\n".join(dict.fromkeys(p for p in parts if p)) or None


def merge(replies: list[Reply]) -> Reply:
    """One Reply from many: a refusal wins and carries everything the agent should read."""
    deny = _join(r.deny for r in replies)
    context = _join(r.context for r in replies)
    if deny:
        deny, context = _join([deny, context]), None
    notice = next((r.notice for r in replies if r.notice), None)
    return Reply(context, deny, _join(r.keep_going for r in replies), notice)


def dispatch(agent: str, raw: dict) -> tuple[str, int]:
    event = parse(agent, raw)
    if not event:
        return silent(agent, raw), 0
    replies = []
    for ref in HANDLERS.get(event.kind, []):
        try:
            if reply := _handler(ref)(event):
                replies.append(reply)
        except Exception as e:  # one handler's failure must not cost the others, or the session
            print(f"recall hook: {ref}: {e}", file=sys.stderr)
    reply = merge(replies)
    if event.observe or not (reply.context or reply.deny or reply.keep_going):
        return silent(agent, raw), 0
    return render(event, reply)


def render(e: Event, r: Reply) -> tuple[str, int]:
    agent, kind = e.agent, e.kind
    if kind in ("session_start", "compact") and agent in ("claude", "codex"):
        return r.context or "", 0  # plain text joins the session's context
    if kind == "stop":
        if agent == "cursor":
            return (json.dumps({"followup_message": r.keep_going}), 0) if r.keep_going else ("", 0)
        if agent in ("claude", "codex") and r.keep_going:
            return json.dumps({"decision": "block", "reason": r.keep_going}), 0
        return "", 0
    if kind == "prompt" and agent in ("claude", "codex") and r.context:
        return json.dumps(
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": r.context}}
        ), 0
    if kind == "prompt" and agent == "cursor" and r.context:
        # beforeSubmitPrompt: documented as continue/user_message only, but 3.23's validator
        # takes additional_context and the composer sends it as a system reminder (app bundle).
        return json.dumps({"additional_context": r.context}), 0
    if kind == "edit":
        return _render_edit(e, r)
    if kind in ("read", "tool_failure") and r.context:
        if agent == "cursor":
            return json.dumps({"additional_context": r.context}), 0
        if agent in ("claude", "codex"):
            return json.dumps({"hookSpecificOutput": {"hookEventName": e.name, "additionalContext": r.context}}), 0
    if kind == "subagent_start" and r.context:
        if agent == "claude" and e.name == "PreToolUse":
            args = e.raw.get("tool_input") or {}
            prompt = f"{args.get('prompt') or ''}\n\n{r.context}".strip()
            return json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": args | {"prompt": prompt}}}
            ), 0
        if agent == "cursor" and e.name == "preToolUse":
            # Merged into the Task call's input (extensions/cursor-agent-exec/dist/main.js in 3.23);
            # no permission field, so nothing is approved. Not live-tested.
            prompt = f"{(e.raw.get('tool_input') or {}).get('prompt') or ''}\n\n{r.context}".strip()
            return json.dumps({"updated_input": {"prompt": prompt}}), 0
        if agent in ("claude", "codex") and e.name == "SubagentStart":
            return json.dumps(
                {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": r.context}}
            ), 0
    return silent(agent, e.raw), 0  # a kind whose reply is not confirmed for this agent


def _render_edit(e: Event, r: Reply) -> tuple[str, int]:
    agent, message = e.agent, r.deny or r.context or ""
    if agent in ("claude", "codex"):
        out = {"hookEventName": "PreToolUse"}
        if r.deny:
            out |= {"permissionDecision": "deny", "permissionDecisionReason": message}
        else:
            out["additionalContext"] = message
        reply = {"hookSpecificOutput": out}
        if agent == "claude" and r.notice:
            reply["systemMessage"] = r.notice  # Codex refuses fields it does not know
        return json.dumps(reply), 0
    if agent == "cursor":
        if r.deny:
            return json.dumps({"permission": "deny", "agent_message": message, "user_message": r.notice or ""}), 0
        return json.dumps({"additional_context": message}), 0
    if agent == "gemini":
        if r.deny:
            return json.dumps({"decision": "deny", "reason": message}), 0
        return json.dumps({"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": message}}), 0
    if agent == "opencode":
        return json.dumps({"deny": message} if r.deny else {"tell": message}), 0
    return "", 0


def silent(agent: str, raw: dict | None = None) -> str:
    """Cursor's preToolUse blocks the call unless it gets JSON back. For an edit that is an
    allow; for any other tool (a Task, say) an empty answer, since allow would approve it."""
    raw = raw or {}
    if agent == "cursor" and str(raw.get("hook_event_name", "preToolUse")) == "preToolUse":
        return json.dumps({"permission": "allow"} if raw.get("tool_name") in (None, "", "Write") else {})
    return ""
