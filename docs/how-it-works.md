# How it works

How recall reads chats, writes notes, and hands them to agents.

```text
 Claude Code ─┐                                    ┌─ dream (nightly) ──────────────────────────┐
 Codex ───────┼─ parse & clean ─ redact ─ SQLite ──┤  digest → extract → consolidate → notes     │
 Cursor ──────┘                          (FTS5)    │          (batched)   (per project)  (git)   │
                                                   └────────────────────────────────────────────┘
               ↓ search_chats, read_chat                      ↓ MEMORY.md indexes
          MCP server · /recall skill · CLI            session-start hook · memory_search
```

1. **Index.** Each agent's history is parsed into your turns and the agent's turns. Tool
   output and text the tools inject (system reminders, plan-mode boilerplate) are dropped,
   and secrets are redacted before anything is stored. Headless runs and Codex subagents stay
   searchable but are never mined for memory; Claude Code's subagent transcripts are not
   indexed. Clones and worktrees of one repo share a
   project, keyed on the git remote. recall keeps its own compressed copy of every chat it
   indexes, so a chat stays searchable after its agent deletes or rewrites the file (see
   [Retention and forgetting](reference.md#retention-and-forgetting)).
2. **Digest.** Each chat shrinks to your words plus the start and end of each agent reply,
   which is where agents say what they will do and what they did.
3. **Extract.** The model reads each chat on its own and proposes candidate memories
   (`preference`, `fact`, `decision`, `lesson`, `open`), each with a supporting quote. It
   decides first whether each one holds beyond the task at hand, and drops those that don't.
   A quote that can't be found in the chat demotes the candidate to low confidence.
4. **Consolidate.** Per project (and for global and unfiled notes), oldest first, candidates are
   folded into notes: create, update, supersede (newer facts win) or discard. Every operation
   is validated before a file changes. Open threads expire after 14 days, and a low-confidence
   note from a single chat after 180.
5. **Commit.** The `MEMORY.md` indexes are rebuilt, and the run becomes one git commit in
   `~/.agents/memory`.

Each step resumes where the last run stopped, so a crash or a spent budget only delays the
rest until the next night.

## Memory notes

```text
~/.agents/memory/
  MEMORY.md                      how you work, and your projects
  projects.yaml                  your projects and the folders each one spans
  global/<note>.md               cross-project preferences
  projects/<repo>/MEMORY.md      what an agent loads when it starts in that repo
  projects/<repo>/<note>.md      facts, decisions, lessons, open threads
  unfiled/<note>.md              facts from chats outside any project folder
  archive/                       superseded and expired notes
```

A note is plain markdown with a little YAML:

```markdown
---
id: run-migrations-through-make-3f2a
type: lesson
scope: project
title: Run migrations through make
project: github.com/acme/harbor
project_name: harbor
confidence: high
status: active
created: '2026-09-12'
updated: '2026-09-30'
last_seen: '2026-09-30'
sources:
- codex:0192f3a8-5c1e-7d2b-9e4f-3a6b8c1d2e4f
- claude:7c41e9b2-1f3a-4d5e-8b6c-9a0f2e3d4c5b
---
Run database migrations with `make migrate`, never by hand: the Makefile also seeds
the audit tables. Why: two hand-run migrations left staging without audit rows.
```

Each note ends with wikilinks to its closest notes, which recall rewrites after every run.
Two notes are linked when they share distinctive words, more strongly when they came from
the same chat or project; no model call is involved. A `[[note-id]]` you write in a note's
body is a link too, and recall leaves it alone.

The files are the source of truth. Edit them, and recall keeps your edits; delete one, and
it is never learned again. The folder is a git repository, so `git log` shows what each
night changed and `git revert` undoes it. It also opens as an Obsidian vault, though nothing
requires Obsidian. Why markdown rather than a vector or graph store:
[docs/research/agent-memory-prior-art.md](research/agent-memory-prior-art.md).

Secrets are redacted before a note is written. If a rule is added later that would have
caught something already in a note, recall does not rewrite the file: after the first index
run with the new rules it names the notes (and the entries of `.recall/notes.json`) that the
rules match, on stderr and in `~/.agents/recall/logs/redact.log`. Edit them by hand. The git
history still holds every earlier version, so a secret that was committed stays in it until
you rewrite the history (`git filter-repo`) or start the repository again.

## How agents use it

| Agent | History import | MCP tools | `/recall` skill | Memory at session start | Edit rules |
| --- | --- | --- | --- | --- | --- |
| Claude Code | yes | yes | yes | automatic, via a session-start hook | edit hook |
| Codex | yes | yes | yes | automatic, via a session-start hook you trust once in `/hooks` | edit hook, trusted once in `/hooks` |
| Cursor | yes, and older chats on macOS | yes | yes | on request, via the `project_memory` tool | edit hooks |
| T3 Code | through the agent it runs | through that agent | through that agent | through that agent | the agent's own hooks |
| OpenCode | no | add it yourself | no | via `project_memory` | edit plugin |
| Gemini CLI | no | add it yourself | no | via `project_memory` | edit hooks |

Agents whose hooks recall does not set up yet (Antigravity, Devin, Copilot, Cline, Kiro) can
call the `open_elsewhere` MCP tool before editing, but their own edits stay invisible to the
other chats until recall reads their history or hooks into them.

- **Automatic:** the session-start hook loads your working preferences, a line for each
  always-on rule the repo gives another agent but not this one (a Cursor rule, say, in a
  Claude Code session), and the current project's notes: a body of at most 4,500 characters
  (about 5,000 with its framing), in which the project's notes always get room.
- **Mid-session:** the same hook command can hand over memory at the moment it applies,
  and stays quiet unless it is sure. When your prompt closely matches an earlier chat, the
  agent gets a line saying when and where you worked on it and how that chat ended. When it
  reads a file, it gets the rules scoped to that file that it would not load itself, notes
  that name the file, and the last chat that talked about it. Each item comes at most once
  per session, at most 1,200 characters at a time. A preference or a past chat tied to one
  context reaches only that context's sessions (a chat outside every context only sessions
  outside every context), as at session start; searching chats is not narrowed. The hooks
  read the index without writing to it, and stay quiet while an upgrade has it to rebuild.
  The gate for prompts is fitted on one
  developer's history and fires rarely (about one prompt in eight in a held-out test);
  `recall eval jit --write` fits it to yours. It reads the prompt's meaning only with a local
  embedding model, so a hosted one never receives your prompts from a hook. Claude Code and
  Codex get prompts and reads (Codex reads are shell commands such as `cat` and `sed -n`);
  Cursor gets them too, its prompt answer read from the 3.23 app rather than its docs. Past
  errors and notes after compaction are wired but rarely fire yet: the index does not keep
  tool errors, and notes seldom clear the gate. `recall jit "<prompt>"` shows what would fire
  and why.
- **On demand:** eight read-only MCP tools (`search_chats`, `read_chat`, `recent_chats`,
  `memory_search`, `read_note`, `project_memory`, `list_projects`, `open_elsewhere`) and
  `message_chat`, which leaves a message for another chat in recall's own mailbox.
- **Explicit:** type `/recall what did we decide about auth` in any agent with skills.

Agents treat notes and transcripts as background, not instructions: the current request and
the code win when they disagree.

To keep work and personal memory apart, name each context's folders in `config.toml`:

```toml
[contexts]
work = ["~/Projects/Acme"]
personal = ["~/Projects/Personal"]
```

A preference that holds everywhere (communication style, commit policy) then loads in every
folder; one tied to a context loads only in the folders of the contexts it was learned in, and
one only ever seen in one project is listed with that project. A preference tied to a context
but learned only in chats outside every context's folders loads nowhere until you place it. The
nightly run makes that judgement once per note, only while `[contexts]` is set, and keeps it as
`reach:` in the note's frontmatter, so you can correct it by hand. Until a note is judged it
loads everywhere, as it did before contexts.

## What recall is used for

recall logs its own use to `~/.agents/recall/usage.jsonl`: each MCP call, session-start load
and search, with the agent that made it, the words asked and the ids that came back, never
chat or note text. `recall usage` sums it up: which agents use which tools, how many
searches found nothing, how many led to opening a result, and what the mid-session hooks
injected (ids, plus the repo-relative path of a file whose read brought them). `recall eval jit --live` has a judge label those injections, so you can
see how often they were relevant. It stays on your machine; set
`usage_log = false` in `config.toml` to stop it.
