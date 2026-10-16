# Several chats, one repo

Run agents side by side and two of them will sooner or later change the same file. `recall
status` shows what each chat is working on now, on which branch, and which files two chats
both have open changes to: whether the two would conflict if merged now, or share one working
copy.

```text
$ recall status
src/auth/session.py will conflict  (harbor · main ↔ new-login)
    Claude Code · Fix the login redirect
    Codex · Restyle the sign-in page

2 live, 5 since 12h
  harbor         main, new-login
```

After an upgrade it also names the agents to restart. An agent's recall MCP server keeps the
release it started with until the agent restarts, so each server that started before recall was
last installed shows as `Restart <agent>`, with its pid and start time (and, for Claude Code, the
chat). `--json` lists them under `outlived`.

The edit hook acts on it as it happens. Before an agent edits a file another chat has open
changes to, recall applies your rule for the situation, set in `config.toml` (`recall setup`
offers presets; these are the recommended ones):

```toml
[coordination]
same_copy = "ask"    # another live chat is editing the same lines of this working copy
conflict = "tell"    # the two branches would conflict on this file if merged now
apart = "tell"       # another live chat is editing this working copy, on other lines
overlap = "off"      # open in both, but merges cleanly (or the other chat has gone quiet)
```

`ask` stops the agent once so it checks with you; `worktree` stops it once and has it move
its work into its own git worktree; `tell` lets the edit through and tells the agent, which
mentions it to you; `off` does nothing. Each other chat is raised once, with every file the
two share, and again only if things get worse. The hooks record which lines each edit
changes, so two chats working on different parts of one file get `apart`, not `same_copy`.

The other chat hears about it too: recall messages it which chat is about to change which
lines, and what that chat was asked. Either agent can answer with the `message_chat` MCP
tool, to agree who goes first or that they are on different parts. An agent can only message
chats recall introduced it to in the last day, using the token from recall's note (so only
agents with recall's hooks, which get those notes, can message), and at most
ten 600-character messages an hour, so an agent that read something hostile can't reach chats
it was never introduced to. What another agent writes always arrives quoted and marked as not
coming from you. (The token is in the chat's transcript, so it stops a misled agent, not one
set on reading files to pose as another chat.) The menu
bar shows the conversation under the file. A chat reads its messages:

| | On its next edit | When it finishes a turn | On your next prompt |
| --- | --- | --- | --- |
| Claude Code | yes | yes: it keeps going to answer | yes |
| Codex | yes | yes: it keeps going to answer | yes |
| Cursor | yes, after the edit | yes: sent as a follow-up message | no (Cursor's prompt hook can only block) |

Keeping an agent going is capped at three times an hour per sender (and Cursor's follow-ups
at three in a row), so two agents cannot keep each other talking. A chat sitting idle hears
nothing until one of these happens. Line ranges come from each agent's edit: the text
Claude Code replaces, the hunks of a Codex patch, and the difference between the file and
the whole new file Cursor writes.

The menu bar app is a macOS 14 prototype you build from source (`apps/menubar/build.sh`); it is
not part of the package. In it, **Resolve** on a shared file sends every chat involved a note
asking it to agree with the others who goes first. Each chat reads it at the next of those
moments, so nothing is interrupted; the row shows when it was sent and clears once the overlap
does.
