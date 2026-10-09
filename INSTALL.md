# Installing recall: instructions for a coding agent

The person asked you to install recall, a local tool that indexes their Claude Code, Codex
and Cursor chat history, keeps memory notes distilled from it, and gives every agent search
over both. Work through these steps in order. Run the commands yourself; stop and ask the
person only where a step says so. Everything installs into their home folder and nothing
needs root.

## 1. Check what is here

- The OS is macOS or Linux (`uname`). recall does not run on Windows yet.
- `uv --version` works. If it does not, ask before installing uv with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- At least one of Claude Code, Codex or Cursor is installed: `ls -d ~/.claude ~/.codex ~/.cursor`.
- Whether Ollama is running: `curl -s localhost:11434/api/tags`. It is optional, and enables a
  private local model and semantic search.

## 2. Ask one question: which model writes their memories

Every night recall sends digests of the day's chats to this model, with secrets redacted.
Tell the person where their chat text would go for each option, and let them choose:

| Option | Value for `--model` | Where chat excerpts go |
| --- | --- | --- |
| A local model through Ollama | `local:qwen3:4b` (after `ollama pull qwen3:4b`) | Nowhere: it stays on this machine |
| Their Claude plan, through Claude Code | `claude:haiku` | Anthropic, under their plan |
| Their ChatGPT plan, through Codex | `chatgpt:<model>`, a model their Codex offers (`chatgpt:` alone takes Codex's default) | OpenAI, under their plan |
| An API key (OpenAI, Anthropic, OpenRouter) | leave `--model` out | Tell them to run `recall model` afterwards. Never ask for, type or store an API key yourself. |

If Ollama is running, also offer semantic search: it finds chats described in other words
than they used, at the cost of a one-time download of `nomic-embed-text` (274 MB) and a few
minutes of indexing. Ask before pulling the model.

## 3. Install and set up

```bash
uv tool install 'recall-agents[embeddings]'
ollama pull nomic-embed-text            # only if they chose semantic search
recall setup --yes --model <their choice> --embeddings nomic-embed-text   # drop --embeddings if not
```

`recall setup --yes` imports every agent's history, tests the model with one call, connects
recall to each agent it finds (an MCP server, the `/recall` skill, and session-start hooks for
Claude Code and Codex; each config file is backed up beside itself first), schedules the
nightly memory run for 03:30, and starts the first memory run and the embedding in the
background. Indexing a large history can take a few minutes.

## 4. Check it worked

```bash
recall --version
recall stats                      # chats found per agent
recall search <a word they used in a recent chat>
recall context                    # what agents load at session start; empty until the first memory run finishes
```

If a step fails, read its message: they name the fix (a model that is not signed in, Ollama not
running, an agent not found). `recall install --dry-run` shows what the connection step would
change.

## 5. Tell the person

- Restart any open Codex and Cursor windows so they load the MCP server.
- In Codex, run `/hooks` once and trust recall's session-start hook; until then Codex can still
  use recall's tools but does not load memory automatically.
- Their notes live in `~/.agents/memory` (plain markdown, a git repository) and the index in
  `~/.agents/recall`. Nothing else is written outside those folders and the agent configs.
- `recall projects suggest --write` drafts a list of their projects from their history, which
  they can then edit; `recall rules` shows which instructions each agent reads in a repo.
- To undo everything: `recall uninstall && recall schedule disable && uv tool uninstall recall-agents`.
