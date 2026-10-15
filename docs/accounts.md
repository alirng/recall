# Several accounts and installs

Working across a personal and a work account, or Cursor next to Cursor Nightly, means several
agent folders: Claude Code with a second `CLAUDE_CONFIG_DIR`, Codex with another `CODEX_HOME`,
a second Cursor build. recall finds each one (the defaults, the environment, `export` lines,
assignments that start a statement and aliases in your shell rc files, `~/.claude-*` style folders
that hold a real config, Cursor's builds), names it (`claude`, `claude-work`, `cursor-nightly`) and
indexes it, so every chat records which folder it came from. Text that only mentions a variable (an
`echo`, a comment) names nothing; the home folder and anything above it never count; and a folder
called `*-backup` or `*.bak`, or one whose chats are the default folder's, is not an account. Those
checks apply to folders recall finds by name: a backup you name yourself in `CLAUDE_CONFIG_DIR` or
`CODEX_HOME` is a root like any other. Its copies of the default folder's chats are the same chats,
so the index holds each once, under whichever folder it read last; give a backup `index = false`
(below) before it is first read. `recall doctor` lists them:

```text
claude          ~/.claude
                connected; 5,812 chats, last indexed 3 min ago; a***@***
claude-work     ~/.claude-work
                waiting for first launch (the agent has not used this folder yet; recall writes nothing there)
codex           ~/.codex
                hooks written, trust pending: trust them once with /hooks in Codex; 212 chats, last indexed 3 min ago
cursor          ~/.cursor  [~/Library/Application Support/Cursor/User]
                connected, approval pending: Cursor asks once per project before it loads a new MCP server; 3 chats
```

A folder is written to only after the agent has used it. Chats of a second account are searchable
at once, but they only become memory when you say so, per root in `config.toml`:

```toml
[roots.claude-work]
mine = true      # `recall dream` turns this root's chats into notes (default roots: on)
index = false    # do not read this root's chats at all
connect = false  # autopilot and `recall install` leave it alone
```

Roots are accounts and installs; [`[contexts]`](how-it-works.md#how-agents-use-it) are folders. A mined root's notes take their
context from the folder each chat ran in, so name your work folders in `[contexts]` before you turn
`mine` on for a work account, or its notes will belong to no context and load everywhere.

Say yes once to *keep recall connected automatically* (`recall setup` asks, and the default is no;
or `recall autopilot on`). recall then writes its MCP server, hooks and skill into each agent folder
that is new or has lost them, using the code `recall install` runs, with each file backed up first
(`<file>.recall-backup` keeps the file as it was before recall first touched it; later changes keep
`<file>.<time>.recall-backup`). The hooks can add context to your prompts and continue a turn, so the
yes covers the hooks this release installs and no others: a hook a later release adds shows in
`recall doctor` as "new hooks available" and waits for you to run `recall install`. A hook that
already runs an existing recall is left alone, so running recall from a checkout never takes the
installed one's hooks over; `recall install` and `recall doctor` name the recall the hooks run when it
is not the one you ran, and `recall install --repoint` points them at this one (Codex asks you to
trust them again; the MCP server entry is not moved). It checks every night and, when a session starts in Claude Code or
Codex, looks for gaps and fixes them in the background. With autopilot off nothing changes.
`recall uninstall` turns it off, takes its lock and removes recall from every folder it connected,
including one your rc no longer names; `recall uninstall --root claude-work` removes it from one
and keeps autopilot out of it. Chat history is only ever read.

Two agents ask for a second step that recall cannot take for you. Codex runs hooks only after you
trust them once in `/hooks`. Cursor loads an MCP server from `mcp.json` only after you approve it,
once per project (`cursor-agent mcp enable recall`, or the prompt in the app), and asks again when
the entry changes; `recall doctor` reads the approval files and says "approval pending" until one
exists. Cursor's builds share `~/.cursor` for MCP, hooks, skills and transcripts, so a second build
needs no new files.
