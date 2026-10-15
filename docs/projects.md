# Projects and rules

A project you work on is rarely one folder. It is a repo cloned twice, a dozen worktrees, the
folder it lived in before you moved it, a second repo for the web app, and the chats you had
with no folder open at all. recall keeps a list of your projects in
`~/.agents/memory/projects.yaml`, and follows each one across all of that:

```bash
recall projects suggest            # draft the list from your chat history; --write adds it
recall projects                    # every project over time, as git-style lanes
recall projects show harbor        # where its chats ran, which agents, the latest chats
```

```text
                            May   Jun   Jul   Aug   Sep   Oct
harbor            work      ●─────●─────●─────●─────●─────●       412 chats
harbor-mobile     work                  └─────○─────●─────○       23 chats · from harbor
blog              personal                    ○─────●─────●       76 chats
tidepool          personal  ○                                     190 chats · paused
```

`●` is a month with ten or more chats, `○` a quieter one, `┄` a stretch with no chats, and `└` a
project that grew out of the one above it. The file is yours: rename and merge entries, list the
folders each one spans under `paths`, set `kind` (work or personal), `status` (active, paused,
done or dropped) and `from`. recall only appends to it, and the memory folder's git history
keeps every version. `words` place chats that ran in no project folder: a chat whose title or
first message names one of them joins that project. Project names work in `search_chats`,
`recent_chats` and `memory_search`, and the session-start hook loads the notes of every folder a
project spans.

## The same rules in every agent

Each agent reads its own instruction files: Cursor its `.cursor/rules`, Codex `AGENTS.md`,
Claude Code `CLAUDE.md` (and `AGENTS.md` only when there is no `CLAUDE.md`, or when it
imports it). A repo set up well for one agent is a stranger to the others. `recall rules`
shows which agent reads what:

```text
$ recall rules harbor
Instructions                                   Cursor       Claude Code  Codex        applies
AGENTS.md (22)                                 ✓            ✓            ✓            always
.cursor/rules/staging-database.mdc (36)        ✓            ·            ·            always
.cursor/rules/commit-style.mdc (27) personal   ✓            ·            ·            always
…
Claude Code misses 5 always-on rules (190 lines).
```

With `--write` it copies the committed always-on Cursor rules into a marked block in
`AGENTS.md`, which all three agents read, and adds `@AGENTS.md` to a `CLAUDE.md` that lacks
it. It shows the diff first and writes nothing without `--write`; run it again after you
change a rule. Gitignored rules are personal, so they never go into a committed file;
instead the session-start hook gives Claude Code and Codex one line for each, with the
file to read. MCP servers and skills are reported per agent and left alone.
