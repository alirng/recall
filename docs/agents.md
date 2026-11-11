# Other coding agents

recall's three main agents are Claude Code, Codex and Cursor: it reads their history, sets up their
MCP server, skill and hooks, and turns their chats into notes. Beyond those it is **compatible** with
37 other agents: it reads the history of 35 of them, and for Windsurf and Warp it only registers its
MCP server. Compatible means two things and nothing more:

- **History.** recall reads the agent's local chat history into the index, read-only, so `search_chats`
  and `read_chat` (in any agent) find those chats, labelled with the agent's name and the folder it ran
  in when the agent records one.
- **MCP.** Where the agent documents an MCP config file, `recall install` lists recall's server there, so
  the agent can search and read every other agent's chats. It backs the file up first
  (`<file>.recall-backup`), leaves your other entries alone, and `recall uninstall` takes the entry out
  again (and removes the file if recall made it).

There are no session-start hooks, subagent briefs or edit hooks for these agents (Gemini CLI and OpenCode
keep the edit hooks they already had), and their chats do not feed the nightly notes. To let one, set
`mine = true` for it in `config.toml`:

```toml
[roots.opencode]
mine = true      # `recall dream` turns this agent's chats into notes (default: off)
index = false    # do not read this agent's chats at all
connect = false  # `recall install` leaves its MCP config alone
```

The names are the keys in the table below. `recall index --source opencode` reads one agent, and
`recall search --agent opencode` filters by it. With `sources = []` in `config.toml` (the default) every
agent found is read; a list of names limits it.

## What is supported

"History" is where recall reads from (macOS and Linux; each agent's own variable, such as
`GEMINI_CLI_HOME` or `KIRO_HOME`, moves it). "MCP" is the file `recall install` edits.

| Agent (key) | History | MCP file |
| --- | --- | --- |
| OpenCode (`opencode`) | `~/.local/share/opencode/opencode.db`, both schema families | `~/.config/opencode/opencode.json` |
| Kilo Code (`kilo`, `kilo-legacy`) | `~/.local/share/kilo/kilo.db`; the extension's `tasks/` in VS Code's globalStorage | `~/.config/kilo/kilo.json` |
| Antigravity (`antigravity`) | plaintext `brain/<id>/.system_generated/logs/transcript_full.jsonl`; for older chats the agent's own plan files | `~/.gemini/config/mcp_config.json` and `~/.gemini/antigravity/mcp_config.json` |
| Gemini CLI (`gemini`) | `~/.gemini/tmp/*/chats` (JSONL, and the JSON before 0.39) | `~/.gemini/settings.json` |
| Qwen Code (`qwen`), iFlow (`iflow`) | `~/.qwen/projects/*/chats`, `~/.iflow/projects` | `~/.qwen/settings.json` |
| Grok Build (`grok`) | `~/.grok/sessions` | `~/.grok/config.toml` |
| Kimi Code (`kimi`) | `~/.kimi-code/sessions/*/*/agents/*/wire.jsonl` | `~/.kimi-code/mcp.json` |
| Pi (`pi`) and its forks Oh My Pi (`omp`), Prime Agent (`prime`), OMO (`omo`), StepCode (`stepcode`), Senpi (`senpi`), gajae-code (`gjc`), Kimchi (`kimchi`) | each fork's `sessions/` under its own agent folder | none: Pi has no MCP client |
| OpenClaw (`openclaw`) | `~/.openclaw/agents/*/sessions/*.jsonl` and its SQLite store | none yet |
| GitHub Copilot CLI (`copilot`) | `~/.copilot/session-state/*/events.jsonl` | `~/.copilot/mcp-config.json` |
| VS Code Copilot Chat (`vscode`) | `workspaceStorage/*/chatSessions` in VS Code, Insiders and VSCodium | `<User>/mcp.json` (VS Code) |
| Factory Droid (`droid`) | `~/.factory/sessions` | `~/.factory/mcp.json` |
| Kiro (`kiro`) | `~/.kiro/sessions` (CLI and IDE), the classic CLI's `data.sqlite3` | `~/.kiro/settings/mcp.json`, and the skill in `~/.kiro/skills` |
| Cline (`cline`) | `~/.cline/data/sessions`, and the older `tasks/` in VS Code, Cursor and Windsurf's globalStorage | `~/.cline/data/settings/cline_mcp_settings.json` |
| Roo Code (`roo`) | `tasks/` in the same editors' globalStorage | none |
| Goose (`goose`) | `~/.local/share/goose/sessions/sessions.db` | none yet (YAML) |
| Crush (`crush`) | each project's `.crush/crush.db`, found through `~/.local/share/crush/projects.json` | `~/.config/crush/crush.json` |
| Devin CLI (`devin`) | `~/.local/share/devin/cli/sessions.db` | `~/.config/devin/mcp_config.json` |
| Hermes Agent (`hermes`) | `~/.hermes/state.db` and its profiles | none yet (YAML) |
| Continue (`continue`) | `~/.continue/sessions` | none yet (YAML) |
| Claude Cowork (`cowork`) | local sessions in `local-agent-mode-sessions` in the Claude desktop app's folder; tasks run in the cloud are not on disk | none |
| Zed (`zed`) | `threads/threads.db` (zstd bodies) | none: its settings file takes comments |
| Amp (`amp`) | legacy local threads `~/.local/share/amp/threads` (Amp keeps no local chats now) | `~/.config/amp/settings.json` |
| Muse Code (`muse`), Mistral Vibe (`vibe`), Command Code (`commandcode`), Junie (`junie`) | `~/.local/share/muse/sessions`, `~/.vibe/logs/session`, `~/.commandcode/projects`, `~/.junie/sessions` | Junie: `~/.junie/mcp/mcp.json` |
| Windsurf, Warp | not read (see below) | `~/.codeium/windsurf/mcp_config.json`, `~/.warp/.mcp.json` |

A config file with comments (VS Code's, Amp's and OpenCode's `.jsonc` can have them) is reported and left
alone: `recall install` says which file and asks you to add the server by hand.

## claude.ai and ChatGPT chats

Both services keep their chats on the server, including the ones from the Claude desktop app's Chat tab, so recall reads them
from the account export. Request one, download the ZIP from the email, then bring it in one of three ways: drop it on **Import
chats** on the Mac app's This Mac page, tell your agent where the file is, or run `recall import`. recall works out which service
it came from.

- **claude.ai:** Settings > Privacy > Export data.
- **ChatGPT:** Settings > Data controls > Export data.

```bash
recall import ~/Downloads/data-export.zip
```

Chats are labelled `claudeai` or `chatgpt` and have no project. Run it again with a later export to pick up what changed:
nothing updates between imports. recall keeps message text and attachment names, but not thinking or tool calls, and for an
edited or regenerated ChatGPT chat only the final version. It keeps its copy in the index and archive, so you can delete the
ZIP, and `recall forget` removes a chat for good. Set `mine = true` under `[roots.claudeai]` or `[roots.chatgpt]` to let the
chats feed the nightly notes. Neither service documents its export format fully, and a chat in a shape recall does not know is
skipped.

## What is not read, and why

- **Windsurf Cascade and Devin Desktop** keep conversations as AES-GCM protobuf. One competitor decrypts
  them with a key baked into the app. recall does not decrypt anything, and has decided not to read them.
- **ChatGPT desktop's classic chats** are encrypted (recall does not decrypt them), and Chat mode is server-side. Import the account
  export instead (above).
- **The Claude desktop app's Chat tab** is claude.ai, and its chats live on the server. The app's local cache holds only a few
  recently opened ones, so recall does not read it; import the account export instead (above). The Code tab is Claude
  Code, which recall already reads.
- **Antigravity's older conversations** (`conversations/*.pb`) are encrypted. For those chats recall reads
  the Markdown plan files beside them, which hold what the agent wrote but not what you typed.
- **T3 Code** wraps Claude Code and Codex, whose stores recall already reads, so its chats appear as
  Claude Code and Codex chats and are never indexed twice.
- **Warp** keeps its history in a container macOS blocks by default; **Aider** writes one Markdown file per
  repository, which would need a scan of every repository; **Positron Assistant, JetBrains AI Assistant,
  TRAE and a long tail of one-reader agents** are not built.
- **Other agents' own imports**: Cline's sessions imported from another agent are skipped, since that
  agent's reader has them.

## Known limits

- **YAML and commented configs.** Goose, Hermes and Continue keep MCP servers in YAML, and Zed's (and sometimes
  VS Code's, Amp's or OpenCode's) settings file allows comments. recall does not rewrite those: add the server
  by hand (`recall install` names the file).
- **One chat, two copies.** After Senpi's first-run move into Pi's folder the same chat can sit under both
  roots and be indexed under both agents. A Grok Build `--fork-session` copy repeats the turns of the chat it
  was forked from, and a Gemini CLI `.json` and `.jsonl` of one session are de-duplicated only when their file
  names match.
- **Kiro's classic CLI database.** `kiro-cli/data.sqlite3` may hold an auth table beside the conversations.
  recall lists the table names and reads only `conversations_v2`, but it does open the file.
- **Formats checked on real installs.** Only OpenCode and Antigravity's old plan files were checked against
  real data. The rest follow other tools' parsers and synthetic fixtures; a format that changes makes a
  reader skip the chat, never fail the index.
- **Upgrading.** With `sources = []` (the default) every agent found is read, so an existing install starts
  reading these agents after the upgrade, and the chat tables rebuild once (about three minutes with a large
  Cursor history).

## How it behaves

- **Cheap when absent.** Each reader checks that the agent's folder exists with stat calls only, and does
  nothing else on a machine that does not have the agent.
- **Read-only.** SQLite files open with `mode=ro` (never `immutable`, which would ignore the write-ahead
  log). recall never opens an agent's auth, token or credentials file and never writes to its folder.
- **No injected context.** Hook output, system reminders, IDE context blocks and tool output are not
  indexed. Failed tool calls go to recall's tool-errors table where the format records them.
- **Changing formats.** A format recall does not recognise is skipped with no error. A reader's
  `CHAT_SCHEMA` string is part of the chat schema hash, so upgrading to a release that adds or changes
  a reader rebuilds the chat tables once from the agents' files (chats whose files are gone come back
  from recall's archive).
- **Subagents** are marked as such only where the agent's format says so.

## Sources of format knowledge

The formats are the agents' own and mostly undocumented by their vendors, so recall's readers follow
the permissively licensed parsers of other tools, read as documentation (no code is copied):
[deja-vu](https://github.com/vshulcz/deja-vu) (MIT),
[agentsview](https://github.com/kenn-io/agentsview) (MIT),
[franken_agent_detection](https://github.com/Dicklesworthstone/franken_agent_detection) (MIT),
[SpecStory](https://github.com/specstoryai/getspecstory) (Apache-2.0) and
[agent-sessions](https://github.com/jazzyalex/agent-sessions) (MIT), together with the agents' own source
where it is public. `cass` and `cass_memory_system` are MIT with a rider that restricts OpenAI and Anthropic and
their affiliates, so nothing is taken from them. ByteRover's Elastic License 2.0 allows reading
its list of supported agents but not reuse, so only facts were taken. Per-agent paths, formats and the
sources behind them are in [docs/research/agent-storage-and-integration.md](research/agent-storage-and-integration.md).

## Adding an agent

See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-an-agent-source). Each reader has synthetic fixtures in
`docs/benchmarks/agent_fixtures/` (never a real chat) and a scenario in `docs/benchmarks/setup_eval.py`
(`uv run python docs/benchmarks/setup_eval.py --scenario agents-<module>`) that checks it finds the chat,
leaves the agent's files untouched and installs and uninstalls cleanly.
