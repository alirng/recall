# recall: agent instructions

Python 3.11+, managed with uv. Runtime deps: `mcp` 2.x (`mcp.server.mcpserver.MCPServer`), `pyyaml`, `jsonschema`; `anthropic` is the optional `recall[anthropic]` extra.

- Run from the repo with `uv run recall <cmd>`. The installed `recall` (`uv tool install --editable .`) is what hooks, the MCP server and the nightly job run, so after changing dependencies run `uv tool install --editable . --force`.
- Before committing: `uvx ruff format src && uvx ruff check src` (CI pins the ruff version; match it when CI and local disagree).
- Layout and extension points are in README "Development". Read that before adding an agent source or a model provider.
- recall never assumes a model vendor. Provider choice lives in `config.toml` `[model]`; code paths go through `llm.build()` / `llm.from_spec()` and the `Provider` interface.
- The chat index is derived data: bump `CHAT_SCHEMA_VERSION` in `chats/store.py` when parsing changes, and it rebuilds itself. Memory tables and notes are not derived, so never drop them.
- A prompt change bumps the version line at the top of its file in `memory/prompts/`. Compare `uv run recall eval memory` before and after, averaging several `--fresh` runs: single runs vary by ±12 points.
- When search or memory misses something real, add a case to the personal evals in `~/.agents/recall/evals/` before changing code. Those files name real chats and stay out of the repo.
- Agents' chat history is read-only (`mode=ro` for SQLite): never write to Cursor's `state.vscdb`, `~/.codex/sessions` or `~/.claude/projects`. Only `recall install` touches agent settings files, and it backs each one up first.
- The index, notes, keys and eval sets hold private content. Never commit them or copy them into the repo.
- No unit tests. The checks are evals and running the CLI, MCP server and hook end to end.
