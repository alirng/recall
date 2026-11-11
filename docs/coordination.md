# Several chats, one repo

Run agents side by side and two of them will sooner or later change the same file. `recall
status` shows what each chat is working on now, on which branch, and the files two chats are
at right now: two chats editing the same lines of one working copy, or on two branches that
would conflict on the file. Files that are merely still open in several checkouts, because a
chat finished or its changes merge cleanly, are counted as "more files open" and listed under
`quiet` in `--json`; the menu bar's "Needs you" holds only the first kind.

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
same_copy = "ask"    # another chat is at the same lines of this working copy right now
conflict = "tell"    # the two branches would conflict on this file if merged now
apart = "tell"       # another chat is at this working copy right now, on other lines
overlap = "off"      # open in both, but merges cleanly, or the other chat is not at the file
```

`ask` stops the agent once so it checks with you; `worktree` stops it once and has it move
its work into its own git worktree; `tell` lets the edit through and tells the agent, which
mentions it to you; `off` does nothing. A chat counts as "at the file right now" only if it wrote something in
the last 5 minutes and edited that file in the last 15 (30 and 60 for a clash across branches). A
chat that has finished, an orchestrator that handed a file over to the sub-session now working on
it, and a branch that was merged days ago are `overlap`, which does nothing by default. Chats whose
last message is older than the hook's window are not looked at at all. Each other chat is raised
once, with every file the two share, and again only if things get worse. The hooks record which lines each edit
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

## Handing off to another chat

An agent can message any chat on this Mac, in any project, not only one it shares a file with. Ask it in your own words ("tell the Codex chat on the billing migration to rerun the tests"). It finds the chat with `search_chats` or `recent_chats` and calls `message_chat` with the chat's id, up to 4,000 characters, and a `who` line saying who it is.

The message arrives quoted and marked as from another agent. recall names the sending chat where it can tell which one it is, and says when it can't. The receiving agent treats it as a request from a colleague: it uses its judgement, and asks you first if the request is risky or out of scope. An agent can send ten messages an hour, a chat can be woken by one sender three times an hour, and a message waits up to a week.

If the chat is idle, recall wakes it. There is no daemon: `message_chat` starts one short `recall wake`.

| | Chat open and idle | Chat closed |
| --- | --- | --- |
| Claude Code | waits for its next turn; with `wake_open_sessions`, a new turn starts at once through its message socket | resumed headlessly and restricted |
| Codex | resumed headlessly and read-only, unless it was active in the last two minutes | resumed headlessly and read-only |
| Cursor | waits for its next edit or turn end | waits until it is next used |

A woken chat runs on your plan. A resumed chat is restricted even if your own default bypasses permissions, because nobody is watching and the request came from an agent: it can read and answer, tools you have already allowed in Claude Code's settings still run, and anything else needing approval is refused. It can tell the sender what it would need. A woken chat's own messages wait for their target's next turn rather than waking it, at most 20 wakes run an hour in all, and a chat already being woken isn't woken twice. In your config.toml:

```toml
[messaging]
wake = false                # leave messages for the chat's next turn
wake_open_sessions = true   # also wake open Claude Code chats, with their own permissions
inherit_permissions = true  # let resumed chats keep your own permission settings
```

Waking an open Claude Code chat is off by default. The turn runs with that chat's own permissions, so if it bypasses them, another agent's text would run unchecked while nobody watches. Even with `wake_open_sessions` on, Claude Code holds a message for a session that bypasses permissions when the sender doesn't, unless its `crossSessionInbound` setting is `accept`. recall counts a message as delivered only once it is in the chat's transcript; otherwise it waits for the chat's next turn.

A closed Claude Code chat resumed this way does not refresh a window already showing it. Reopen it to see the turn.
