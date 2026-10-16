# Security

recall reads your coding agents' chat history, and it sends parts of that history to a model
you choose. It also adds an MCP server and hooks to your agents, and one of its tools lets an
agent leave a message for another chat. This page says what recall reads, where it writes, what
its tools and hooks can do, how secrets are handled, how long it keeps chats, and how to report
a problem.

## What recall reads

recall reads the chat history that each agent keeps on disk:

- Claude Code: `~/.claude/projects`
- Codex: `~/.codex`
- Cursor: `~/.cursor/projects`, and Cursor's user data folder
  (`~/Library/Application Support/Cursor/User` on macOS)

It reads the git repositories your chats worked in, with read-only git commands, for
`recall status` and for linking commits to chats. It reads your shell's startup files
(`~/.zshrc` and the like) only to find the `CLAUDE_CONFIG_DIR` and `CODEX_HOME` you set for a
second account, and keeps just those two values. It reads Claude Code's settings
(`~/.claude/settings.json`, its cached `remote-settings.json`) and an organisation's managed
settings (macOS managed preferences, the `managed-settings` folders) for `cleanupPeriodDays`
only. It also reads its own index and its memory notes. It never writes to an agent's history.
SQLite databases are opened read-only.

## What recall writes

- `~/.agents/recall/` (or `RECALL_HOME`):
  - the search index, `config.toml`, logs, and `keys.json` if you chose file storage for a key;
  - `usage.jsonl`, a log of recall's own use: each MCP call, search, memory load and
    mid-session hook hand-over, with the agent that made it, the words asked and the ids that
    came back, never chat or note text. `usage_log = false` in `config.toml` turns it off;
  - `cache/`, short-lived state shared by the hooks and the MCP server: the mailbox between
    chats (see below), which chats are editing which files, and what memory a session has
    already been shown. Messages can quote a few words of what a chat was asked;
  - `autopilot.json` and `autopilot.lock`, only if you turn on autopilot.
- `~/.agents/memory/` (or `RECALL_MEMORY`): memory notes as markdown. This folder is a git
  repository, and each memory run is one local commit. recall never pushes.
- `~/.agents/skills/`, which holds the `/recall` skill, and links to it in `~/.claude/skills`,
  `~/.codex/skills` and `~/.cursor/skills`.
- Agent settings, when you run `recall install`, in each agent folder it finds (so a second
  Claude Code or Codex account too):
  - the MCP server. Claude Code's entry is added with its own `claude mcp` command, Codex's is
    in `~/.codex/config.toml`, and Cursor's is in `~/.cursor/mcp.json`;
  - the hooks, in `~/.claude/settings.json`, `~/.codex/hooks.json` and `~/.cursor/hooks.json`,
    and, if you have them, `~/.gemini/settings.json` for Gemini CLI and one plugin file,
    `~/.config/opencode/plugins/recall.js`, for OpenCode;
  - `cleanupPeriodDays` in `~/.claude/settings.json`, only if you answer yes when asked (see
    "How long recall keeps chats").
- Autopilot, which is off unless you say yes: recall repeats the install by itself when it finds
  a new or incomplete agent folder, at session start and in the nightly run.
- The nightly job, when you enable it: a launchd agent in `~/Library/LaunchAgents` on macOS,
  or systemd user units in `~/.config/systemd/user` on Linux.

Before recall changes an agent settings file, it copies the file to `<name>.recall-backup`
beside it, and if the file has changed since, keeps a dated copy as well. `recall uninstall`
reverses each step. A file that did not exist has no backup.

## What the MCP tools can do

`recall serve` gives every agent nine tools. Eight of them (`search_chats`, `read_chat`,
`recent_chats`, `memory_search`, `read_note`, `project_memory`, `list_projects` and
`open_elsewhere`) only read: they never change your chats, your notes or your repositories.
They do touch recall's own files: the index catches up with new chats (and with commit links
when `recent_chats` is asked about a file or commit), and each call is added to `usage.jsonl`.

The ninth, `message_chat`, writes. It adds a message to recall's mailbox
(`cache/mail.jsonl`) for another chat. That chat's hooks hand the message to its agent on its
next edit, when its turn ends, or when you next write to it, and at the end of a turn it can
keep a finished agent going to answer. The message arrives quoted and marked as not from you.
An agent can only message chats recall introduced it to in the last day, using the token in
recall's note, at most ten messages an hour of 600 characters each. A finishing agent is kept
going at most three times an hour per sender. The token sits in the chat's transcript, so this
stops a misled agent, not one set on reading files. recall sets Codex to ask you before it
runs `message_chat`. It does not pre-approve the tool in Claude Code or Cursor, so their own
approval settings decide there.

## What the hooks can do

The hooks `recall install` adds run `recall hook` on an agent's events. Codex runs them only
after you trust them with `/hooks`. A hook calls no hosted service: it matches your prompt only
with a local embedding model, if you have one. What a hook adds to a session goes to that
agent's model, like the rest of the session. A hook can:

- **Add context.** Memory at session start and after compaction, and, mid-session, notes or an
  earlier chat that fit your prompt or a file the agent reads.
- **Refuse an edit once.** Before an agent edits a file another live chat has open changes to,
  the hook applies your `[coordination]` rules from `config.toml`. By default, when the other
  chat is editing the same lines of the same working copy (`same_copy = "ask"`), the edit is
  refused once, with a message that tells the agent to check with you; the agent may then
  retry. `worktree` refuses once and tells the agent to move to its own git worktree, `tell`
  lets the edit through with a note, and `off` does nothing.
- **Rewrite a subagent's prompt.** Only if you set `serve = true` under `[explore]` in
  `config.toml`, which is off by default. In Claude Code and Cursor, when an agent starts an
  exploration subagent, the hook then adds recall's earlier report on that repository to the
  subagent's prompt, if one answers the question.
- **Tell the other chat.** When two live chats have open changes to the same file, the hook
  leaves a note for the other chat in recall's mailbox saying which chat is about to change which
  lines, with a few words of what that chat was asked. The other chat's hooks hand it to its agent, which may
  belong to another vendor.
- **Keep an agent going.** When an agent finishes its turn and another chat has sent it a
  message, the hook tells it to carry on and answer (with the limits above).
- **Record.** When an exploration subagent finishes, the hook stores its prompt and final
  report in the index, redacted, for later exploration lookups.

## How long recall keeps chats

recall keeps its own compressed copy of every chat it indexes, so that a chat stays
searchable after the agent deletes its file. Claude Code removes transcripts after
`cleanupPeriodDays` (30 days by default). recall keeps its copy past that, including past a
shorter `cleanupPeriodDays` that you set yourself: lowering the setting does not remove
recall's copy. The only limit recall honours is an organisation's. When managed settings set
`cleanupPeriodDays`, recall forgets chats older than that. Codex and Cursor have no setting
that deletes chats on a schedule.

`recall install` offers to raise Claude Code's `cleanupPeriodDays` to ten years, so that the
agent keeps them too. To remove chats from recall, use `recall forget <chat id>` or
`recall forget --project NAME`, which also remove them from embeddings and exploration reports
(the notes that cite them are listed and left to you). Deleting `~/.agents/recall` removes the
index, the archive and everything else recall keeps there; the notes in `~/.agents/memory` and
any key in your keychain stay until you delete them.

## Secrets

recall redacts secrets before it writes a chat to the index. Each match is replaced with
`[REDACTED]`. The patterns cover private key blocks, common token formats (OpenAI, Anthropic,
GitHub, AWS, Slack, Google and Stripe), JWTs, database URLs that contain a password, and
assignments to names such as `key`, `secret`, `token` or `password`. Memory is built from the
index, so the model sees the redacted text.

Redaction is pattern matching. It errs toward redacting, but no pattern list catches every
secret. Do not paste secrets into agent chats. recall does not change the original chat files,
so a secret already in one stays there.

When the patterns change, recall rewrites the text it keeps and cannot read again from the
agents' files (extracted candidates, cached model answers, archived chats, exploration reports)
the next time it indexes, once per version of the rules. It does not rewrite your notes: it
names the ones the rules match in `logs/redact.log` and on stderr, and you edit them. A note
you committed keeps the old text in `~/.agents/memory`'s git history until you rewrite that
history yourself (`git filter-repo`, or start the repository again).

API keys never go in `config.toml`. The file holds only a reference to the key:

- `env:NAME` reads an environment variable.
- `keychain` uses the OS credential store: the macOS Keychain, or libsecret through
  `secret-tool` on Linux.
- `file` keeps the key in `keys.json`, created with mode 0600 so that only your user can read
  it. recall uses this when no keychain tool is available. The key is plain text in that file.

## Where chat excerpts go

Memory extraction sends chat excerpts to the model you chose. Where they go depends on the
provider:

| Provider | Chat excerpts go to |
| --- | --- |
| Local model (Ollama, LM Studio, llama.cpp) | nowhere, if the server runs on this machine (`localhost`, a loopback address or a unix socket), which is the default. A `base_url` on another host sends them there, and recall does not call that private. |
| ChatGPT, via the Codex CLI | OpenAI, under your plan's data terms |
| Claude, via Claude Code | Anthropic, under your plan's data terms |
| API key | the provider whose key you use: OpenAI, Anthropic or OpenRouter |
| Your server (`custom`) | the endpoint at `base_url` |

The subscription CLIs run with your MCP servers, hooks and plugins turned off, and with
API-key environment variables removed, so that recall's calls use your plan. recall has no
telemetry. Its only network traffic is the model calls you configured.

## Injected memory

Memory that recall gives an agent, at session start and mid-session when it fits your prompt or
a file, is wrapped in a `<recall-memory>` block. When chats are indexed, that block is stripped,
so injected memory is never learned again as if you had said it. Agents receive notes and
transcripts as background records, not as instructions. The current request and the code take
priority. That is guidance to the model, not a guarantee: notes are distilled from your chats,
which can include text an agent read from a web page or a file. You can read, edit or delete
any note in `~/.agents/memory`.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Open a private security advisory on
the recall GitHub repository: <https://github.com/alirng/recall/security/advisories/new>. Describe
what you found, the steps to reproduce it, and what you expected. Use made-up chats and fake
keys in the report.
