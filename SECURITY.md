# Security

recall reads your coding agents' chat history, and it sends parts of that history to a model
you choose. This page says what it reads, where it writes, how secrets are handled, and how to
report a problem.

## What recall reads

recall reads the chat history that each agent keeps on disk:

- Claude Code: `~/.claude/projects`
- Codex: `~/.codex`
- Cursor: `~/.cursor/projects`, and Cursor's user data folder
  (`~/Library/Application Support/Cursor/User` on macOS)

It also reads its own index and its memory notes. It never writes to an agent's history.
SQLite databases are opened read-only.

## What recall writes

- `~/.agents/recall/` (or `RECALL_HOME`): the search index, `config.toml`, logs, and
  `keys.json` if you chose file storage for a key.
- `~/.agents/memory/` (or `RECALL_MEMORY`): memory notes as markdown. This folder is a git
  repository, and each memory run is one local commit. recall never pushes.
- `~/.agents/skills/`, which holds the `/recall` skill, and links to it in `~/.claude/skills`,
  `~/.codex/skills` and `~/.cursor/skills`.
- Agent settings, when you run `recall install`: the MCP server and the session-start hook.
  Claude Code's MCP entry is added with its own `claude mcp` command, the hook is in
  `~/.claude/settings.json`, Codex's entry is in `~/.codex/config.toml`, and Cursor's entry is
  in `~/.cursor/mcp.json`.
- The nightly job, when you enable it: a launchd agent in `~/Library/LaunchAgents` on macOS,
  or systemd user units in `~/.config/systemd/user` on Linux.

Before recall changes an agent settings file, it copies the file to `<name>.recall-backup`
beside it. `recall uninstall` reverses each step. A file that did not exist has no backup.

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
| Local model (Ollama, LM Studio, llama.cpp) | nowhere, if the server runs on this machine, which is the default. A `base_url` on another host sends them there. |
| ChatGPT, via the Codex CLI | OpenAI, under your plan's data terms |
| Claude, via Claude Code | Anthropic, under your plan's data terms |
| API key | the provider whose key you use: OpenAI, Anthropic or OpenRouter |
| Your server (`custom`) | the endpoint at `base_url` |

The subscription CLIs run with your MCP servers, hooks and plugins turned off, and with
API-key environment variables removed, so that recall's calls use your plan. recall has no
telemetry. Its only network traffic is the model calls you configured.

## Injected memory

Memory that recall gives an agent is wrapped in a `<recall-memory>` block. When chats are
indexed, that block is stripped, so injected memory is never learned again as if you had said
it. Agents receive notes and transcripts as background records, not as instructions. The
current request and the code take priority.

All six MCP tools are read-only. They search and read the index and the notes.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Open a private security advisory on
the recall GitHub repository: <https://github.com/alirng/recall/security/advisories/new>. Describe
what you found, the steps to reproduce it, and what you expected. Use made-up chats and fake
keys in the report.
