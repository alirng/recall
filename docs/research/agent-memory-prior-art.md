# Agent memory: prior art, evidence, and recommendations for recall

Research note for `recall`. Verified 2026-10-06 against live vendor docs, pinned GitHub source, and arXiv. Question: how should a local tool store, consolidate and retrieve coding-agent memory, and how do existing systems do it?

## 0. How to read this

- **Citations.** Every factual claim ends with a bracketed key such as [cc-memory]. Each key is a link; all link targets are listed in the reference block at the very end of the file. `(inference)` marks my own reasoning, `(judgment)` marks a proposed value that no source fixes.
- **Evidence grades.** *Docs* say what a vendor intends, *source* shows what code does, and benchmark numbers are almost all *vendor-reported* (measured by the party selling the system) unless marked *independent*. Where docs and source disagree, both are given.
- **Pinning.** OpenAI Codex source is pinned to commit `822e58c` (2026-10-06). The Agent Memory Repo spec is pinned to `1db04a5` (2026-10-05). Claude Code facts carry the version that introduced them where the changelog gives one.
- **Two OpenAI pages** (ChatGPT memory announcement and Help Center article) return HTTP 403 to automated fetches, so I read Wayback Machine copies (2026-10-01 and 2026-09-19) and link those.
- **Local observation.** One fact (the frontmatter keys Claude Code writes into its own memory files) comes from inspecting key names, not contents, of local files on one machine. It is marked "observed".
- **Out of scope.** Where each agent stores chats, MCP config paths and hook payload schemas are covered in the companion note [sibling-doc]. This note only verifies the three context-injection routes that recommendation R4 depends on, and its hook facts agree with that note's hook table.
- **Known gaps** (things I looked for and could not establish):
  - A Claude Code "auto dream" / `/dream` feature. Third-party blogs describe it [3p-autodream-1], [3p-autodream-2]; the full Claude Code docs text (8.8 MB) contains no occurrence of "dream" [cc-llms-full] and neither does the changelog through v2.1.291 [cc-changelog]. Treat as unverified.
  - Why Cursor removed Memories. Staff stated the removal and a migration path but no rationale in the threads I found [cursor-forum-gone].
  - ChatGPT's internal retrieval design. OpenAI documents behavior and controls, not mechanism [chatgpt-help].
  - The internals of Anthropic's Dreams pipeline and Devin's dreaming pass. Only inputs, outputs and behavior are documented [api-dreams], [devin-blog].
  - No independent head-to-head benchmark of memory architectures for *coding* agents exists in what I found. Everything on conversational benchmarks is vendor-measured or contested (section 7).
  - Boris Cherny's post [bcherny-x] was read from a search-engine snippet; the page requires login.

## Summary of findings

1. **Convergence on files.** Claude Code, Codex, Devin, Letta and Cognition's open spec all store memory as markdown files behind a short index or summary that is loaded at session start, with details read on demand [cc-memory], [codex-readme], [devin-blog], [letta-docs-memfs], [amr-spec]. Letta (the MemGPT team) moved from tool-edited blocks plus a vector archive to git-tracked markdown with no vector index by default [letta-docs-memgpt], [letta-docs-memfs].
2. **Capture is cheap, consolidation is the product.** Every major system now has a separate background consolidation pass: Devin "dreaming" (announced 2026-10-05) [devin-blog], Anthropic Dreams (research preview, 2026-05-06) [api-dreams], Codex two-phase pipeline [codex-readme], Letta "dreaming" [letta-docs-memory].
3. **Staleness is the dominant failure mode, and append-only can be worse than no memory.** In a controlled reversal test an append-only store scored 0.210 against 0.309 for no memory at all, while explicit revocation scored 0.950 [tepa]. Qwen3 models from 0.6B to 8B answered with a stale stored value 0.92-1.00 of the time when the stored fact was the only source [trustgap], and the best of 25 frontier models reached only 52.8% when preferences had evolved [horizonbench]. Mem0 nonetheless moved to ADD-only extraction in April 2026, with time-aware ranking at read time [mem0-readme] (vendor-reported).
4. **Provenance is universal but not self-enforcing.** Devin links notes to sessions [devin-blog], Copilot cites code locations and re-validates them at use time [copilot-docs], Codex cites rollout line ranges [codex-read-v1]. Yet in a controlled scenario with a two-record verification budget, 16 models inspected a superseded constraint's provenance in only about 1 of 5 episodes and then decided consistently with the stale constraint about 75% of the time [budgeted-verif]. Verification should therefore be automated by the memory system, not left to the consuming agent (inference).
5. **Extraction prompts carry the quality.** The most detailed published production prompts (Codex's, in open source) require evidence from the user's words rather than assistant suggestions, allow a no-op, forbid turning "do X now" into "user prefers X", and treat transcript text as untrusted data [codex-stage1-v1], [codex-stage1-v2].
6. **Memory writes are an injection channel.** Researchers persisted attacker instructions through ChatGPT's and Windsurf's memory features [rehberger-chatgpt], [rehberger-windsurf]; Anthropic warns that a read-write memory store lets one injected session poison later "trusted" sessions [api-ma-memory]; Codex ships a switch that excludes threads that touched MCP or web content from memory generation [codex-config-ref].
7. **The always-loaded layer must be tiny, and the write path must know the read-path cap.** Verified limits: Claude Code hook context 10,000 characters, MCP server instructions 2,048 characters, Codex hook output about 2,500 tokens, and, as a precedent for always-on size, Windsurf's 6,000-character global rules file [cc-hooks], [cc-changelog], [codex-hooks], [windsurf-memories]. LLM-written context files raised inference cost by over 20% without raising task success [agents-md-study]. Silent truncation is a reported failure on both sides: a Claude Code user cannot tell whether the memory index loaded whole [cc-issue-82056], and a Codex user measured about 60% of a 6.1k-token summary (including the newest entries) cut from the middle because the injection cap is 2,500 tokens while the consolidation prompt had no numeric budget [codex-issue-48679].
8. **Scoping bleed is a real, reported bug class.** Codex injects the whole global summary into every repository; maintainers closed the report as by design [codex-issue-17496] and a request for project scope remains open [codex-issue-18343]. Claude Code users report facts written to a different project's store [cc-issue-82056]. Recurring Codex automations can dominate the global store because repetition is mistaken for importance [codex-issue-33641].
9. **Background consolidation has a cost problem.** A user reported Codex memory generation draining quota while the app was idle [codex-issue-19732]; the next day commits added a rate-limit guard and moved the trigger to the first real user turn with a cooldown, citing quota use at "exactly the wrong time" and "ghost cost" [codex-commit-19990], [codex-commit-19970]. Another user reported a hidden consolidation subagent consuming a 5-hour quota after a hook failure [codex-issue-22008].
10. **The trend is toward explicit, reviewable files, whether or not capture is automatic (inference).** Some vendors retreated from auto-generated memory: Cursor removed Memories in 2.1.x [cursor-forum-gone]; Windsurf recommends Rules over auto-memories and its new default agent does not persist memories [windsurf-memories]; Mem0 removed its local OpenMemory server from the monorepo [openmemory-removal]. Others expanded automatic capture but made the result plain files with a consolidation pass: Devin [devin-blog], Letta [letta-docs-memfs], Anthropic Dreams [api-dreams], and Codex's simplified v2 with a summary file plus rollout summaries [codex-commit-43813].
11. **Benchmarks are weak guidance.** An independent audit found 6.4% of LoCoMo answers wrong and a judge that accepted 62.81% of deliberately vague wrong answers [locomo-audit]; vendors report mutually incompatible state-of-the-art results (section 7.2).

## Comparison table

One row per system. "Load" means what reaches the model without the agent asking.

| System | Store and format | Capture and consolidation | Load at session start | Scope and provenance | Lifecycle |
| --- | --- | --- | --- | --- | --- |
| Claude Code auto memory [cc-memory] | Markdown files, `MEMORY.md` index plus topic files, frontmatter `type` (user, feedback, project, reference) and `modified` | Agent writes during sessions; no documented consolidation (reminders to compact the index) | First 200 lines or 25 KB of `MEMORY.md`; topic files read on demand | Per git repo, shared across worktrees; machine-local; no per-note provenance documented | Exempt from transcript cleanup; user or agent edits |
| Claude API memory tool [api-memory-tool] | Client-side `/memories` directory, six file commands | Agent-driven; app handles expiry | Protocol prompt tells the model to view `/memories` first | App-defined | Docs advise expiring unaccessed files |
| Claude Managed Agents memory and Dreams [api-ma-memory], [api-dreams] | Text documents in a store mounted as a directory; immutable versions | Agents write; a Dream reads store plus up to 100 sessions and writes a new store | Mount note added to system prompt | Per store; versions give an audit trail | Versions kept 30 days; Dreams merge duplicates and replace stale entries |
| Codex memories [codex-readme] | Markdown under `~/.codex/memories/`, git baseline, SQLite job state | Phase 1 per-thread extraction, Phase 2 global consolidation agent | `memory_summary.md` injected; `MEMORY.md` grepped on demand | Working-directory scoped blocks; rollout ids and line-range citations | Usage-ranked selection; unused after 30 days dropped |
| Devin Memory and Dreaming [devin-blog] | Git repo of markdown ("Memory Drive") | Agent writes notes; daily dreaming pass | `MEMORY.md` | Personal per org; each note links to its source session | Dreaming removes transient and unused notes |
| ChatGPT memory [chatgpt-help] | Opaque summary plus saved memories | Automatic from chats; "improved memory" keeps a summary current | Relevant context chosen per response | Per account; optional project-only; "Sources" shown | User correction; logs of deleted memories kept up to 30 days |
| Cursor Memories (removed) [cursor-1-0], [cursor-forum-gone] | Per-project, per-user, managed in Settings | Auto-generated, beta from 1.0 | n/a | Per project | Removed from 2.1.17; export to Rules |
| Windsurf Memories [windsurf-memories] | Local files in `~/.codeium/windsurf/memories/` | Auto-generated by Cascade | Retrieved when judged relevant | Per workspace, not committed | Docs recommend Rules; new default agent keeps none |
| GitHub Copilot memory [copilot-blog], [copilot-docs] | Hosted facts (subject, fact, citations, reason) | Agents call a store-memory tool; no offline curation | Most recent repository memories in the prompt | Repository-level facts, user-level preferences; code citations | Citations re-checked on use; 28 days unused then deleted |
| Letta MemFS [letta-docs-memfs] | Git-backed markdown with `name` and `description` frontmatter | Reflection and defragmentation subagents in git worktrees | Files at the memory root pinned every turn | Per agent; git history | Defragmentation merges and splits; every edit is a commit |
| Mem0 [mem0-readme] | Vector store with entity links; managed service | Single-pass ADD-only extraction since April 2026 | Retrieval per query | User, session and agent levels | Accumulates; temporal ranking at read time |
| Zep and Graphiti [graphiti] | Temporal knowledge graph in a graph database | LLM extraction, edge invalidation on contradiction | Hybrid retrieval | Every fact traces to source episodes | Old facts invalidated, not deleted |
| Basic Memory [basic-memory-format] | Markdown with observations and wiki-link relations | Written by the agent over MCP | On demand via MCP; Claude Code plugin briefs at start | Per project | User-managed |
| claude-mem [claude-mem] | SQLite plus Chroma, hook-captured observations | Hooks capture tool use; summaries generated | Recent observations at session start | Per project; observation ids citable | Not documented |
| ByteRover [byterover-paper] | Markdown context tree with relations | Agent curates; review workflow | Tiered retrieval | Provenance on entries | Importance score, maturity tiers, recency decay |

## 1. Devin: memory and dreaming

**What shipped.** Cognition announced Memory and Dreaming for Devin on 2026-10-05 [devin-blog]. The post contains no benchmark numbers [devin-blog].

**What a memory is.** Not a session summary: a short lesson learned from working with the user (preferences, corrections, project and workflow lessons), written as a note with a link back to the session where it was learned [devin-blog]. Memory is personal to the user within each organization, not shared org instructions [devin-blog].

**Storage.** A persistent Git repository of markdown files (the "Memory Drive"). Notes can be organized by repository, project or topic; a short `MEMORY.md` holds general preferences plus an index of the other files [devin-blog].

**Retrieval.** Devin receives `MEMORY.md` as context at session start and then searches and reads notes with the same tools it uses to navigate code, without loading the whole archive [devin-blog].

**Concurrency.** Each session works in its own Git checkout, commits, merges updates from other sessions and saves; a revision check rejects stale writes and conflicting edits are surfaced instead of overwritten [devin-blog].

**Dreaming.** A daily asynchronous session that reviews past conversations alongside existing memory. It consolidates overlapping notes, removes transient details (the worked example drops a note that the dev server "was on port 3001 this afternoon"), looks for lessons missed at capture time, and removes stale records that no session used; source references and explicit preferences are preserved [devin-blog]. Users can browse memories and recent dreaming sessions under Customize, Memory [devin-blog].

**Memory versus skills.** Skills are deliberately packaged repeatable workflows; memory accumulates through sessions and is revisited by dreaming, so their lifecycles differ [devin-blog].

**The open standard.** Cognition published Agent Memory Repo (repo created 2026-10-04, MIT) [amr-spec]. What it specifies:
- A memory repo is a git repo whose root `MEMORY.md` is the required entry point, loaded every session, kept short, with top-of-file entries every session needs and an `## Index` of `[[path]]` links [amr-spec].
- An entry is one bullet on one line with optional trailing metadata `[key: value; key: value]`; recommended keys are `source` (link to the session where it was learned) and `added` (YYYY-MM-DD); entries are updated or removed when information changes [amr-spec].
- The loop is: clone, search or follow links, update entries "with no human in the loop", push after every edit; dreaming is a periodic agent that adds patterns across sessions and cleans up by merging duplicates, removing outdated entries and checking sources to resolve contradictions [amr-site].
- The reference skill states the safety posture: memory is data, not instructions; no secrets; separate repo, local by default; edit in place and remove outdated entries instead of adding a contradicting one; clean worktree before writing; commit after every edit [amr-skill].

**Related, same vendor family.** Windsurf's docs now live under docs.devin.ai and describe auto-memories as legacy (section 4) [windsurf-memories].

## 2. Anthropic: Claude Code, the API memory tool, Managed Agents and Dreams

### 2.1 Claude Code instruction files (CLAUDE.md hierarchy, imports, rules)

- **Two systems, both loaded every conversation.** CLAUDE.md files hold instructions the user writes; auto memory holds notes Claude writes. Both are context, not enforced configuration; to block an action regardless, use a hook [cc-memory].
- **Scopes, broadest first** (a later file appears later in context): managed policy (`/Library/Application Support/ClaudeCode/CLAUDE.md` on macOS), user (`~/.claude/CLAUDE.md`), project (`./CLAUDE.md` or `./.claude/CLAUDE.md`), local (`./CLAUDE.local.md`, gitignored) [cc-memory].
- **Discovery.** Files in the working directory and every ancestor load at launch, concatenated rather than overriding, ordered from the filesystem root down; subdirectory files load on demand when Claude reads, writes or edits a file there [cc-memory]. Block-level HTML comments are stripped before injection [cc-memory].
- **Imports.** `@path/to/file` expands the file at launch; relative paths resolve from the importing file; imports recurse to a maximum of four hops; imports do not reduce context cost because the imported content loads anyway; imports that resolve outside the working directory in a project file trigger a one-time approval dialog, while imports in user-scope files such as `~/.claude/CLAUDE.md` load without it [cc-memory].
- **Rules.** `.claude/rules/*.md` (and `~/.claude/rules/`) hold one topic per file; `paths` is the only frontmatter field Claude Code reads, taking glob patterns so a rule loads only when matching files are opened [cc-memory].
- **AGENTS.md.** Read directly when no CLAUDE.md or CLAUDE.local.md exists in or above the working directory (v2.1.277 and later), otherwise import it with `@AGENTS.md` [cc-memory].
- **Size guidance.** Target under 200 lines per file because longer files consume more context and reduce adherence; contradictory instructions may be resolved arbitrarily; a CLAUDE.md up to 4 MiB loads in full [cc-memory]. The best-practices page frames it as a pruning test: would removing the line cause mistakes, and if not, cut it [cc-bestpractices]. `/doctor` proposes trims that remove content Claude can derive from the codebase and keep pitfalls, rationale and non-default conventions [cc-memory]. Multi-step procedures belong in skills, not CLAUDE.md [cc-memory].
- **Compaction.** Project-root CLAUDE.md is re-read from disk after `/compact`; instructions given only in conversation are lost [cc-memory].

### 2.2 Claude Code auto memory (index plus topic files)

- **Introduced** in v2.1.59 ("Claude automatically saves useful context to auto-memory") and made shared across git worktrees of one repository in v2.1.63 [cc-changelog].
- **Storage.** `~/.claude/projects/<project>/memory/`, where `<project>` derives from the git repository so worktrees and subdirectories share one directory; outside a repo the project root is used; machine-local; `autoMemoryDirectory` relocates it [cc-memory]. The sweep that deletes transcripts after `cleanupPeriodDays` (default 30) skips this directory [cc-directory].
- **Layout.** `MEMORY.md` is an index with one line per memory, loaded every session; each memory is a topic file; topic files are not loaded at startup and are read on demand with ordinary file tools [cc-memory].
- **Budget.** Only the first 200 lines or 25 KB of `MEMORY.md`, whichever comes first, load at session start. After a write, Claude Code measures the file; near the limit it reminds the agent to keep one line per entry, move detail to topic files and merge or drop stale entries; over the limit the write succeeds but an error tells the agent to rewrite the index, because everything past the limit is dropped on the next load [cc-memory]. The 25 KB cap was added in v2.1.83 and the over-limit error in v2.1.210 [cc-changelog].
- **What gets saved.** Four kinds, recorded as a `type` field in frontmatter: `user` (role, expertise, working preferences), `feedback` (corrections and confirmed approaches), `project` (ongoing work, deadlines, decisions not derivable from code or git history), `reference` (pointers to external information such as trackers and dashboards). Claude skips what it can derive from the codebase and what CLAUDE.md already says, and does not save every session [cc-memory].
- **Timestamps.** When a written memory file has YAML frontmatter, Claude Code records the write time in a `modified` ISO 8601 field (v2.1.214 and later) [cc-memory].
- **Observed** (key names only): `name`, `description`, and a `metadata` map with `node_type`, `type`, `originSessionId`, `modified`. Only `type` and `modified` are documented upstream. Codex's importer also refers to `metadata.originSessionId` [codex-import-src].
- **Audit and control.** Files are plain markdown that can be edited or deleted; `/memory` browses them and toggles the feature; `autoMemoryEnabled` or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` turns it off [cc-memory].
- **Hardening.** v2.1.284 neutralizes invisible characters and tags that imitate Claude Code's own markup in `MEMORY.md` and recalled notes before they reach the model; v2.1.285 stops auto memory being switched on from background sessions or sessions started by Claude Code's own tools [cc-changelog].
- **Subagent memory.** A subagent's `memory` field (`user`, `project` or `local`) gives it its own persistent directory whose first 200 lines or 25 KB of `MEMORY.md` load into its system prompt; the main conversation's auto memory is not passed to subagents [cc-subagents], [cc-memory].
- **No consolidation is documented.** The documented mechanism is size-triggered self-compaction by the agent [cc-memory]; I found no documented scheduled consolidation in Claude Code (see 2.5).

### 2.3 The API memory tool (`memory_20250818`)

- **Shape.** Declared as `{"type": "memory_20250818", "name": "memory"}`; client-side, so Claude requests file operations under a `/memories` prefix and the application executes them against storage it controls. Commands: `view` (directories listed two levels deep; files with line numbers and optional `view_range`), `create`, `str_replace`, `insert`, `delete`, `rename`. Available on Claude 4 and later models [api-memory-tool]. Launched in beta on 2025-09-29 [api-release-notes].
- **Injected protocol.** When the tool is present the API adds a system-prompt protocol: always view the memory directory first, record progress as you go, and assume interruption because context may reset at any moment [api-memory-tool].
- **Guidance for the developer.** Tell the model to keep memory up to date, coherent and organized, rename or delete irrelevant files and avoid creating files unnecessarily; restrict what is written to a topic if wanted [api-memory-tool].
- **Security section.** Validate every path (the docs show `/memories/../../secrets.env` as the attack); strip sensitive data before writes because the model only "usually" refuses to write it; cap file sizes and page long views; periodically delete files not accessed in a long time [api-memory-tool].
- **Multi-session pattern for software projects.** An initializer session creates a progress log, a feature checklist and a pointer to a startup script; later sessions open by reading them and end by updating the progress log; mark a feature complete only after end-to-end verification [api-memory-tool]. Anthropic's harness write-up adds that it chose JSON for the feature list because the model is less likely to overwrite JSON than Markdown, and names premature victory declarations and broken handoffs as failure modes [blog-harnesses].
- **Reported effect.** Anthropic reports that memory plus context editing improved an internal agentic-search evaluation by 39% over baseline and context editing alone by 29%; separately, in a 100-turn web-search evaluation context editing cut token consumption by 84% (vendor-reported, no methodology given) [blog-context-mgmt]. The general stance is just-in-time retrieval and progressive disclosure, treating context as a finite resource with diminishing returns [blog-context-eng].

### 2.4 Managed Agents memory stores

- **Model.** A store is a workspace-scoped collection of text documents mounted into the session sandbox as a directory under `/mnt/memory/`; agents read and write it with the normal file tools, and a note describing each mount is added to the system prompt. Public beta from 2026-04-23 [api-ma-memory], [api-release-notes].
- **Audit.** Every change creates an immutable version; versions are kept 30 days (recent versions of a live memory are always kept); a redact call scrubs content from history; updates accept a `content_sha256` precondition for optimistic concurrency [api-ma-memory].
- **Limits.** 100 kB (about 25k tokens) per memory, 10,000 memories per store, 8 stores per session; the docs say to prefer many small focused files [api-ma-memory].
- **Trust boundary.** Stores attach `read_write` by default. If the agent handles untrusted input, "a successful prompt injection could write malicious content into the store" and later sessions read it as trusted memory; use `read_only` for reference material [api-ma-memory].
- **Hygiene.** Prune before the store fills, or run a dream to produce a consolidated store and switch sessions to it [api-ma-memory].

### 2.5 Dreams (Managed Agents)

- **Status.** Research preview announced 2026-05-06 behind the `dreaming-2026-04-21` beta header [api-release-notes], [api-dreams]. Anthropic's post describes dreaming as a scheduled process that reviews sessions and memory stores, extracts patterns, and curates memories; it can update memory automatically or let you review changes before they land [blog-managed-agents].
- **Mechanics.** A dream is an asynchronous job whose inputs are an existing memory store and 1 to 100 past sessions; its output is a new, reorganized store: duplicates merged, stale or contradicted entries replaced with the latest value, new insights surfaced. The input store is never modified, so output can be reviewed and discarded [api-dreams].
- **Steering.** An optional `instructions` field (up to 4,096 characters) steers focus; the pipeline is a synthesis pass, not a text editor, so line-level edit commands generally produce no change [api-dreams].
- **Cost model.** Runs take minutes to a few hours; billed at standard token rates; cost scales roughly linearly with the number and length of input sessions; the docs advise starting with a small batch [api-dreams].
- **Observability.** While running, the dream's underlying session can be streamed, and it is archived afterwards so the transcript remains [api-dreams].
- **Reported effect.** Harvey's completion rates rose about 6x in its own tests with dreaming (customer-reported, no methodology) [blog-managed-agents].
- **Claude Code "auto dream".** Third-party posts describe an Auto Dream feature and a `/dream` command in Claude Code, including a trigger of 24 hours plus 5 sessions [3p-autodream-1], [3p-autodream-2]. I could not confirm it in first-party sources (section 0), so nothing in the recommendations depends on it.

### 2.6 claude.ai consumer memory

Memory began rolling out to Team and Enterprise on 2025-09-11 and to Pro and Max on 2025-10-23 (per the post and its update); Claude keeps a separate memory per project, shows an editable memory summary, offers incognito chats that are not saved, focuses on work context, and was safety-tested for reinforcing harmful patterns and over-accommodation [blog-claude-memory].

### 2.7 What users report (issue tracker, user-filed)

- **Silent partial loads.** A session cannot tell whether the index loaded whole, truncated or not at all, and a fact can be written to a different project's store and become unreachable; the reporter's index was 161 lines against the 200-line limit indexing 131 topic files [cc-issue-82056].
- **Size-target pressure.** The built-in reminder asks the agent to compact to about 17.1 KB (roughly 70% of the cap); a user found compaction passes then deleted load-bearing content to hit the number and settled on about 22 KB [cc-issue-91188].
- **Fixed overhead.** One user measured a memory preamble of 11.3k to 16.2k tokens in `/context` on v2.1.149 even with auto memory disabled and an empty directory [cc-issue-63903]. This is a single user measurement of a past version.

## 3. OpenAI Codex memories

### 3.1 The `[memories]` configuration surface

Codex memories are off by default and enabled with `[features] memories = true` or the desktop Settings, Personalization toggle; per-chat `/memories` controls whether a chat can use or contribute to memory [codex-memories-doc]. The reference page and the source disagree on two defaults and the reference omits the version keys:

| Key | Reference docs [codex-config-ref] | Source at `822e58c` [codex-config-src] |
| --- | --- | --- |
| `generate_memories` | default true; false stops new threads becoming inputs | true |
| `use_memories` | default true; false skips injecting memories | true |
| `disable_on_external_context` (alias `no_memories_if_mcp_or_web_search`) | default false; threads that used MCP, web search or tool search are kept out of generation | false |
| `max_raw_memories_for_consolidation` | 256, cap 4096 | 256, clamp 1 to 4096 |
| `max_unused_days` | 30, clamp 0 to 365 | 30, clamp 0 to 365 |
| `max_rollout_age_days` | **30**, clamp 0 to 90 | **10**, clamp 0 to 90 |
| `max_rollouts_per_startup` | **16**, cap 128 | **2**, clamp 1 to 128 |
| `min_rollout_idle_hours` | 6, clamp 1 to 48 | 6, clamp 1 to 48 (code comment: more than 12 recommended) |
| `min_rate_limit_remaining_percent` | 25, clamp 0 to 100 | 25, clamp 0 to 100 |
| `extract_model`, `consolidation_model` | optional overrides | unset; a provider-preferred model is chosen in code |
| `version`, `dual_write`, `dedicated_tools` | not in the reference table | `v1` default, `v2` opt-in; v1 and v2 can run side by side; dedicated memory tools off |

A commit message states that startup defaults were reduced to at most 2 rollouts over 10 days [codex-commit-19970], so the public reference appears to lag the source (inference).

### 3.2 Pipeline

- **Trigger.** The README says the pipeline starts when a root session starts and only if the session is not ephemeral, memory is enabled, it is not a sub-agent session and the state DB is available [codex-readme]. A later commit moved the trigger from thread lifecycle events to the first non-empty user turn and added a six-hour success cooldown for global consolidation, "to prevent ghost cost due to pre-warm" [codex-commit-19970].
- **Phase 1, per thread.** Claims a bounded set of eligible rollouts (interactive sources, inside the age window, idle long enough, not owned by another worker), filters each to memory-relevant items, runs the extraction model in parallel (8 concurrent jobs, low reasoning effort, input truncated to 70% of the model's context window with a 150,000-token fallback), and stores structured output: a detailed `raw_memory`, a compact `rollout_summary` and a `rollout_slug`; secrets are redacted before upload and in outputs; jobs carry one-hour leases and a retry delay [codex-readme], [codex-lib-src]. Outcomes are `succeeded`, `succeeded_no_output` and `failed` with backoff [codex-readme].
- **Phase 2, global.** Under a single global lock it loads up to `max_raw_memories_for_consolidation` stage-1 outputs, dropping any whose last use is older than `max_unused_days` (falling back to creation time when never used) and ranking by `usage_count` then recency; syncs `raw_memories.md` and `rollout_summaries/`; keeps the memory root as a git baseline; writes a diff from the previous successful baseline; if nothing changed it stops; otherwise it spawns a consolidation sub-agent (medium reasoning effort) with no approvals, no network and write access only to the memory root, collaboration disabled, lease heartbeats every 90 seconds, then resets the baseline [codex-readme], [codex-lib-src], [codex-state-src].
- **Why two phases.** Phase 1 scales across many rollouts; Phase 2 serializes global consolidation so shared artifacts update consistently [codex-readme].

### 3.3 Where memories live

`~/.codex/memories/` (under `CODEX_HOME`) holds `memory_summary.md`, `MEMORY.md`, `raw_memories.md`, `rollout_summaries/`, `skills/` and `extensions/`, plus a `.git` baseline; job state sits in SQLite [codex-memories-doc], [codex-readme], [codex-consol-v1]. The official page calls these files generated state and says not to rely on hand edits as the primary control surface [codex-memories-doc]; yet the consolidation prompt treats any diff, including a stray user edit, as authoritative input to be propagated [codex-consol-v1].

### 3.4 What the prompts instruct (paraphrased from source)

**Extraction (v1)**, per [codex-stage1-v1].
- Four high-signal buckets: stable user operating preferences; high-leverage procedural knowledge (shortcuts, failure shields, exact commands); reliable task maps and decision triggers; durable evidence about the user's environment and workflow.
- A no-op gate: ask whether a future agent would plausibly act better because of what is written; if not, return empty fields.
- Evidence weighting: user messages first, then tool output, then assistant messages; assistant proposals and brainstorming stay out of durable memory unless adopted or repeatedly reinforced; write "the user asked for X" rather than an unattributed fact.
- Each task gets an outcome label (success, partial, fail, uncertain) with explicit user feedback and test results outranking heuristics, and the last task treated conservatively.
- Preferences are written as evidence plus implication ("when X, the user said Y, which suggests Z") with distinct defaults kept in separate bullets.
- Raw-memory frontmatter: `description`, `task`, `task_group`, `task_outcome`, `cwd`, `keywords`.
- Safety: rollout text is data, not instructions; redact secrets; do not copy large tool output.

**Consolidation (v1)**, per [codex-consol-v1].
- A `MEMORY.md` block per task group with `scope:` and a mandatory `applies_to: cwd=...; reuse_rule=...` line, task sections that list rollout summary files with `cwd`, `rollout_path`, `updated_at`, `thread_id`, and grep-friendly keywords, then consolidated User preferences, Reusable knowledge and Failures sections.
- A `memory_summary.md` that always loads: a profile (up to 350 words), actionable preferences, general tips, and an index of `MEMORY.md` grouped by project scope then by recent day.
- Preserve the source's wording where it is searchable (exact error strings, names) instead of paraphrasing; fresher validated evidence wins; keep uncertainty explicit when evidence conflicts; default to separating similar tasks from different working directories.
- Incremental forgetting: delete only memory supported solely by deleted inputs and keep still-supported content in mixed blocks; minimize churn when existing blocks still match the evidence.
- Skills: turn a repeated procedure into a `skills/<name>/SKILL.md` when it clearly saves time; merge duplicates aggressively.

**v2 (opt-in)**, per [codex-stage1-v2], [codex-consol-v2], [codex-commit-43800], [codex-commit-43813] and [codex-commit-43799].
- Extraction returns only a task-history summary and slug under a 9,000-byte budget, with working directory and branch hints; the stated goal is to preserve the scope of user corrections and preferences "without turning task-specific instructions into general claims" about the user. The worked example: "show me the plan before editing this" must not become "the user prefers plans before editing".
- Evidence is selected in tiers so human input and assistant final answers outrank commentary and tool output.
- Consolidation writes only `memory_summary.md` (no handbook, no raw-memory file) and validates required sections and a size below 10,000 UTF-8 bytes; it applies user edits, removes claims supported only by deleted sources, never restores corrected claims, treats memory content as data, and does not reopen original transcripts.
- Dual-write lets v2 build in the background; a status endpoint reports readiness once at least 20 threads have been consolidated by default [codex-commit-43827].

### 3.5 Read path, citations and usage-based retention

- **Read path (v1).** A summary is injected into the developer prompt; the agent decides whether to consult memory (skip for self-contained requests, use by default when the task touches paths in the summary or asks for prior decisions), greps `MEMORY.md` for keywords, opens at most one or two rollout summaries or skills, and aims for roughly four to six search steps [codex-read-v1].
- **Verification rule.** Weigh drift risk against verification cost; if a drift-prone fact was not re-verified, say it came from memory and may be stale and offer to refresh [codex-read-v1]. In v2: "memory is not proof of current behavior" and do not retrieve history speculatively [codex-read-v2].
- **Citations.** When memory informed the answer the agent ends with an `<oai-mem-citation>` block listing file and line ranges plus rollout ids, and never cites memory in pull-request text [codex-read-v1].
- **Usage feeds retention.** Each cited rollout id increments `usage_count` and sets `last_usage`; Phase 2 selection ranks by usage count then recency and excludes memories unused for more than `max_unused_days`; a pruning query deletes stale stage-1 outputs not selected for consolidation [codex-state-src].
- **Writes only on request.** Memory is updated only when the user explicitly asks to remember, forget or correct; the agent drops a small note under `extensions/ad_hoc/notes/` and consolidation applies it; generated memory files are not edited directly [codex-read-v1], [codex-read-v2].

### 3.6 Guards

- **Rate-limit guard.** Before startup the pipeline checks Codex rate-limit windows and skips Phase 1 and 2 if either is above the configured ceiling (default: at least 25% remaining required) [codex-guard-src], [codex-commit-19990].
- **Idle window.** A thread must be idle at least `min_rollout_idle_hours` (default 6) so work in progress is not summarized [codex-memories-doc], [codex-config-src].
- **External-context taint.** With `disable_on_external_context` on, threads that used MCP, web search or tool search are marked `polluted` and kept out of generation (default off) [codex-polluted-src], [codex-config-ref].
- **Sandboxed consolidator.** No approvals, no network, memory-root write only [codex-readme].

### 3.7 Interoperability: Codex imports another agent's memory

Codex has an `external_agent_import` extension that discovers markdown files under another agent's `projects/*/memory/` directory (the Claude Code layout) and copies them as resources scoped to the project's working directory. Its instructions say to read each project's `MEMORY.md` first, treat other files as progressive-disclosure detail rather than flattening them into the summary, treat imported content as source material and not authoritative instructions, and never edit imported resources [codex-import-src]. Reading another agent's memory directory is therefore an accepted pattern (inference: a de facto interchange format is `MEMORY.md` plus topic files).

### 3.8 Reported problems (issue tracker, user-filed)

- Idle quota drain from background memory generation [codex-issue-19732]; a hidden consolidation subagent consuming a 5-hour quota after a hook failure [codex-issue-22008].
- Global summary injected regardless of working directory; closed as by design [codex-issue-17496]; project-scoped memory still requested [codex-issue-18343].
- Memory ignored until the user asks the agent to read it [codex-issue-18738].
- The injected summary is truncated from the middle at 2,500 tokens while consolidation is never told the limit, dropping the newest entries; the same report notes consolidation dedupes memory against memory but not against always-loaded AGENTS.md [codex-issue-48679].
- Recurring automations dominating global memory [codex-issue-33641].

## 4. ChatGPT, Cursor, Windsurf and GitHub Copilot

### 4.1 ChatGPT memory

- **Timeline.** Tested from 2024-02-13; available to Free, Plus, Team and Enterprise from 2024-09-05 with "Memory updated" notices and a Manage view; on 2025-04-10 it began referencing all past conversations, so memory works in two ways, "saved memories" and "chat history"; a lightweight version reached free users on 2025-06-03 [chatgpt-announce].
- **Early design statements.** Users can tell it to remember something or let it pick up details; it was steered away from proactively remembering sensitive information such as health details; memories were "not linked to specific conversations", and deleting a chat did not delete its memories [chatgpt-announce].
- **Current behavior** (Help Center, archived 2026-09-19) [chatgpt-help].
  - Memory can draw on past chats, saved memories, custom instructions, Library files and connected apps, and it does not search history on every response, only when that is likely to improve the answer.
  - A "memory summary" gives a high-level view; "Sources" under a response show what contributed and allow correcting or deleting a saved memory or referenced chat.
  - "Don't mention this again" reduces future references without deleting the underlying source; deleting a chat alone does not necessarily delete a separate saved memory; logs of deleted saved memories may be kept for up to 30 days.
  - "Improved memory" continually updates a broader summary of relevant context, while legacy saved memories are an explicit list of items; saved-memory controls can include search, sorting, automatic prioritization and version history.
  - Project-only memory keeps references within a project; temporary chats neither create nor update memory.
- **Take-away for design.** The consumer product pairs automatic capture with visible provenance ("Sources"), user correction, and a "stop referencing this" control that is distinct from deletion (inference from the controls described) [chatgpt-help].

### 4.2 Cursor Memories (introduced, then removed)

- **Introduced.** A staff reply on 2025-05-30 said Memories were a feature coming in 0.51 and required privacy mode off, because knowledge about a codebase can end up in memories and they wanted no vector by which sensitive-codebase information could reach LLM-provider training data [cursor-forum-051]. Cursor 1.0 (2025-06-04) shipped "a first look at memories": Cursor remembers facts from conversations, stored per project at the individual level, managed in Settings, rolled out as a beta enabled from Settings, Rules [cursor-1-0].
- **Removed.** On 2025-11-24 and 2025-11-25 a staff member said the feature "was intentionally removed starting from version 2.1.x" (and from 2.1.17 specifically) and told users to run "Export memories" from the Command Palette, which writes an `.mdc` file to import into Rules; a later user reported the export option missing in 2.1.34 [cursor-forum-notshowing], [cursor-forum-gone].
- **Why.** No rationale appears in the staff replies I found. A user wrote that memories were "almost no different than .mdc files" [cursor-forum-gone]. Cursor's rules docs now describe four rule modes (always, intelligent, file-scoped, manual), `AGENTS.md`, user rules, and a recommendation to keep rules under 500 lines, with no mention of Memories [cursor-rules]. The simplest reading is consolidation onto explicit, version-controlled rules (inference).

### 4.3 Windsurf memories

- **Mechanism.** Cascade auto-generates Memories during conversations and retrieves them "when it believes they're relevant"; they are stored locally in `~/.codeium/windsurf/memories/`, scoped to the workspace where they were created, and not committed to the repository; creating and using them does not consume credits [windsurf-memories].
- **Rules instead.** User-defined Rules (global file limited to 6,000 characters, workspace rule files to 12,000 characters each, plus `AGENTS.md`) are version-controlled and shareable; the docs recommend writing knowledge you want reused reliably as a Rule or in `AGENTS.md` rather than relying on auto-generated Memories [windsurf-memories].
- **Retreat.** Memories apply only to the legacy Cascade agent; the newer Devin Local agent, now the default for new tabs, does not persist memories, and users are told to migrate the ones they rely on to skills [windsurf-memories].

### 4.4 GitHub Copilot agentic memory

- **Shape.** In public preview (engineering write-up dated 2026-01-15, opt-in at launch). A memory is `subject`, `fact`, `citations` (code locations such as `path:line`) and `reason`, created through a tool call when an agent finds something with future implications [copilot-blog].
- **Just-in-time verification instead of offline curation.** GitHub considered a service to deduplicate, resolve conflicts, track branch status and expire facts, judged the engineering complexity and LLM cost too high at its scale, and chose to verify at read time on the argument that retrieval is hard but checking is easy. Before using a memory the agent checks the cited locations against the current branch; if the code contradicts it or citations are invalid the agent is encouraged to store a corrected version; if it checks out the agent re-stores it to refresh its timestamp [copilot-blog].
- **Retrieval.** At session start the most recent memories for the repository go into the prompt; search tools and weighted prioritization are listed as future work [copilot-blog].
- **Scope and permissions.** Repository-level facts can be created only from actions by contributors with write permission and used only in that repository by users with read permission; separate user-level preferences capture individual coding style, carry citations that may include direct user quotes, and are validated by the agent's judgment [copilot-blog], [copilot-docs]. Memory is per user, on by default for individual plans, and needs an admin policy for organization plans [copilot-docs].
- **Lifecycle.** Any stored fact or preference that goes unused is deleted after 28 days; the timer may reset when Copilot validates and uses an entry; facts from pull requests closed without merging are kept but only take effect if the current codebase still substantiates them [copilot-docs].
- **Evidence (vendor-reported).** Adversarial memories with wrong citations were seeded and agents "consistently verified citations, discovered contradictions, and updated incorrect memories". With a deliberately noisy pool (over-representing abandoned branches) code review gained 3% precision and 4% recall; in A/B tests the coding agent's pull-request merge rate was 90% with memory against 83% without, and code-review positive feedback 77% against 75% (p < 0.00001 for both) [copilot-blog].
- **Used by** the cloud agent, code review, CLI and agentic autofix; repository owners can review and delete repository facts [copilot-docs].

## 5. Letta and MemGPT

- **MemGPT (2023).** Treats the context window as fast memory and external storage as slow memory, with the model moving data between tiers and using interrupts for control flow (virtual context management) [memgpt].
- **Blocks versus archival.** *Memory blocks* are labelled sections of the context window (label, description, value, character limit) that are always visible, shareable across agents and can be read-only; setting a block's value replaces it entirely and concurrent writers get last-write-wins [letta-docs-blocks]. *Archival memory* is a semantically searchable store that cannot be pinned to context, written and queried through `archival_memory_insert` and `archival_memory_search`, suited to document repositories and logs, not to information needing constant visibility [letta-docs-archival]. *Recall memory* is conversation history on disk [letta-docs-memgpt].
- **Sleep-time compute.** The paper has models "think" offline about a context before queries arrive. On two stateful reasoning tasks it cut test-time compute for equal accuracy by about 5x, and scaling sleep-time compute added up to 13% (Stateful GSM-Symbolic) and 18% (Stateful AIME) accuracy; amortizing across related queries cut average cost per query 2.5x; benefit correlated with how predictable the user's query was; it includes an agentic SWE case study [sleeptime-paper]. In Letta, a primary agent without memory-editing ability is paired with a sleep-time agent that edits the shared memory blocks asynchronously; more frequent runs cost more tokens but give more time to revise learned context [letta-sleeptime-blog].
- **Current design: MemFS (git-backed markdown).** Letta's docs now present MemFS as its latest memory system, git-tracked and built around agent dreaming; their SDK comparison shows the older V1 SDK as memory blocks only and the new Agent SDK with MemFS, and recommends new developers build on the Agent SDK [letta-docs-memgpt]. Each memory except an index is markdown with `name` and `description` frontmatter; files at the memory root are loaded into the system prompt every turn; directories with their own `MEMORY.md` stay out of context until needed, with names acting as signposts; there is no semantic or vector index by default, and agents use ordinary file search and read tools, with an optional search mod (keyword by default, semantic or hybrid via the QMD tool); every edit is a git commit; dreaming and "memory doctor" subagents work in git worktrees [letta-docs-memfs].
- **Coding-agent features** (2026-02-12, [letta-ctxrepo-blog]): an `/init` that can learn from existing Claude Code and Codex histories by fanning concurrent subagents across slices of history in separate worktrees and merging results; a background reflection process that persists important information with informative commit messages; a defragmentation skill that backs up the store, then splits large files, merges duplicates and restructures into 15 to 25 focused files. The docs add `/remember` for explicit teaching, `/sleeptime` triggers (after a number of steps or on compaction), an "agent reviews before applying" option that uses a second background conversation without asking the user, and `/doctor` to audit placement, duplication and system-prompt token usage [letta-docs-memory].
- **"Is a filesystem all you need?" (2025-08-12).** Letta placed LoCoMo conversations in files and let a GPT-4o mini agent search them with `grep`, `search_files` (files are auto-embedded, so it is semantic search) and `open`, forced by tool rules to search before answering; it scored 74.0% against Mem0's self-reported 68.5% for its best graph variant [letta-fs-blog]. Caveats Letta states or implies: LoCoMo measures retrieval, not agentic memory; Mem0 did not answer questions about its method; cross-framework comparisons are "apples to oranges"; the agent used minimal prompt tuning [letta-fs-blog]. Note that this is not a grep-only result: the filesystem agent had embedded search (my reading of [letta-fs-blog]).
- **Context-Bench V2 (2026-07-28).** A private Letta benchmark that separates *memory usage* (adherence and retrieval) from *memory generation* (generalization and hygiene) using memory profiles from real agents, including a coding agent, in both messy and cleaned-up states. Failures it names: weaker models log user feedback verbatim instead of rewriting rules, leave stale and contradictory entries behind, and sometimes treat an unexplained standing instruction in memory as a prompt injection and remove it [letta-eval-blog].

## 6. Other systems and papers

### 6.1 Mem0 and OpenMemory MCP

- **Paper (2025-04-28).** Two phases: extraction from the latest message pair using a conversation summary and recent messages (10 in the experiments), then an update phase that retrieves the 10 most similar existing memories by embedding and lets an LLM choose ADD, UPDATE, DELETE or NOOP via a tool call; a graph variant stores entity-relation triplets with conflict detection [mem0-paper-html]. Reported on LoCoMo with GPT-4o-mini: a 26% relative improvement in LLM-as-judge score over OpenAI's memory, 91% lower p95 latency and over 90% lower token cost than full-context [mem0-paper].
- **What the tables show.** Full-context scored highest on the judge metric (about 73%) at about 17 s p95 latency; Mem0 about 67%, the graph variant 68.44%, the best RAG about 61%, so memory traded a few points of accuracy for a large cost saving [mem0-paper-html]. The paper's claim that its methods consistently outperform all existing memory systems across the four question categories is not what its own table shows: Zep scored 76.60 on open-domain questions against 72.93 for Mem0 [mem0-paper-html].
- **Contested.** Zep re-ran the comparison and reported its own LoCoMo score as 75.14% against 65.99% in Mem0's paper, arguing that Mem0 mis-configured Zep (both speakers as users, timestamps appended to text, sequential searches); Zep's post carries a 2026 correction because it first miscalculated its own score [zep-rebuttal].
- **April 2026 redesign.** The README now describes single-pass ADD-only extraction ("no UPDATE/DELETE. Memories accumulate; nothing is overwritten"), entity linking, fused semantic plus BM25 plus entity retrieval and time-aware ranking, and reports LoCoMo 92.5 (from 71.4), LongMemEval 94.4 (from 67.8), BEAM 64.1 at 1M tokens and 48.6 at 10M, at about 7K tokens per query; these are managed-platform numbers with proprietary optimizations (vendor-reported) [mem0-readme]. The current docs describe the add path as additive [mem0-docs-add].
- **OpenMemory MCP.** Launched as a private, local-first MCP server with `add_memories`, `search_memory`, `list_memories` and `delete_all_memories`, a dashboard, nothing sent to the cloud, and an OpenAI key for embeddings [openmemory-archive]. It was removed from the mem0 monorepo on 2026-07-29 and its docs now redirect to the introduction [openmemory-removal]. Mem0's current coding-agent route is a Claude Code plugin that captures evidence locally through lifecycle hooks, extracts memories in a detached background worker and exposes a single `search_memories` tool [mem0-llms].

### 6.2 Zep and Graphiti

- **Architecture** (paper, 2025-01-20): a temporally aware graph with episode, semantic-entity and community tiers; episodes are a non-lossy store from which facts are extracted, and semantic items trace back to sources; a bi-temporal model records when a fact was true and when it was ingested; when a new edge contradicts a related existing edge, an LLM-driven step sets the old edge's invalid time rather than deleting it, prioritizing new information [zep-paper-html].
- **Reported results.** DMR 94.8% (GPT-4 Turbo) and 98.2% (GPT-4o mini) against MemGPT's 93.4%, though the paper itself calls DMR weak (60-message conversations that fit in context; simple full-context does well). LongMemEval accuracy improved from 55.4% to 63.8% (GPT-4o mini) and 60.2% to 71.2% (GPT-4o) over full-context baselines, which the paper reports as 15.2% and 18.5% relative improvements, with about 90% lower latency [zep-paper-html], [zep-paper].
- **Graphiti (open source).** Facts have validity windows and are invalidated, not deleted; every entity and relationship traces to the episodes that produced them; retrieval fuses embeddings, BM25 and graph traversal; it needs a graph database (Neo4j or FalkorDB profiles) and ships an MCP server [graphiti].

### 6.3 Cognee, Basic Memory, claude-mem, Pieces, Supermemory, ByteRover

| System | Architecture | Notable design facts | Evidence status |
| --- | --- | --- | --- |
| Cognee [cognee] | Open-source knowledge graph plus vector store; `remember` and `recall` calls; works locally with small models and no API key | Turns text into entities and relations and code into a symbol graph; "session distillation" curates accepted lessons into permanent memory | Its paper tunes graph-to-LLM hyperparameters on multi-hop QA, not a memory benchmark; gains were consistent but not uniform [cognee-paper] |
| Basic Memory [basic-memory], [basic-memory-format] | Local-first markdown, AGPL-3.0, over MCP; Obsidian reads the same files | Notes have frontmatter (`title`, `type`, `tags`, `permalink`, `created`, `modified`); facts are observations written as `- [category] text #tag`; relations are `- relation_type [[Note]]`; `build_context` walks links; optional semantic search with reranking; a Claude Code plugin adds session-start briefings and pre-compaction checkpoints | No benchmark claims found |
| claude-mem [claude-mem] | Apache-2.0 plugin: hooks (SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd), a local worker, SQLite plus Chroma hybrid search | Captures tool-use observations rather than distilling chats; three-layer MCP workflow `search` (compact index, about 50 to 100 tokens per result) then `timeline` then `get_observations` (about 500 to 1,000 tokens per result); `<private>` tags exclude content; startup context limited to the current harness unless configured | "About 10x token savings" is the project's own claim; most-starred project checked (96.8k stars on 2026-10-06), which measures popularity, not quality |
| Pieces Long-Term Memory [pieces-ltm], [pieces-ltm2], [pieces-mcp] | OS-level capture on the device (code copied, screens viewed, audio) rather than chats | LTM-2 keeps up to nine months with roll-ups every 10 minutes; per-app access control; an MCP server (2025-04-15) exposes the memory to other tools; an agentic layer searches summaries, events and people across multiple turns | Vendor description only |
| Supermemory [supermemory] | Hosted and local memory plus RAG with plugins and an MCP server (`memory`, `recall`, `context` tools) | Extracts facts, maintains static and recent-activity user profiles, handles temporal change, contradictions and forgetting; memory is scoped by "container tags" | README claims first place on LongMemEval, LoCoMo and ConvoMem; unverified, and it conflicts with other vendors' claims |
| ByteRover (repo `campfirein/cipher`, archived 2026-06-25) [byterover], [byterover-paper] | Agent-native markdown "context tree" (Domain, Topic, Subtopic, Entry) with explicit relations and provenance; the agent that reasons also curates | Adaptive lifecycle with importance scoring, maturity tiers and recency decay; five-tier progressive retrieval that answers most queries in under 100 ms without an LLM; no vector DB, graph DB or embedding service; git-like version control and an approve-or-reject review workflow | README claims LoCoMo 96.1% and LongMemEval-S 92.8% on the production codebase; the paper claims state of the art on LoCoMo and competitive LongMemEval results; all self-reported |

### 6.4 A-MEM and Generative Agents

- **A-MEM** (Zettelkasten-style). Each new memory becomes a note with contextual description, keywords and tags; the system finds related historical notes and creates links; new memories can trigger "memory evolution", updating the context and attributes of existing notes; reported to beat baselines on six foundation models [amem].
- **Generative Agents.** A memory stream of natural-language records; retrieval scores are a min-max-normalized combination of recency (exponential decay, factor 0.995 per game hour since last retrieval), importance (an LLM-rated integer assigned at creation) and relevance (embedding similarity); reflections are generated when the summed importance of recent events exceeds 150 (about two or three times a day): the model proposes three salient questions from the 100 most recent records, retrieves evidence and writes higher-level insights that cite their evidence; ablations show observation, planning and reflection each contribute [genagents].

### 6.5 Research on distilled experience (why lessons beat raw trajectories)

| Work | What it stores | Reported result |
| --- | --- | --- |
| ReasoningBank [reasoningbank] | Strategies distilled from self-judged successes and failures | Outperforms memories that store raw trajectories or only successful routines on web and software-engineering benchmarks |
| Agent Workflow Memory [awm] | Reusable workflows induced from experience | 24.6% and 51.1% relative success-rate gains on Mind2Web and WebArena |
| Reflexion [reflexion] | Verbal reflections in an episodic buffer | 91% pass@1 on HumanEval against 80% for GPT-4 |
| ExpeL [expel] | Natural-language insights extracted from experience, plus past experiences | Performance improves as experience accumulates |
| Voyager [voyager] | A growing library of executable skills | 3.3x more unique items and up to 15.3x faster milestones than prior best |

Two surveys frame the terrain: one separates memory by form (token-level, parametric, latent) and function (factual, experiential, working) [survey-hu]; another formalizes a write-manage-read loop, covers applications including coding agents, and names write-path filtering, contradiction handling, latency budgets and privacy governance as engineering realities [survey-du]. CoALA's modular split is short-term working memory plus long-term episodic, semantic and procedural memory [coala].

## 7. Evidence: markdown versus vectors versus graphs, and known failure modes

### 7.1 The benchmarks that exist

| Benchmark | What it measures | Design | What to know |
| --- | --- | --- | --- |
| LoCoMo [locomo] | Question answering, summarization and dialogue over very long conversations | About 300 turns and 9K tokens on average, up to 35 sessions | Short by current context sizes; independently audited, see 7.3 |
| LongMemEval [longmemeval], [longmemeval-repo] | Information extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention | 500 questions; S variant about 115K tokens, M variant about 500 sessions; GPT-4o judge | Commercial assistants and long-context models showed about a 30% accuracy drop on sustained interactions; proposes session decomposition, fact-augmented key expansion and time-aware query expansion |
| MemoryAgentBench [memoryagentbench] | Accurate retrieval, test-time learning, long-range understanding, selective forgetting | Incremental multi-turn format | RAG agents beat their backbone on accurate retrieval; long-context models were best at test-time learning and long-range understanding; multi-hop selective forgetting stayed near the floor (for example 4.0 in one update-policy ablation) |
| STALE [stale] | Noticing that a stored belief is outdated when the contradiction is implicit | 400 scenarios, 1,200 queries, contexts to 150K tokens | Best evaluated model reached 55.2% |
| HorizonBench [horizonbench] | Tracking preferences that change over six months | 4,245 items, 360 simulated users, about 163K tokens | Best of 25 models 52.8%; most at or below the 20% chance level; over a third of errors chose the originally stated value |
| PrefEval [prefeval] | Following stated preferences in long conversations | 3,000 preference-query pairs, up to 100K tokens | Zero-shot accuracy under 10% at 10 turns (about 3K tokens); prompting and retrieval did not fully fix it |
| LoCoMo-Plus [locomo-plus] | Applying implicit constraints that no later question mentions | Cue-trigger semantic disconnect | Shows string-match metrics are misaligned with this setting |
| Agent Retrieval Bench [agent-retrieval-bench] | File-level context retrieval for coding agents | 427 samples, 25 repositories, 392,000 files | No retrieval family dominates; logged agent trajectories missed every gold file on 27 to 35 percent of samples |
| Context-Bench V2 [letta-eval-blog] | Memory usage versus memory generation in stateful agents | Private; memory profiles from real agents | Vendor benchmark, not reproducible |

### 7.2 Evidence on the storage and retrieval substrate

| Evidence | Measured by | Result | Reading |
| --- | --- | --- | --- |
| Letta filesystem agent against Mem0 graph on LoCoMo [letta-fs-blog] | Vendor against a competitor's self-reported figure | 74.0% against 68.5% with GPT-4o mini | An agent that iterates over files with grep and embedded search beat a single-shot pipeline; not a grep-only result |
| Mem0 paper [mem0-paper-html] | Vendor | Judge scores: full-context about 73, Mem0 graph 68.44, Mem0 about 67, best RAG about 61 | At 26K-token conversations memory is a cost and latency optimization, not an accuracy gain over full context |
| Zep paper [zep-paper-html] | Vendor | LongMemEval 15.2% and 18.5% relative gains over full-context baselines; DMR called a weak benchmark by the authors | Temporal structure helps on update and time questions |
| Mem0 2026 algorithm [mem0-readme] | Vendor, managed platform | LoCoMo 92.5, LongMemEval 94.4 with ADD-only extraction and fused semantic, BM25 and entity retrieval | Not independently reproduced |
| LoCoMo audit [locomo-audit] | Independent individual author | Answer-key ceiling 93.57%; a full-context GPT-4.1-mini run with a better answer prompt scored 92.62%, above a claimed 92.32% state of the art; a third party reported 38.38% against that system's claimed 92.32% | Scores above about 90% sit within prompt, judge and key-error effects |
| MemoryAgentBench [memoryagentbench] | Academic | RAG strong at exact retrieval, weak at global understanding and multi-hop forgetting | Substrate matters for lookups; update logic is the hard part |
| Keyword search against RAG [keyword-search] | Industry authors | Agentic keyword search reached over 90% of RAG metrics with no vector database | Document question answering, not memory, but the same retrieval trade-off |
| Claude Code design change [bcherny-x] | Builder statement | Moved from RAG with a local vector database to agentic search, citing simplicity and fewer security, privacy, staleness and reliability problems | A design decision, not a benchmark |
| Copilot A/B test [copilot-blog] | Vendor, production scale | Recency-based retrieval plus citation verification, merge rate 90% against 83% | Simple retrieval plus verification shipped with measured gains |
| Letta MemFS [letta-docs-memfs] | Design decision | No vector index by default; optional keyword, semantic or hybrid search mod | The MemGPT team dropped its vector archive as the default |
| ByteRover [byterover-paper] | Self-reported | Markdown tree with no vector or graph database claims state of the art on LoCoMo | Conflicts with other vendors' state-of-the-art claims |
| TEPA [tepa] | Academic, controlled drift | Append-only and last-write-wins fell below no memory after a full reversal; revocation held 0.950 | Lifecycle logic dominates substrate |

**Reading across the rows (inference).**
- No independent head-to-head of memory architectures for coding agents exists in what I found.
- The measured differences come less from vector, graph or file substrate than from (a) whether the agent iterates on its own queries, (b) how updates and contradictions are handled, (c) answer-prompt and judge effects.
- Temporal graphs are justified by update and time-reasoning tasks, but the minimal mechanism (a validity interval plus a supersession pointer) fits in a markdown note's frontmatter.
- Counter-evidence worth keeping in view: vendors report large gains from hybrid retrieval with entity linking, and RAG is demonstrably better at exact lookups. At personal scale (hundreds to low thousands of short notes) lexical search over descriptions is the cheap baseline, and embeddings are an option to earn with an eval, not a default.

### 7.3 Why headline numbers mislead

- **Key and judge errors.** 99 of 1,540 LoCoMo questions (6.4%) have wrong golden answers; a GPT-4o-mini judge accepted 62.81% of intentionally wrong but topical answers; 446 adversarial questions are unevaluated in published results [locomo-audit].
- **Harness disagreement.** Zep says Mem0 mis-configured Zep when it ran the comparison [zep-rebuttal]; Letta says Mem0 did not clarify how it ran MemGPT [letta-fs-blog].
- **Mutually exclusive leaderboards.** Supermemory, ByteRover, Mem0 and EverMemOS each claim state of the art on LoCoMo or LongMemEval [supermemory], [byterover-paper], [mem0-readme], [evermemos].
- **Baselines.** Full-context with a good prompt matches or beats memory systems at these conversation lengths [locomo-audit], [mem0-paper-html]; real coding-agent histories are far longer, so the comparison that matters (memory against no memory at realistic length) is barely measured (inference).
- **Wrong target.** These tasks score answers to questions about a chat, not whether an agent behaves better after having memory. The results I found that measure the latter are Copilot's A/B test [copilot-blog] and Anthropic's internal agentic-search evaluation [blog-context-mgmt].
- **Mixed evidence on context files.** LLM-written or committed `AGENTS.md` files did not generally improve task success and raised inference cost by over 20%, although instructions in them were followed and non-standard practices were useful [agents-md-study]; a later study found that guidance tuned against synthetic probes raised a small model's SWE-bench Verified resolve rate from 25.5% to 33.0%, and argues that how guidance is produced is the decisive variable [probe-refine].

### 7.4 Failure modes and what systems do about them

**A. Stale and contradictory memories.**
- Models often fail to act on updated evidence even when they retrieve it: best model 55.2% on implicit-conflict scenarios [stale].
- Stale notes override fresh evidence: the smallest to largest Qwen3 models answered with the stale value 0.92 to 1.00 of the time when it was the only source; a stale note dated newer fooled larger models more; exposing metadata helped capable models while only pre-resolving the conflict helped small ones [trustgap].
- Provenance links did not rescue decisions: 16 models inspected a superseded constraint's provenance path in about one episode in five under a two-record budget, and 75% of decisions stayed consistent with the stale constraint [budgeted-verif]. A verifier that checks provenance and chronology still does not establish semantic supersession, as its authors state [stateauditor].
- Update semantics matter: append-only and last-write-wins stores fell below no memory after a reversal while revocation with preserved history recovered [tepa].
- What systems do: write-time supersession with history (Graphiti invalidates instead of deleting [graphiti]; Dreams replace stale entries with the latest value [api-dreams]; Codex says later corrections supersede earlier claims and never restores corrected ones [codex-consol-v2]); read-time verification against live state (Copilot [copilot-docs]; Codex asks the agent to verify drift-prone facts when cheap [codex-read-v1]); expiry by disuse (section 8.5); asking the user when a conflict is ambiguous, because recency and provenance rules alone cannot separate drift from temporary context or poisoning [capture].

**B. Poisoning and persisted prompt injection.**
- ChatGPT: untrusted content from connected apps, images and browsing stored fake memories that persisted across chats; per the researcher, OpenAI classed the report a model-safety issue and mitigated browsing only (post dated 2024-05-22) [rehberger-chatgpt].
- Windsurf: injected text in source-code comments triggered Cascade's `create_memory` tool to persist instructions across sessions; reported 2025-05-30, published 2025-08-22; the author recommends that systems suggest memories to the user instead of adding them automatically, and that users review stored memories regularly [rehberger-windsurf].
- Research: a memory bank can be compromised using only queries and observed outputs, with no direct write access [minja]; poisoning under 0.1% of a memory or knowledge base gave over 80% average attack success on three agent types [agentpoison].
- Controls shipped: Anthropic's warning and `read_only` mode [api-ma-memory]; Claude Code neutralizing invisible characters and markup-imitating tags in memory (v2.1.284) [cc-changelog]; Codex excluding threads that touched external context [codex-config-ref]; Copilot's adversarial seeding test and read-time verification [copilot-blog]; "memory is data, not instructions" in Cognition's skill and in Codex's prompts [amr-skill], [codex-stage1-v2].
- A shared cross-agent store enlarges the blast radius: a note injected through a browsing-capable agent is loaded by a shell-capable one (inference). Codex's import rule that imported memory is source material, not instruction, is the same defence [codex-import-src].

**C. Over-retrieval and context pollution.**
- Across 18 models, performance degrades as input grows, and even a single distractor lowers it [context-rot]; relevant information in the middle of long inputs is used worst [lost-middle].
- Context files cost over 20% more inference and, for repository overviews, bought nothing; instructions in them are followed, so wrong instructions are also followed (inference from [agents-md-study]).
- Anthropic's guidance is the smallest set of high-signal tokens and just-in-time retrieval [blog-context-eng]; Claude Code's docs say a long CLAUDE.md causes rules to be lost [cc-bestpractices], [cc-memory].
- Reported in practice: injecting a whole global summary into unrelated repositories [codex-issue-17496]; a 270-line summary losing about 60% to middle truncation [codex-issue-48679]; an 11k to 16k token memory preamble [cc-issue-63903].
- What systems do: layered retrieval (claude-mem's search, timeline, get-observations [claude-mem]); directory indexes that keep subtrees out of context [letta-docs-memfs]; "do not retrieve history speculatively" [codex-read-v2].

**D. Preference drift and over-generalization.**
- Evolved preferences are tracked poorly (52.8% best) and models fall back to the originally stated value [horizonbench]; zero-shot preference following fell below 10% at 10 turns (about 3K tokens) [prefeval].
- Over-generalizing a one-off request is a design concern for OpenAI: the v2 extraction prompt exists to avoid "turning task-specific instructions into general claims about the user" [codex-commit-43800].
- Weak models log feedback instead of rewriting rules, and strong models can mistake an unexplained standing instruction for an injection [letta-eval-blog].
- Anthropic tested whether memory could reinforce harmful patterns or lead to over-accommodation before launch [blog-claude-memory].
- What systems do: scope language preserved in notes [codex-consol-v2]; user-visible controls to stop referencing something without deleting its source [chatgpt-help].

**E. Scoping bleed.** Global injection across repositories and a request for project scope in Codex [codex-issue-17496], [codex-issue-18343]; a fact written to a different project's store in Claude Code [cc-issue-82056]; Anthropic, ChatGPT, Windsurf and Copilot each scope by project or repository [blog-claude-memory], [chatgpt-help], [windsurf-memories], [copilot-docs].

**F. Cost and silent failure of background jobs.** Idle quota drain and a consolidation subagent that consumed a 5-hour quota [codex-issue-19732], [codex-issue-22008]; Dream cost scaling with sessions [api-dreams]; Claude Code over-limit and truncation reports [cc-issue-82056], [cc-issue-91188]. What systems do: rate-limit guards, cooldowns, idle windows and bounded batches [codex-guard-src], [codex-commit-19970].

**G. Self-ingestion and feedback loops.** Eligible Codex rollouts exclude ephemeral and sub-agent sessions [codex-readme]; Claude Code offers `--no-session-persistence` for print mode [cc-cli] and Codex offers `exec --ephemeral` [codex-exec]; Claude Code refuses to switch auto memory on from background sessions [cc-changelog]; recurring automations can dominate a store when they are not excluded [codex-issue-33641].

## 8. A working design vocabulary: taxonomy, scope, provenance, lifecycle, consolidation

### 8.1 Taxonomies in use

| Source | Categories |
| --- | --- |
| CoALA [coala] | Short-term working; long-term episodic, semantic, procedural |
| Agent-memory survey [survey-hu] | Factual, experiential, working |
| Claude Code auto memory [cc-memory] | `user`, `feedback`, `project`, `reference` |
| Codex extraction [codex-stage1-v1] | Operating preferences; procedural knowledge and failure shields; task maps and decision triggers; environment and workflow evidence |
| Copilot [copilot-docs] | Repository-level facts; user-level preferences |
| Devin [devin-blog] | Preferences, corrections, project and workflow lessons; skills kept separate |
| Windsurf [windsurf-memories] | Auto memories; rules; workflows; skills |
| Cline Memory Bank [cline-memory-bank] | Project brief, product context, active context, system patterns, tech context, progress |
| Basic Memory [basic-memory-format] | Observation categories such as decision, approach, constraint |
| Mem0 [mem0-readme] | User, session and agent levels |
| Anthropic memory tool [api-memory-tool] | Project context, lessons from interactions, decisions and feedback, knowledge base |

### 8.2 A taxonomy that works for coding agents (synthesis)

Five durable types plus one promotion path. This is my synthesis of the rows above (inference); the mapping column shows where each type already exists.

| Type | Holds | Already present as | Load policy | Lifecycle |
| --- | --- | --- | --- | --- |
| `preference` | How this developer works: tools, conventions, workflow, communication, quality bar, plus corrections that recur | Claude `user` and `feedback`; Codex operating preferences; Copilot user-level preferences; Devin preferences and corrections | Always loaded (global, plus per-project overrides). Stated preferences are followed poorly even when in context and decay with conversation length [prefeval], so they should not depend on a discretionary lookup (inference) | No expiry; supersede on a later explicit change; separate permanent from "for this task" |
| `fact` | Stable project knowledge not derivable from the code: non-default commands, environments, integrations, where truth lives | Claude `project` and `reference`; Codex task maps; Copilot repository facts | Top few per project; rest on demand | Verify against the repo when anchored; drop from index after disuse |
| `decision` | A choice and its reason | Devin notes with reasons; Copilot `reason`; Basic Memory `[decision]` | Top few per project | Supersede on reversal and keep the history |
| `lesson` | Symptom, cause, fix: failure shields and gotchas | Codex failure shields; Devin environment gotchas | Mostly on demand; always-loaded only if recurring and costly | Expire by disuse; promote to a skill when a stable procedure emerges |
| `open` | In-progress or episodic state that a later session should resume | Cline `activeContext` and `progress`; Anthropic progress logs | At most a few recent items | Short TTL; recall's chat index already is the episodic store |

- **Not memory.** Anything derivable from the code (Claude skips it; repository overviews did not help [cc-memory], [agents-md-study]), one-off task detail and transient values such as a dev-server port [devin-blog], [codex-stage1-v1], secrets, generic advice, and assistant-only proposals [codex-stage1-v1].
- **Procedures become skills.** Multi-step procedures belong in skills, not CLAUDE.md or notes [cc-memory]; Codex consolidation emits `skills/` for repeated procedures [codex-consol-v1]; Devin separates skills from memory by lifecycle [devin-blog]. The Agent Skills format needs only `name` (up to 64 characters) and `description` (up to 1,024), loads metadata first (about 100 tokens), the body when activated (under 5,000 tokens recommended) and resources on demand [agent-skills-spec].

### 8.3 Scoping

| System | Scopes | Note |
| --- | --- | --- |
| Claude Code [cc-memory], [cc-subagents] | Per git repository (worktrees and subdirectories share); user-level instruction files; subagent memory at user, project or local scope | Facts keyed to the repo, not the checkout |
| Codex [codex-consol-v1], [codex-issue-17496] | Global summary plus working-directory blocks with a `reuse_rule` | Write path is cwd-aware; read path injects the whole global summary |
| Copilot [copilot-docs] | Repository facts (permission-gated) and user preferences (tied to the billing entity) | Validated against the current branch |
| Windsurf [windsurf-memories] | Per workspace; global rules file | Auto-memories not committed |
| claude.ai, ChatGPT [blog-claude-memory], [chatgpt-help] | Per project; account with optional project-only memory | |
| Devin and Agent Memory Repo [devin-blog], [amr-spec] | Personal per organization; several memory repos can load at once, each with its own ownership | |

**Worktrees and clones.** Claude Code shares one memory across worktrees of a repository [cc-memory]; Codex tells its consolidator to keep similar tasks from different working directories apart and to mark checkout-specific guidance [codex-consol-v1]; Copilot sidesteps the question by verifying against the current branch [copilot-docs]. Sharing across clones gives reuse; branch-specific facts then need an explicit qualifier or verification (inference).

### 8.4 Provenance

| System | What a note points to | Checked when |
| --- | --- | --- |
| Devin [devin-blog] | The session where it was learned | Not documented |
| Copilot [copilot-docs] | Code locations (repository facts) and user quotes (preferences) | Every use, against the current branch |
| Codex [codex-read-v1], [codex-consol-v1] | Rollout summary files with thread ids and line ranges | Consolidation deletes memory supported only by deleted inputs |
| Graphiti [graphiti] | The source episodes, in both directions | On query |
| Agent Memory Repo [amr-spec] | A `source` link per entry | Not specified |
| ChatGPT [chatgpt-help] | "Sources" shown with the response | By the user |
| Anthropic memory stores [api-ma-memory] | Immutable versions with attribution | By the operator |

The consuming agent rarely verifies provenance by itself [budgeted-verif]; the checks that work are automated ones: Copilot's per-use validation and Codex's deletion of memory whose evidence was removed (inference from the rows above).

### 8.5 Lifecycle

- **Expiry by disuse.** Copilot: 28 days unused, timer renewed by validated use [copilot-docs]. Codex: 30 days since last cited use by default, tracked through citation counts [codex-config-src], [codex-state-src]. Devin: records no session used are removed [devin-blog]. Anthropic: periodically delete memory files not accessed in a long time [api-memory-tool]. ByteRover: importance scores, maturity tiers and recency decay [byterover-paper]. Generative Agents: exponential recency decay [genagents].
- **Supersession.** Invalidate with history (Graphiti [graphiti]; TEPA revocation [tepa]); replace with the latest value (Dreams [api-dreams]); Mem0 moved from UPDATE and DELETE to ADD-only with time-aware ranking [mem0-paper-html], [mem0-readme].
- **Confidence.** Codex labels task outcomes and keeps uncertainty explicit when evidence conflicts [codex-stage1-v1], [codex-consol-v1]; Copilot's equivalent is validation at use [copilot-docs]; recall's in-progress design already carries a confidence field (see the fit-with-code note under Recommendations).
- **Size-triggered compaction.** Claude Code reminds the agent to compact the index near the cap [cc-memory]; Letta runs defragmentation to 15 to 25 files [letta-ctxrepo-blog]; users report compaction targets deleting load-bearing content [cc-issue-91188].
- **History retention.** Git for Codex, Devin and Letta [codex-readme], [devin-blog], [letta-docs-memfs]; versions kept 30 days for Anthropic stores [api-ma-memory].

### 8.6 Consolidation operations

| Operation | Who does it and how | Risk |
| --- | --- | --- |
| Deduplicate and merge | Devin dreaming and Dreams merge overlapping notes [devin-blog], [api-dreams]; Codex merges duplicates and keeps one searchable phrasing [codex-consol-v1]; Letta defragmentation [letta-ctxrepo-blog]; Mem0 matched new facts to the 10 nearest memories [mem0-paper-html] | Over-merging distinct preferences (Codex says keep separate bullets when they change different defaults [codex-consol-v1]) |
| Resolve contradictions | Newer evidence wins (Graphiti, Codex, Dreams); Agent Memory Repo dreaming checks sources [amr-site]; Copilot checks code [copilot-docs]; CAPTURE asks the user when uncertain [capture] | Implicit conflicts are missed [stale]; temporary context mistaken for change [capture] |
| Generalize and promote | Generative Agents reflection [genagents]; Codex promotes preferences supported across distinct tasks and keeps single-task requests local [codex-consol-v1]; ReasoningBank distills strategies [reasoningbank] | Overgeneralization [codex-commit-43800] |
| Forget | Disuse, unsupported evidence, transient detail (section 8.5) | Deleting load-bearing content to hit a size target [cc-issue-91188] |
| Human review | Dreams can stage output for review [blog-managed-agents]; ByteRover approve-or-reject [byterover]; Letta optional agent review [letta-docs-memory]; Codex treats user edits as authoritative [codex-consol-v1] | Review fatigue (inference) |
| Concurrency | Git worktrees (Letta [letta-docs-memfs]); revision checks (Devin [devin-blog]); content-hash preconditions (Anthropic [api-ma-memory]); global lock (Codex [codex-readme]) | Lost updates |

## 9. How this differs from a curated PKM such as Obsidian

| Dimension | Curated PKM (Obsidian vault) | Automatic agent memory |
| --- | --- | --- |
| Capture | Manual: write, clip, link | From sessions, without the user doing anything |
| Maintenance | The user's job; Karpathy argues people abandon wikis because upkeep grows faster than value [karpathy-wiki] | A machine job (dreaming), so staleness control is a feature requirement |
| Primary reader | The human | A model with a hard token budget |
| Trust | Author-written, trusted | Machine-extracted from content that can contain injected text |
| Structure | User-chosen folders, links, typed properties | Small fixed schema plus an index |
| Format | Markdown files in a vault, YAML properties with a type per property name, wikilinks or Markdown links [obsidian-storage], [obsidian-properties], [obsidian-links] | The same files can serve both if the schema stays simple |

- **Obsidian MCP integrations expose a vault; they do not decide what to remember.** mcp-obsidian offers list, get, search, patch, append and delete tools through the Local REST API plugin [obsidian-mcp-markus]; obsidian-mcp-server has 14 tools including frontmatter and tag management, folder-scoped read and write permissions and optional delete confirmation [obsidian-mcp-cyanheads]; Obsidian also ships a CLI whose commands include create, read, search and append and which requires the app to be running [obsidian-cli]. None capture automatically (inference from their tool lists).
- **The bridge designs.** Basic Memory writes agent-authored markdown with observations and relations that Obsidian opens directly [basic-memory-format]. Karpathy's LLM Wiki gist (2026-04-04) has the LLM maintain a markdown wiki between immutable raw sources and a schema file, with ingest, query and lint operations, a content catalog `index.md` read first (works at roughly 100 sources and hundreds of pages without embeddings), an append-only `log.md` with a parseable prefix, git history, and an optional local search engine at scale [karpathy-wiki].
- **Mapping to recall (inference).** Chat transcripts are the raw sources, notes are the wiki, the consolidation prompt is the schema, the nightly pass is ingest plus lint. Borrowable: an index read first, an append-only dream log, a lint pass for contradictions, stale claims and orphans, and a vault-compatible layout so humans can inspect and edit without special tools.
- **Compatibility details.** Obsidian supports both wikilinks and Markdown links and defaults to wikilinks, with Markdown links for interoperability [obsidian-links]; a property name keeps one type across the vault, so frontmatter types should be stable [obsidian-properties]; vaults refresh on external edits and sync via Git [obsidian-storage].

## Recommendations for recall

Scope: a local, single-user tool with one runtime dependency (the `mcp` SDK), SQLite with FTS5, and three agents to serve (Claude Code, Codex, Cursor). Every recommendation names the evidence it rests on; values marked (judgment) are proposed starting points that no source fixes and that the evals in R6 should tune.

### R1. Storage format: markdown notes in git, SQLite as a disposable index, no vector or graph store in v1

**Decision.** Notes are markdown files with YAML frontmatter in a local git repository (`~/.agents/memory`). They are the system of record. SQLite FTS5 (already used for chats) indexes them and is rebuilt from the files. Raw chats stay in `recall.db` as immutable, redacted evidence. Relations are plain fields (`supersedes`, `related`) and links, not a graph database. Embeddings are an optional later layer.

**Why, in order of weight.**
1. **Claude Code, Codex, Devin and Letta converged on this shape**, and Letta (author of MemGPT) abandoned tool-edited blocks plus a vector archive for git markdown with no vector index by default [cc-memory], [codex-readme], [devin-blog], [letta-docs-memgpt], [letta-docs-memfs].
2. **Retrieval evidence favours cheap, agent-driven search at this scale.** Claude Code's builders dropped RAG with a local vector database for agentic search, citing simplicity and fewer security, privacy, staleness and reliability problems [bcherny-x]; agentic keyword search reached over 90% of RAG metrics [keyword-search]; Copilot shipped recency retrieval plus verification with a 7-point merge-rate gain [copilot-blog]; no retrieval family won on coding-agent context tasks [agent-retrieval-bench].
3. **What the benchmarks reward is lifecycle logic, not substrate.** Update, revocation and supersession drove results [tepa], [stale], [memoryagentbench], and the part of a graph that earns its keep (validity intervals and provenance, as in Graphiti [graphiti]) fits in frontmatter (inference).
4. **Auditability and user control.** Git gives a diff and a revert for every nightly run, as in Codex, Devin and Letta [codex-readme], [devin-blog], [letta-docs-memfs]; users can edit files and edits win [codex-consol-v1]; Anthropic and ByteRover both offer review before changes land [blog-managed-agents], [byterover].
5. **Interoperability.** `MEMORY.md` plus topic files is already read by Codex's importer [codex-import-src], opens in Obsidian [obsidian-storage] and matches Cognition's open spec [amr-spec].
6. **Fit with recall's invariants.** Local-first, with no hosted memory store; a graph database needs a server process [graphiti]; Cursor's early Memories required privacy mode off and drew user complaints [cursor-forum-051].

**Two stores, kept separate.** Evidence (chats, immutable) versus claims (notes, curated). This is the raw-source versus wiki split in Karpathy's pattern, Graphiti's episodes versus facts, and Codex's rollouts versus memories [karpathy-wiki], [graphiti], [codex-readme].

**Revisit when.** Add hybrid BM25 plus embeddings (first for chats, as already on the roadmap) if note-query hit@3 on paraphrased queries falls below 0.85 or a project passes about 1,000 active notes (judgment). The upgrade path has precedent: fused semantic, keyword and entity retrieval [mem0-readme] and an optional QMD-backed search mod [letta-docs-memfs].

### R2. Note schema (frontmatter) and index format

One file per note, `<id>.md`; the body is one to three sentences ending in `Why: ...` when a reason is known. Property types stay fixed per key because Obsidian types a property name across the whole vault [obsidian-properties].

| Field | Type | Required | Purpose | Anchor |
| --- | --- | --- | --- | --- |
| `schema` | int | yes | Format version; regenerate or migrate on mismatch | Codex's version header and schema reset [codex-consol-v1] |
| `id` | string | yes | Stable slug plus short hash; equals the filename | Path-addressed memory in Letta and Cognition's spec [letta-docs-memfs], [amr-spec] |
| `type` | enum | yes | `preference`, `fact`, `decision`, `lesson`, `open` | Section 8.2 |
| `scope` | enum | yes | `global`, `project`, `unfiled` | Section 8.3 |
| `project` | string | if project | Stable key from the git remote (`git:host/owner/repo`), plus a display name | Repo-keyed memory shared across worktrees [cc-memory], [copilot-docs] |
| `title` | string, 80 chars max | yes | Human label | Field already in the in-progress code |
| `description` | string, 160 chars max | yes | The one-line retrieval hook shown in indexes and search results | `description` in Letta memory files, Agent Skills and Claude auto memory files (observed) [letta-docs-memfs], [agent-skills-spec] |
| `status` | enum | yes | `active`, `superseded`, `expired`, `rejected` (rejected is a tombstone) | Codex never restores corrected claims; ChatGPT separates "stop referencing" from delete [codex-consol-v2], [chatgpt-help] |
| `origin` | enum | yes | `user_stated`, `user_corrected`, `inferred`, `assistant_proposed` | Codex keeps user statements, user corrections and assistant proposals apart [codex-stage1-v1] |
| `confidence` | enum | yes | `high`, `medium`, `low`; derived from origin plus corroboration | Already in the in-progress code; Codex preserves uncertainty [codex-consol-v1] |
| `created`, `updated` | date | yes | Lifecycle bookkeeping | Claude Code's `modified` field [cc-memory] |
| `last_seen` | date | yes | Last time evidence re-confirmed the claim | Field already in the in-progress code |
| `last_used`, `use_count` | date, int | yes | Last agent read and number of reads | Codex `last_usage` and `usage_count`; Copilot renews on validated use [codex-state-src], [copilot-docs] |
| `valid_from`, `valid_until` | date | `valid_until` set on supersede or expiry | Validity interval for facts and decisions | Graphiti validity windows; TEPA revocation with history [graphiti], [tepa] |
| `supersedes`, `superseded_by` | id list, id | when relevant | Supersession chain | Fields already in the in-progress code |
| `sources` | list of objects | yes, at most 5 | Provenance: `chat` (recall chat id), `turns` (range), `quote` (160 chars max), `quote_sha`, `speaker`, `date` | Session links, line-range citations and source episodes [devin-blog], [codex-read-v1], [graphiti] |
| `applies_to` | object | optional | Qualifiers: `branch`, `paths` (globs), `os` | Codex `applies_to` and `reuse_rule`; Claude path-scoped rules [codex-consol-v1], [cc-memory] |
| `anchors` | list of objects | optional | Verifiable code anchors: `path` and `pattern` | Copilot citations validated against the current branch [copilot-docs] |
| `related` | id list | optional | Soft links for navigation | Cross-links in Cognition's spec and Basic Memory [amr-spec], [basic-memory-format] |
| `human_edited` | bool | set by lint | File differs from recall's last commit; never auto-rewrite the body | User edits are authoritative in Codex consolidation [codex-consol-v1] |

```yaml
---
schema: 1
id: pkg-manager-bun-3f9a
type: preference
scope: project
project: git:github.com/example/site
title: Use bun, not npm
description: Package manager is bun; the lockfile is bun.lock; do not run npm install.
status: active
origin: user_corrected
confidence: high
created: 2026-09-12
updated: 2026-10-02
last_seen: 2026-10-02
last_used: 2026-10-04
use_count: 6
valid_from: 2026-09-12
sources:
  - {chat: "claude:2d1c0f", turns: [3, 4], quote: "no, bun not npm, the lockfile is bun.lock", quote_sha: 9a1b6c, speaker: user, date: 2026-09-12}
  - {chat: "codex:7e51a2", turns: [1, 1], quote: "use bun for installs", quote_sha: 51cc0e, speaker: user, date: 2026-10-02}
---
Install and run scripts with bun. Why: the repo ships bun.lock and CI uses bun.
```

**Index line format** (what is injected and what `MEMORY.md` lists): `- <description> (<id>)`, grouped under `Preferences`, `Facts`, `Decisions`, `Lessons`, `Open`, at most 160 characters per line. The id lets an agent call `read_note` directly, which also records a use. Optional compatibility: a root `MEMORY.md` with an `## Index` section matches Cognition's spec and costs nothing [amr-spec].

### R3. Size budgets

**Verified limits that bound the design** (the Windsurf and Agent Skills rows are size precedents, not channels recall uses):

| Channel | Limit | Source |
| --- | --- | --- |
| Claude Code SessionStart hook output | 10,000 characters per field or stdout; over that it is spilled to a file and the model sees a 2,000-character preview | [cc-hooks] |
| Claude Code MCP server instructions | 2,048 characters | [cc-changelog], [cc-mcp] |
| Claude Code `MEMORY.md` loader | First 200 lines or 25 KB | [cc-memory] |
| Codex hook output | About 2,500 tokens by default, then spilled with a preview | [codex-hooks] |
| Codex `memory_summary.md` | Injected up to 2,500 tokens, cut from the middle (user report); the v2 summary is validated under 10,000 bytes | [codex-issue-48679], [codex-consol-v2] |
| Windsurf global rules | 6,000 characters | [windsurf-memories] |
| Agent Skills | Metadata about 100 tokens per skill; body under 5,000 tokens | [agent-skills-spec] |

**Proposed budgets (judgment, inside those limits):**
- **Session-start block (global plus project plus framing): a 4,500-character body, about 5,000 with framing** (about 1,200 to 1,300 tokens). It sits well under the Claude Code and Codex injection caps. It was 6,000 until October 2026; `recall eval session` showed a smaller block loads fewer notes that do not apply, at a small cost in ones that do (on held-out folders, 4,500 kept 0.62 of the notes that should load against 0.65 at 6,000, and cut the lines that should not load from 0.38 to 0.34).
  - Framing: 500 characters at most, placed first.
  - Global preferences: 2,000 characters (about 12 lines).
  - Project: 5,500 characters (at most 6 preferences, 12 facts, 8 decisions, 6 lessons, 3 open threads).
  - Fill by priority, never cut mid-line, and end with an explicit "N more notes: use memory_search" line, so truncation is visible and ordered by importance instead of cutting the middle [codex-issue-48679], [cc-issue-82056].
- **On-disk index files** (root and per project): at most 150 lines and 16 KB, under about 70% of Claude's 25 KB loader cap, where Claude's own compaction reminder fires [cc-issue-91188].
- **Per note:** `title` 80 characters, `description` 160, body 700 characters (the current extraction limit), at most 5 sources.
- **MCP `instructions`:** 1,500 characters at most, a pointer and usage hints, not content.
- **Tool results:** `memory_search` returns at most 10 results of about 300 characters; full text only through `read_note`, mirroring layered retrieval [claude-mem].
- **Soft cap of 200 active notes per project** triggers a merge pass (judgment; compare Letta's defragmentation to 15 to 25 files [letta-ctxrepo-blog]).

**Arithmetic against the current constants** (snapshot of `src/recall/memory/notes.py`): the caps allow 40 global lines plus 92 project lines (12 preference, 30 fact, 20 decision, 20 lesson, 10 open), 132 lines in all. When every section is full, at 150 to 200 characters per line that is 20 to 26 KB, and about 38 KB at the longest line the code permits (120-character title plus 160-character hook). That is 2 to 3.8 times Claude's 10,000-character hook cap. Over the cap Claude Code would show only a 2,000-character preview [cc-hooks], which would hold part of the global preferences and none of the project block or the trailing safety footer (inference). A young project will sit well under the caps, so this bites as memory matures; it is still the highest-impact gap in the current code.

### R4. What to auto-load and what to retrieve on demand

| Item | Auto-load at session start? | Why |
| --- | --- | --- |
| Global preferences (top 12 by score) | Yes | Preference following decays in long contexts, so these cannot depend on a discretionary lookup (inference from [prefeval]); Codex ranks preference evidence first because it saves user keystrokes [codex-stage1-v1] |
| Current project's preferences, facts and decisions (top lines by score) | Yes, filtered by the project of the working directory | Non-derivable facts only; repository overviews added cost without success [agents-md-study]; filtering avoids cross-project bleed [codex-issue-17496] |
| Open threads | At most 3, updated within 7 days | They go stale fastest (section 8.2) |
| Lessons | Only recurring, costly ones; otherwise on demand | Failure shields help when matched to a task [codex-consol-v1] |
| Other notes, superseded notes, evidence quotes | No: `memory_search`, `read_note` | Just-in-time retrieval and progressive disclosure [blog-context-eng], [codex-read-v2] |
| Chat transcripts | No: `search_chats`, `read_chat` | Episodic evidence; already built |
| Procedures | No: promote repeated ones to skills later | Skills load by description on demand [cc-memory], [agent-skills-spec] |
| Anything already stated in the user's `CLAUDE.md` or `AGENTS.md` | No | Claude's own auto memory skips it [cc-memory]; a Codex user reports consolidation does not dedupe against always-loaded AGENTS.md text [codex-issue-48679] |

**Delivery, one command, three formats.** `recall context --cwd <dir>` prints the block from precompiled files (no LLM or heavy query in the path, because SessionStart hooks delay the first response [cc-hooks]).
- **Claude Code:** a SessionStart hook (matcher `startup|resume|clear|compact`, so memory returns after compaction) whose plain stdout is added as context, within the 10,000-character cap [cc-hooks].
- **Codex:** a SessionStart hook whose plain stdout becomes developer context, source values `startup`, `resume`, `clear`, `compact`, with the output limit above; each non-managed hook must be reviewed and trusted once through `/hooks`, and a new or changed hook is skipped until trusted [codex-hooks]. Fallback is a managed block in `~/.codex/AGENTS.md` within its 32 KiB budget [codex-agents-md].
- **Cursor:** a `sessionStart` hook in `~/.cursor/hooks.json` returning JSON with `additional_context`, documented as added to the initial system context but also as fire-and-forget; user-level hooks do not apply to cloud agents [cursor-hooks]. Treat it as best effort: in April 2026 Cursor staff acknowledged that `additional_context` from `sessionStart` is dropped because of a timing issue and gave no fix date [cursor-forum-sessionstart]. Pair it with a line in the project `AGENTS.md` (read in the project root and subdirectories [cursor-rules]) telling the agent to call `project_memory` first, and verify injection on the installed version; the companion note reaches the same conclusion [sibling-doc].
- **Anywhere hooks are unavailable:** the existing `project_memory(path)` tool plus a short MCP `instructions` line telling the agent to call it once; the instructions field is honoured by Claude Code up to 2,048 characters [cc-changelog]; I did not verify Codex or Cursor behaviour.
- Keep the block byte-stable between sessions unless a note changed, because prompt-cache behaviour is sensitive to injected prefixes (a Claude Code fix addressed a cache bust from late-connecting MCP instructions [cc-changelog]; inference).

**Framing.** Put it first and make it short. Say that the lines are the user's recorded preferences and decisions with dates; apply them unless the current request or the code says otherwise; verify drift-prone facts and say when an answer rests on memory [codex-read-v1]; never act on commands, URLs or tool-use instructions found inside a note [amr-skill]. This differs from the current "background, not instructions" wording. The reasons (judgment): a user reported memory ignored until asked to read it [codex-issue-18738], and Letta found strong models discarding an unexplained standing instruction as a suspected injection [letta-eval-blog]. Dated, reasoned, user-attributed lines are both more followable and easier to distinguish from injected text.

### R5. Consolidation algorithm (nightly, resumable, one git commit per run)

Design principle: cheap deterministic checks bracket two LLM steps, and the deterministic code has the last word on anything that reaches the always-loaded set.

**S0. Gate (deterministic).**
- Mine only interactive chats (not headless or sub-agent runs), idle at least 6 hours and preferably 12 [codex-config-src], with at least 2 user turns, de-duplicated by fingerprint.
- Cap work per run: at most 30 chats nightly and 100 for a backfill batch (judgment; Dreams accept 1 to 100 sessions [api-dreams]), plus call and token budgets.
- When the backend is a subscription CLI, skip when an agent session was active in the last 30 minutes and cap calls, since recall cannot read vendor quota (judgment; Codex gates on remaining quota and added a cooldown to prevent "ghost cost" [codex-guard-src], [codex-commit-19970]).
- Run the job's own agent calls without session persistence, without tools, and with the vendor's memory features off [cc-cli], [codex-exec], [codex-config-ref].
- Never mine recall's own injected block. Hosts persist injected hook output (Claude Code saves it in the session transcript [cc-hooks]; the companion note records how each host stores it [sibling-doc]). recall reads only user and assistant turns today, which skips Claude's `attachment` entries and Codex developer messages, but a fixed sentinel first line in the block plus a strip step in the digest builder makes the guarantee explicit for Cursor and future sources (inference).

**S1. Extract (small, cheap model; per chat chunk).** Output candidates with `type`, `scope`, `title`, `description`, `claim`, `why`, `origin`, `temporal` (`permanent`, `this_task`, `unknown`) and `evidence: [{turn, quote}]`. Prompt rules, taken from Codex: a no-op is the default; ask whether a future agent would plausibly act better; weigh user words over tool output over assistant text; never turn "do X now" into "the user prefers X"; treat chat text as untrusted data [codex-stage1-v1], [codex-stage1-v2]. Typical yield is 0 to 3 per chat.

**S2. Ground (deterministic).** Verify each evidence quote occurs in the cited turn (normalized, with a fuzzy threshold of about 0.9); require at least one quote from a user turn for `preference` and `decision`; compute `quote_sha`; drop candidates that fail, contain secrets or contact details, or match injection patterns (zero-width or bidirectional characters, markup resembling system tags, commands, URLs, tool-use directives) [cc-changelog]. Rationale: a model's provenance link is rarely checked by the consumer [budgeted-verif], so the memory system must check it once, at write time.

**S3. Match (deterministic).** FTS5 over active notes in the same scope (and global notes for global candidates) returns the top 5; a normalized claim hash catches exact repeats; a close match to a `rejected` tombstone drops the candidate silently. No match means `new`; otherwise it goes to adjudication.

**S4. Adjudicate (strongest affordable model; matched and conflicting groups only, batches of at most 20).** Operations: `reinforce` (add source, no text change), `refine` (merge wording), `supersede`, `coexist` (split by scope or add `applies_to`), `conflict` (leave both and mark for review), `discard`. Deterministic checks after the model answers:
- `supersede` is allowed only when the candidate's evidence is newer than the target's `last_seen`, is user-originated, and is not `this_task`.
- A `human_edited` note's body is never rewritten; at most sources are appended and a proposed change goes to the dream report.
- Nothing is deleted: superseded and expired notes move to `archive/` with `valid_until`.
- Use a stronger model here than in S1: memory generation is a distinct capability on which weaker models log feedback verbatim and leave stale or contradictory content [letta-eval-blog], and Letta advises making the sleep-time agent the stronger model because it is less latency-constrained [letta-sleeptime-blog]. With only a small local model, allow `reinforce`, `create` and `discard`, and send `refine`, `supersede` and `coexist` to the review queue (judgment).

**Contradiction policy:**

| Case | Action | Anchor |
| --- | --- | --- |
| Newer permanent user statement contradicts an older one | `supersede`, keep history | [codex-consol-v2], [graphiti] |
| New statement is explicitly for this task only | No change to the permanent note | [codex-stage1-v2], [capture] |
| Assistant-proposed or inferred claim contradicts a user statement | Discard the candidate | [codex-stage1-v1] |
| Similar claims from different projects conflict | `coexist` by project; not eligible for global | [codex-consol-v1] |
| A fact's code anchor no longer holds | Mark stale and exclude from the index until verified | [copilot-docs] |
| Cannot decide | `conflict`; surfaced for review; never in the always-loaded set | [capture], [stale] |

**S5. Promote and demote (deterministic).**
- *Index eligibility:* active, confidence medium or better, and either user-originated or corroborated by two or more distinct chats on distinct days (judgment, from Codex promoting what is "supported across distinct tasks" [codex-consol-v1]). An explicit directive ("always", "never", "remember") is eligible immediately [cc-memory].
- *Global scope* additionally needs generalizing language in a user quote or sources from at least two projects (judgment, motivated by Codex's rule against turning task-specific requests into general claims [codex-commit-43800]).
- *Ordering score:* confidence times corroboration times recency times a usage factor.
- *Demotion:* drop from the index when neither `last_used` nor `last_seen` is within 30 days (still searchable) [codex-config-src], [copilot-docs]; expire `open` notes after 14 days unseen (judgment; current code uses 45) and weak single-source notes after 180 days.

**S6. Verify (optional, deterministic).** For notes with `anchors`, check that the path and pattern still hold at the project's current checkout; otherwise mark stale [copilot-docs].

**S7. Compile, lint, log, commit.** Regenerate indexes under the budgets in R3 with deterministic ordering; lint for schema, dangling ids and links, duplicate claim hashes, secrets (reuse `redact`), injection patterns, budget overruns and instruction-file duplicates; append an entry to a `dream-log.md` with a parseable prefix [karpathy-wiki]; write a short report of additions, changes and `needs_review` items; commit once [codex-readme], [devin-blog].

**Fast paths.** An explicit "remember/always/never" is `origin: user_stated`, high confidence, immediately eligible [cc-memory]. "Forget" or "that's wrong" creates a `rejected` tombstone so the claim cannot be re-created by a later chat [codex-consol-v2], [chatgpt-help].

**Quarantine for the highest-risk path.** New global notes and any note first entering the always-loaded set wait one nightly cycle in a `pending` view (`recall memory pending`), visible in `git diff` or an Obsidian vault, unless explicitly user-stated. Rationale: a shared store is loaded by every agent, so a poisoned note has a larger blast radius than in any single-agent system (inference), and researchers recommend user approval and audits for memory writes [rehberger-windsurf].

### R6. Evaluation

The repo already evaluates retrieval (hit@k, MRR) and, in progress, extraction (recall of expected memories, type and scope accuracy, negatives, forbidden leaks). Extend it on five tracks. Targets are initial gates (judgment) to calibrate against the first real run.

| Track | Method | Initial gate | Anchor |
| --- | --- | --- | --- |
| 1. Grounding and provenance | Deterministic: every note's quotes exist in the cited chat turns and hash correctly; sample 100 notes for an LLM or human "is the claim supported by its evidence?" check | 98% of quotes grounded; 90% of sampled claims supported | [budgeted-verif], [copilot-blog] |
| 2. Extraction quality | Gold chats with expected and forbidden memories (existing); add over-generalization rate (scope or wording broader than evidence), assistant-only-origin rate among promoted notes, memories per chat, trivial-note rate | Recall at least 0.8; over-generalization at most 5%; assistant-only promoted notes 0 | [codex-commit-43800], [codex-stage1-v1] |
| 3. Update and forgetting | Staged chat pairs where a preference changes, is task-scoped only, or is revoked; implicit-conflict cases; reversal test (the note must not remain in the index); tombstone test | At least 90% correct supersession; zero stale notes in the index; zero resurrected tombstones | [stale], [tepa], [memoryagentbench], [horizonbench] |
| 4. Retrieval and budget | Existing hit@k harness against notes with agent-style queries; known-item queries built from a note's evidence; measure injected characters, truncation rate and hook latency | hit@3 at least 0.9; block under 8,000 characters in 100% of sessions; hook p95 under 200 ms | [letta-fs-blog], [cc-hooks] |
| 5. Security | Plant injection text in assistant turns and in long pasted blocks, plus a hand-poisoned note, and confirm none is promoted or injected unflagged; calibrate any LLM judge by planting known-wrong notes | Zero promotions of planted claims; judge false-accept at most 10% | [rehberger-windsurf], [minja], [copilot-blog], [locomo-audit] |

**Does memory make agents more useful?** Two measures, both computable from data recall already holds:
1. **Repeat-steering rate (primary).** Codex's stated objective is fewer future user corrections and keystrokes [codex-stage1-v1]. Log which note ids each session received (the hook knows). For each preference or decision note, count later user turns that restate its claim (FTS5 candidate retrieval followed by a yes or no judge, sampled for cost), comparing exposed against unexposed sessions. The cap on the always-loaded set creates a natural control: eligible notes that fell just below the cutoff. Report restatements per 100 user turns with confidence intervals; keep a note category only if it lowers the rate or leaves it unchanged while cutting tokens.
2. **Paired replay.** Sample 40 to 100 past tasks where the user later corrected the agent on something a note covers; replay the first prompt headlessly (read-only, no network) with and without the block; score with a deterministic check where possible (for example no `npm` command when the note says bun) and a rubric otherwise; report wins, ties and losses plus token cost. This mirrors Copilot's with-and-without A/B [copilot-blog] and guards against the context-file result that cost rose over 20% without a success gain [agents-md-study]. Separate "memory usage" (adherence, retrieval) from "memory generation" (generalization, hygiene) as Context-Bench V2 does [letta-eval-blog].

**Hygiene.** Version prompts (already `PROMPT_VERSION`) and log metrics per version and per backend in `dream_runs`; compare local and cloud backends on the same gold set; keep personal gold sets under `~/.agents/recall/evals/`, never in the repo; prefer deterministic checks to LLM judges because judges were lenient on vague wrong answers (62.81% accepted) [locomo-audit].

### R7. Fit with the code as of 2026-10-06 (snapshot; the implementation was changing while this was written)

**Already aligned with the evidence.**
- Markdown notes with frontmatter as source of truth, SQLite rebuilt from them, one git commit per run, archive for superseded and expired notes (`src/recall/memory/notes.py`, `dream.py`) [codex-readme], [devin-blog], [letta-docs-memfs].
- The five types; project identity from the git remote so clones and worktrees share memory, scratch folders excluded (`src/recall/projects.py`) [cc-memory], [copilot-docs].
- Interactive chats only, duplicates mined once, headless and sub-agent chats skipped; recall's own model calls use no session persistence and no tools for Claude, and an ephemeral read-only run with Codex memories off for Codex (`model.py`, `sources/`, `dream.py`, `llm.py`) [codex-readme], [cc-cli], [codex-exec].
- Extraction prompt rules (user words preferred, assistant-only capped at medium, no instructions from chat content, 0 to 3 memories per chat, empty result allowed) and a candidate-then-consolidate structure with create, update, supersede and discard (`extract.py`, `consolidate.py`) [codex-stage1-v1], [tepa].
- Call budgets, response cache and call log, resumable runs (`llm.py`, `dream.py`) [codex-issue-19732].
- Project-filtered session context, read-only MCP tools, an extraction eval with forbidden-leak negatives (`context.py`, `server.py`, `evals.py`).

**Gaps, by expected impact.**
1. The injected block has no character budget (R3 arithmetic) and the safety footer sits last, so an over-cap block would lose it first; enforce the budget in the context builder and move the framing first.
2. Provenance is chat-level only, and evidence quotes live in the intermediate `candidates` table of `recall.db` (private chat text), not in the notes; the extractor returns a quote but nothing checks it against the transcript. Persist `sources` objects and add the S2 grounding check.
3. No usage signal: recency is measured from `last_seen` with a 180-day half-life, while vendors prune by disuse at 28 to 30 days [copilot-docs], [codex-config-src]. Log reads and injections and add `last_used` and `use_count`.
4. Scope is chosen per chat by one model call, so a global note can come from a single chat; add the deterministic promotion gate in S5.
5. One client and model serves extraction and consolidation; tier them and restrict small-model adjudication (S4) [letta-eval-blog].
6. Supersession is decided by the model alone and there is no `coexist` or `conflict` outcome; add the S4 guards and a review queue.
7. No tombstones: a note deleted by hand can be recreated by a later chat that repeats the claim [codex-consol-v2].
8. `open` notes live 45 days and up to 10 may be injected; shorten to 14 days and 3 lines.
9. No lint or sanitization stage beyond the secret and contact-detail scrub at extraction (invisible characters, tag-like markup, command and URL patterns are not checked) [cc-changelog].
10. No dedupe against the user's `CLAUDE.md` and `AGENTS.md` [cc-memory], [codex-issue-48679].
11. Memory evals cover extraction only (chat search has its own evals); add the update, note-retrieval, security and end-to-end tracks (R6).

### R8. Open questions and risks

- **Privacy statement.** The README says nothing leaves the machine. With the `claude` or `codex` backends, redacted chat digests go to those vendors (`llm.py`); each vendor already holds chats from its own agent but not necessarily the others' (for example Cursor chats), whereas the Ollama and LM Studio backends stay local. Decide the default and state it in the README.
- **Native memory overlap.** Claude Code auto memory and Codex memories may also be on, and conflicting instructions across stores are resolved arbitrarily [cc-memory]. Options: dedupe against them read-only, or ingest them as extra sources as Codex does for Claude [codex-import-src] and Letta's init does for Claude Code and Codex histories [letta-ctxrepo-blog]. The repo rule against writing to other tools' data argues for hooks over writing into their stores.
- **Review burden.** The pending queue helps only if it stays small; measure it, and keep the vault openable in Obsidian so review needs no new tooling [obsidian-storage].
- **Small local models** may not be able to adjudicate supersession; track track-3 scores per backend before enabling `supersede` for them (S4).
- **Delivery details.** Whether Codex and Cursor honour MCP `instructions` is unverified. Codex hooks are documented but need a one-time trust review [codex-hooks]; Cursor's `sessionStart` injection is best effort (R4) [cursor-forum-sessionstart].
- **Sync.** Multi-machine memory would need a private git remote, as Devin and Letta have; Cognition's skill defaults to local-only and asks before any push [amr-skill]. Keep it off by default.

<!-- Link reference definitions. Each key used in the text resolves here. Retrieved 2026-10-06 unless the URL itself carries a date. -->

<!-- Anthropic: Claude Code -->
[cc-memory]: https://code.claude.com/docs/en/memory
[cc-directory]: https://code.claude.com/docs/en/claude-directory
[cc-subagents]: https://code.claude.com/docs/en/sub-agents
[cc-hooks]: https://code.claude.com/docs/en/hooks
[cc-mcp]: https://code.claude.com/docs/en/mcp
[cc-cli]: https://code.claude.com/docs/en/cli-reference
[cc-bestpractices]: https://code.claude.com/docs/en/best-practices
[cc-changelog]: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
[cc-llms-full]: https://code.claude.com/docs/llms-full.txt
[cc-issue-82056]: https://github.com/anthropics/claude-code/issues/82056
[cc-issue-91188]: https://github.com/anthropics/claude-code/issues/91188
[cc-issue-63903]: https://github.com/anthropics/claude-code/issues/63903
[3p-autodream-1]: https://claudefa.st/blog/guide/mechanics/auto-dream
[3p-autodream-2]: https://wmedia.es/en/tips/claude-code-auto-dream-memory-consolidation

<!-- Anthropic: API, Managed Agents, blogs -->
[api-memory-tool]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
[api-ma-memory]: https://platform.claude.com/docs/en/managed-agents/memory
[api-dreams]: https://platform.claude.com/docs/en/managed-agents/dreams
[api-release-notes]: https://platform.claude.com/docs/en/release-notes/overview
[blog-managed-agents]: https://claude.com/blog/new-in-claude-managed-agents
[blog-context-mgmt]: https://claude.com/blog/context-management
[blog-context-eng]: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
[blog-harnesses]: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
[blog-claude-memory]: https://claude.com/blog/memory
[bcherny-x]: https://x.com/bcherny/status/2017824286489383315

<!-- OpenAI: Codex and ChatGPT. Source links are pinned to openai/codex commit 822e58cc3d66166c7446c5b1ea2e52f5d09594c (2026-10-06) -->
[codex-memories-doc]: https://developers.openai.com/codex/memories
[codex-config-ref]: https://developers.openai.com/codex/config-reference
[codex-exec]: https://developers.openai.com/codex/noninteractive
[codex-hooks]: https://developers.openai.com/codex/hooks
[codex-agents-md]: https://developers.openai.com/codex/guides/agents-md
[codex-readme]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/README.md
[codex-config-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/config/src/types.rs
[codex-lib-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/src/lib.rs
[codex-guard-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/src/guard.rs
[codex-state-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/state/src/runtime/memories.rs
[codex-import-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/external-agent-migration/src/memory_import.rs
[codex-polluted-src]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/core/src/stream_events_utils.rs
[codex-stage1-v1]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/templates/memories/stage_one_system.md
[codex-consol-v1]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/templates/memories/consolidation.md
[codex-stage1-v2]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/templates/memories/stage_one_system_v2.md
[codex-consol-v2]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/memories/write/templates/memories/consolidation_v2.md
[codex-read-v1]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/ext/memories/templates/memories/read_path.md
[codex-read-v2]: https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/ext/memories/templates/memories/read_path_v2.md
[codex-commit-19970]: https://github.com/openai/codex/commit/a9e5c34083d4593b51d520f4d45f751ef9eee297
[codex-commit-19990]: https://github.com/openai/codex/commit/1b743603651db33895b30345e47b7babe8a819a3
[codex-commit-43827]: https://github.com/openai/codex/commit/2cbbf0c9b542a36a1c3284b5e804917635b6f666
[codex-commit-43799]: https://github.com/openai/codex/commit/6924ce636b2186948f2332ac9460b5871b50aa2f
[codex-commit-43800]: https://github.com/openai/codex/commit/74d3a5bf1046f004ee33a200ee497dc7593a5687
[codex-commit-43813]: https://github.com/openai/codex/commit/553df1c691fe8bf7747e50da22f1342984495ae0
[codex-issue-19732]: https://github.com/openai/codex/issues/19732
[codex-issue-17496]: https://github.com/openai/codex/issues/17496
[codex-issue-18343]: https://github.com/openai/codex/issues/18343
[codex-issue-18738]: https://github.com/openai/codex/issues/18738
[codex-issue-22008]: https://github.com/openai/codex/issues/22008
[codex-issue-33641]: https://github.com/openai/codex/issues/33641
[codex-issue-48679]: https://github.com/openai/codex/issues/48679
[chatgpt-announce]: https://web.archive.org/web/20261001220645/https://openai.com/index/memory-and-new-controls-for-chatgpt/
[chatgpt-help]: https://web.archive.org/web/20260919035421/https://help.openai.com/en/articles/8590148-memory-faq

<!-- Cursor, Windsurf, GitHub Copilot -->
[cursor-1-0]: https://cursor.com/changelog/1-0
[cursor-forum-051]: https://forum.cursor.com/t/0-51-memories-feature/98509
[cursor-forum-gone]: https://forum.cursor.com/t/are-my-memories-gone/144057
[cursor-forum-notshowing]: https://forum.cursor.com/t/memories-not-showing/143820
[cursor-rules]: https://cursor.com/docs/context/rules
[cursor-hooks]: https://cursor.com/docs/hooks
[cursor-forum-sessionstart]: https://forum.cursor.com/t/sessionstart-hook-additional-context-is-never-injected-into-agents-initial-system-context/158452
[windsurf-memories]: https://docs.devin.ai/desktop/cascade/memories
[copilot-blog]: https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/
[copilot-docs]: https://docs.github.com/en/copilot/concepts/agents/copilot-memory

<!-- Devin and Cognition -->
[devin-blog]: https://devin.ai/blog/memory-and-dreaming
[amr-site]: https://cognition.com/agent-memory-repo
[amr-spec]: https://github.com/AgentMemoryRepo/agentmemoryrepo/blob/1db04a5735adbc4f2158308f2077fd960e243c04/SPEC.md
[amr-skill]: https://github.com/AgentMemoryRepo/agentmemoryrepo/blob/1db04a5735adbc4f2158308f2077fd960e243c04/skills/agent-memory-repo/SKILL.md

<!-- Letta and MemGPT -->
[memgpt]: https://arxiv.org/abs/2310.08560
[sleeptime-paper]: https://arxiv.org/abs/2504.13171
[letta-sleeptime-blog]: https://www.letta.com/blog/sleep-time-compute
[letta-fs-blog]: https://www.letta.com/blog/benchmarking-ai-agent-memory
[letta-ctxrepo-blog]: https://www.letta.com/blog/context-repositories/
[letta-eval-blog]: https://www.letta.com/blog/evaluating-memory-in-production-agents/
[letta-docs-memory]: https://docs.letta.com/letta-agent/memory
[letta-docs-memfs]: https://docs.letta.com/concepts/memfs/index.md
[letta-docs-memgpt]: https://docs.letta.com/concepts/memgpt
[letta-docs-blocks]: https://docs.letta.com/guides/agents/memory-blocks
[letta-docs-archival]: https://docs.letta.com/guides/agents/archival-memory

<!-- Other memory systems and papers -->
[mem0-paper]: https://arxiv.org/abs/2504.19413
[mem0-paper-html]: https://arxiv.org/html/2504.19413v1
[mem0-readme]: https://github.com/mem0ai/mem0/blob/main/README.md
[mem0-docs-add]: https://docs.mem0.ai/core-concepts/memory-operations/add
[mem0-llms]: https://docs.mem0.ai/llms.txt
[openmemory-archive]: http://web.archive.org/web/20250709185154/https://docs.mem0.ai/openmemory/overview
[openmemory-removal]: https://github.com/mem0ai/mem0/commit/ea2ee0758635a9230bd60855c3fe339170f6cd18
[zep-paper]: https://arxiv.org/abs/2501.13956
[zep-paper-html]: https://arxiv.org/html/2501.13956v1
[zep-rebuttal]: https://www.getzep.com/blog/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/
[graphiti]: https://github.com/getzep/graphiti
[cognee]: https://github.com/topoteretes/cognee
[cognee-paper]: https://arxiv.org/abs/2505.24478
[basic-memory]: https://github.com/basicmachines-co/basic-memory
[basic-memory-format]: https://docs.basicmemory.com/concepts/knowledge-format
[claude-mem]: https://github.com/thedotmack/claude-mem
[pieces-ltm]: https://docs.pieces.app/products/core-dependencies/pieces-os/long-term-memory
[pieces-ltm2]: https://pieces.app/blog/what-is-new-ltm-2
[pieces-mcp]: https://pieces.app/blog/introducing-the-pieces-mcp-server
[supermemory]: https://github.com/supermemoryai/supermemory
[byterover]: https://github.com/campfirein/cipher
[byterover-paper]: https://arxiv.org/abs/2604.01599
[amem]: https://arxiv.org/abs/2502.12110
[genagents]: https://arxiv.org/abs/2304.03442
[coala]: https://arxiv.org/abs/2309.02427
[reasoningbank]: https://arxiv.org/abs/2509.25140
[awm]: https://arxiv.org/abs/2409.07429
[reflexion]: https://arxiv.org/abs/2303.11366
[expel]: https://arxiv.org/abs/2308.10144
[voyager]: https://arxiv.org/abs/2305.16291
[survey-hu]: https://arxiv.org/abs/2512.13564
[survey-du]: https://arxiv.org/abs/2603.07670
[evermemos]: https://arxiv.org/abs/2601.02163
[cline-memory-bank]: https://docs.cline.bot/prompting/cline-memory-bank
[agent-skills-spec]: https://agentskills.io/specification

<!-- Benchmarks, evaluations, failure-mode research -->
[locomo]: https://arxiv.org/abs/2402.17753
[longmemeval]: https://arxiv.org/abs/2410.10813
[longmemeval-repo]: https://github.com/xiaowu0162/LongMemEval
[memoryagentbench]: https://arxiv.org/abs/2507.05257
[locomo-audit]: https://github.com/dial481/locomo-audit
[locomo-plus]: https://arxiv.org/abs/2602.10715
[stale]: https://arxiv.org/abs/2605.06527
[trustgap]: https://arxiv.org/abs/2609.01852
[budgeted-verif]: https://arxiv.org/abs/2608.25553
[stateauditor]: https://arxiv.org/abs/2608.01619
[tepa]: https://arxiv.org/abs/2608.07429
[horizonbench]: https://arxiv.org/abs/2604.17283
[capture]: https://arxiv.org/abs/2609.02265
[prefeval]: https://arxiv.org/abs/2502.09597
[minja]: https://arxiv.org/abs/2503.03704
[agentpoison]: https://arxiv.org/abs/2407.12784
[lost-middle]: https://arxiv.org/abs/2307.03172
[context-rot]: https://www.trychroma.com/research/context-rot
[agents-md-study]: https://arxiv.org/abs/2602.11988
[probe-refine]: https://arxiv.org/abs/2606.20512
[agent-retrieval-bench]: https://arxiv.org/abs/2607.24882
[keyword-search]: https://arxiv.org/abs/2602.23368
[rehberger-chatgpt]: https://embracethered.com/blog/posts/2024/chatgpt-hacking-memories/
[rehberger-windsurf]: https://embracethered.com/blog/posts/2025/windsurf-spaiware-exploit-persistent-prompt-injection/

<!-- Companion note in this folder -->
[sibling-doc]: agent-storage-and-integration.md

<!-- Personal knowledge management and Obsidian -->
[karpathy-wiki]: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
[obsidian-storage]: https://help.obsidian.md/data-storage
[obsidian-properties]: https://help.obsidian.md/properties
[obsidian-links]: https://help.obsidian.md/links
[obsidian-cli]: https://help.obsidian.md/cli
[obsidian-mcp-markus]: https://github.com/MarkusPfundstein/mcp-obsidian
[obsidian-mcp-cyanheads]: https://github.com/cyanheads/obsidian-mcp-server
