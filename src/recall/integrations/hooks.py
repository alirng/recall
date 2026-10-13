"""Each agent's edit hook speaks its own JSON. `parse` turns what an agent sends around an
edit into an Edit; `render` turns recall's decision back into what that agent understands,
as (stdout, exit code); `silent` is what to print when there is nothing to say.

A decision is tell (the agent carries on and is told), or ask / worktree (the edit is
refused once, with the reason as the agent's next instruction; status.check_edit lets the
retry through). No agent's hooks can make it ask the person before an edit in every mode,
but all of these can refuse one, so ask works the same everywhere.

  claude  PreToolUse on Edit/Write: context or deny.
  codex   PreToolUse on apply_patch, whose input is the patch text, and on shell commands,
          since a patch can also be applied through one: context or deny. Codex runs a new or
          changed hook only after it is trusted in /hooks.
  cursor  preToolUse on Write can only allow or deny, and must always print JSON or the edit
          is blocked; postToolUse carries context. Cursor also runs Claude Code's hooks, so
          the claude hook stands down inside Cursor and Cursor's own hooks answer.
  gemini  BeforeTool on write_file/replace can deny; AfterTool carries context.
  opencode  recall's plugin (opencode/recall.js) sends its own JSON: "before" can refuse
          (the plugin throws), "after" can add a note to the tool's result.

Elsewhere (Antigravity, Devin, Copilot, Cline, Kiro…) the MCP tool open_elsewhere and an
AGENTS.md line are the way in, until their hooks are confirmed working.
"""

import json
import os
import re
from dataclasses import dataclass

EDIT_TOOLS = {
    "claude": "Edit|Write|MultiEdit|NotebookEdit",
    "codex": "^(apply_patch|Bash)$",
    "cursor": "Write",
    "gemini": "^(write_file|replace)$",
}
DELIVERS = ("worktree", "ask", "tell")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


@dataclass
class Edit:
    session: str
    files: list[str]
    cwd: str | None
    acts: tuple[str, ...] = DELIVERS  # the actions this call can carry out


def parse(agent: str, event: dict) -> Edit | None:
    args = event.get("tool_input") or {}
    cwd = event.get("cwd") or next(iter(event.get("workspace_roots") or []), None)
    if agent == "claude":
        if event.get("cursor_version"):
            return None
        target = args.get("file_path") or args.get("notebook_path")
        files = [target] if target else []
    elif agent == "codex":
        patch = str(args.get("command") or args.get("input") or "")
        files = [(m.group(1) or m.group(2)).strip() for m in _PATCH_FILE.finditer(patch)]
    elif agent in ("cursor", "gemini", "opencode"):
        # These can refuse only before an edit and add a note only after it.
        if agent == "opencode":
            args = event.get("args") or {}
            patch = str(args.get("patchText") or "")
            files = [(m.group(1) or m.group(2)).strip() for m in _PATCH_FILE.finditer(patch)]
            files += [args["filePath"]] if args.get("filePath") else []
        else:
            target = args.get("file_path") or args.get("path") or args.get("target_file")
            files = [target] if target else []
        files = [f if os.path.isabs(f) or not cwd else os.path.join(cwd, f) for f in files]
        after = str(event.get("hook_event_name", "")).lower().startswith(("post", "after"))
        session = event.get("conversation_id") if agent == "cursor" else event.get("session_id")
        return Edit(session, files, cwd, ("tell",) if after else ("worktree", "ask")) if session and files else None
    else:
        return None
    session = event.get("session_id")
    return Edit(session, list(dict.fromkeys(files)), cwd) if session and files else None


def render(agent: str, event: dict, action: str, message: str, notice: str) -> tuple[str, int]:
    if agent in ("claude", "codex"):
        out = {"hookEventName": "PreToolUse"}
        if action == "tell":
            out["additionalContext"] = message
        else:
            out |= {"permissionDecision": "deny", "permissionDecisionReason": message}
        reply = {"hookSpecificOutput": out}
        if agent == "claude":
            reply["systemMessage"] = notice
        return json.dumps(reply), 0
    if agent == "cursor":
        if action == "tell":
            return json.dumps({"additional_context": message}), 0
        return json.dumps({"permission": "deny", "agent_message": message, "user_message": notice}), 0
    if agent == "gemini":
        if action == "tell":
            return json.dumps({"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": message}}), 0
        return json.dumps({"decision": "deny", "reason": message}), 0
    if agent == "opencode":
        return json.dumps({"tell": message} if action == "tell" else {"deny": message}), 0
    return "", 0


def silent(agent: str) -> str:
    return json.dumps({"permission": "allow"}) if agent == "cursor" else ""
