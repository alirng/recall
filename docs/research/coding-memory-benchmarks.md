# Benchmarks for coding-agent memory: what fits recall, and what it scores

Research note for `recall`, 2026-10-12. Question: which public benchmarks measure what recall
does in production, a coding agent in a new session finding and using the decisions,
conventions and past fixes of earlier sessions, better than LongMemEval and LoCoMo (synthetic
personal-assistant chats)? It extends [retrieval-benchmarks.md](retrieval-benchmarks.md), which
covers recall's search on those two.

The benchmarks are sorted against four product metrics:

| ID | Metric | What a benchmark must provide |
| --- | --- | --- |
| M1 | Find rate | A corpus of past sessions and, per query, the session(s) that answer it |
| M2 | Answer or task success | A task an agent completes with recall's tools, graded by tests or checkable claims |
| M3 | Tokens per answer | Token counts from M2 runs |
| M4 | Note quality | Gold memories per session, so extraction recall, precision and updates can be scored |

Evidence grades: **source** (read in the data or code at a pinned commit, or checked through the
Hugging Face, GitHub or Docker Hub APIs), **docs** (a paper, README or dataset card), **vendor**
(a score the seller ran on its own product), **competitor** (a score run by the maker of a
competing product), **independent** (run by paper authors with no product in the comparison).
Section 4 holds recall's own measurement.

## Summary

1. **Two public benchmarks are built from coding-agent sessions whose answer sits in an earlier
   session**: SWE-ContextBench (real GitHub issue pairs, with 300 Claude Code transcripts as the
   memory for its Lite split) and agent-memory-bench, "AMB" (Claude Code tasks graded by
   executable checkers over a corpus of Claude Code sessions) [swecb-paper], [amb-repo]. Both can
   be scored for retrieval today at no cost; their end-to-end protocols need Docker, which this
   machine does not have.
2. **SWE-ContextBench Lite is the first public set where recall is far from its ceiling.** With
   the related issue as the query, the right past session is in recall 0.2.1's top 5 for 41-54%
   of tasks and in its top 10 for up to 79%, against 99% on LongMemEval_S (section 4). That
   leaves room to hillclimb, and roughly matches the retrievers the paper tested (Mem0,
   OpenViking, LangMem), which all spend LLM calls at ingest; recall spends none. (What 0.3.0
   scores after the hillclimb: [coding-memory-hillclimb.md](coding-memory-hillclimb.md).)
3. **On end-to-end coding benchmarks, memory products rarely beat no memory, and the samples are
   too small to tell them apart.** SWE-ContextBench: Mem0 24.2% resolved against 26.3% without
   context [swecb-paper]. VibeMemBench: 11 of 12 product and model pairings at or below memory
   off [vibemem]. MEMTRACK: Mem0 and Zep within one point of none [memtrack]. AMB: its official
   run is a null, with the placebo arm on top [amb-status]. AML Coding: 57 of 58 entries between 46 and 53 on 150 tasks,
   a plain SQLite FTS baseline at 50 [aml-coding]. At 99-150 tasks one score carries about ±8-10
   points. Task success is worth reporting; hillclimbing needs the retrieval sets.
4. **For note quality, HaluMem is the only public harness that scores free-form extracted
   memories** for recall, precision, updates and hallucination [halumem], but on personal-life
   chats and under a non-commercial, no-derivatives licence. MemoryCode is the only coding-domain
   set where conventions are added and then changed across sessions [memorycode]. No public
   benchmark checks whether secrets leak into notes.
5. **Several of the candidates do not fit or cannot run here.** DreamBench-SWE withholds its
   oracles, VibeMemBench is unreleased, TEPA is a method rather than a benchmark,
   LongMemEval-V2 is web-agent trajectories with the answer labels removed, Letta Context-Bench
   ranks models over synthetic files, and Supersede is LongMemEval's knowledge-update subset,
   which recall already runs (sections 2 and 3).
6. **Name clash.** AMB's GitHub README says it was "built by the authors of recall", and its
   leaderboard has a `recall` arm. That is RE-call (`recall-rag` on PyPI, by AMB's maintainer), a
   different product; nothing links it to this project [amb-repo], [recall-rag]. Any result for this
   project on AMB has to be labelled `recall-agents`.

## 1. Candidates at a glance

| Benchmark | Memory corpus | Scored by | Anchors | Fit | Public, licence, size | Needs | Runnable here |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SWE-ContextBench [swecb-paper] | Claude Code transcripts of earlier, linked GitHub issues (Lite); issue + gold patch records (full set) | Gold link (M1); SWE-bench tests (M2) | M1, M2, M3 | High | Yes; MIT card; 8 MB data + 121 MB transcripts | Docker (amd64) for tests | M1 now; M2 needs Docker |
| AMB [amb-repo] | Claude Code sessions with planted decisions, plus distractors | Executable checkers, no LLM judge | M1, M2, M3, part of M4 | High | Yes; Apache-2.0; 1.3 MB corpus | Claude Code CLI; rootless Docker on Linux for official runs | M1 now; M2 with a deviation |
| MemoryCode [memorycode] | Synthetic mentor chats that add and update coding conventions | Regex over generated code | M1, M2, M4 | Medium | Yes; Apache-2.0; 12 MB | Any LLM | Yes |
| HaluMem [halumem] | Synthetic personal-life chats, gold memory points per session | GPT-4o judge | M4, M2 | Medium-high for M4 | Yes; CC-BY-NC-ND-4.0; 34 MB (Medium) | OpenAI-compatible judge | Yes |
| STALE [stale] | Personal chats where a fact silently goes stale | Gemini judge; answer sessions given | M1, M2, M4 (stale facts) | Medium | Yes; CC-BY-4.0; 306 MB | Judge key | Yes |
| DreamBench-SWE [dreambench] | Three-session coding "traps" (conventions, stale architecture, reviewer preferences) | Hidden executable oracles | M2, M3, M4 | High in design | Package public; oracles withheld | Containers, Codex | No |
| VibeMemBench [vibemem] | 3,634 earlier same-repo agent trajectories | Repo tests | M2, M3 | Medium-high | Not released | Docker | No |
| AML Coding [aml-docs] | Earlier engineering tasks in the same repos (private) | Locked answer model, platform scoring | M2, M3 | High | Private; hosted challenge | Public HTTPS Add/Search API | Only by entering |
| MEMTRACK [memtrack] | Slack, Linear and Git event timelines | gpt-4o judge | M2, M3 | Medium-low | 47 instances; no licence; tools unreleased | Docker, Gitea | No |
| LongMemEval-V2 [lme-v2] | Web and ServiceNow browser-agent trajectories | Matchers + GPT-5.2 judge | M2, M3 | Medium-low | Yes; Apache-2.0; 7.1 GB | Qwen3.5-9B reader | Costly; no M1 labels |
| ETH "Evaluating AGENTS.md" [agentsmd] | None (context files only) | Repo tests | M2, M3 | Medium-low | Yes; MIT code | Docker | No |
| SWE-Bench-CL [swebench-cl] | The agent's own earlier runs | SWE-bench tests | M2 | Low-medium | Yes; MIT; 273 tasks | Docker | No |
| LoCoBench-Agent [locobench-agent] | Within-session context, LLM-generated code | Heuristics | none cleanly | Low | Yes; Apache-2.0 | Several API keys | Not worth it |
| Letta Context-Bench [context-bench] | Synthetic record files and skill libraries | gpt-5-mini judge | M2 for models | Low | Yes; Apache-2.0 | A Letta server | No |
| Supersede [supersede] | LongMemEval knowledge-update subset | Matcher | M4 (updates) | Low-medium | Yes; Apache-2.0 | none | Yes, adds little |
| TEPA [tepa] | (a memory method) | n/a | none | n/a | No release | n/a | n/a |

## 2. Coding-agent benchmarks

### 2.1 SWE-ContextBench (arXiv 2602.08316)

Zhu, Wu and others; affiliations include Oxford, Edinburgh, TUM, NUS, MBZUAI, CMU and MemoraX
AI, a memory vendor that is not among the systems evaluated [swecb-paper]. Each **related task**
is a real GitHub issue linked by hand, through issue and PR cross-references, to an earlier
**experience task** from SWE-bench Lite, Multilingual or Verified. The question is whether
an agent solves the related task better when it can reuse the experience.

What the release holds (source, [swecb-data], [swecb-repo]):

- 1,007 experience and 362 related tasks over 53 repos and 9 languages (the paper says 1,100,
  376 and 51). A `Relationship` table gives each related task's gold experience task(s); every
  link stays within one repo.
- **Lite**: 300 experience and 99 related tasks from 12 Python repos, and, in the GitHub repo
  only, the **300 Claude Code transcripts** (Claude Sonnet 4.5) that are the paper's memory
  corpus for Lite. They are Claude Code's own JSONL, which recall reads unchanged. The
  707 non-Lite experience tasks have issue text and gold patches but no transcripts.
- Task tests run in 357 prebuilt images on Docker Hub, **linux/amd64 only**, 455 GB compressed
  (127 GB for Lite) [swecb-images]. The evaluator pulls, tests and deletes one image at a time.

The paper's settings, on the 99 Lite tasks with Claude Code and Sonnet 4.5 (docs): no context;
the agent choosing among full trajectories or among their summaries; the gold trajectory or
gold summary handed over (oracle); and four memory products, which ingested the 300
transcripts and returned top-k results. Only the oracle summary moved the resolved rate by more
than a few tasks (34.3% against 26.3%). Its Table 5 reports how often each retriever returned the gold
experience ("Overall Matched"), but does not define the metric precisely or say what query text
three of the four products received.

Caveats found in the data (source): 118 of 362 links pair two tasks fixed by **the same PR**,
and for 115 links at least half of the related task's added lines also appear in the
experience task's gold patch, so for about a third of tasks the "past experience" is a run at
the same fix; for 118 links the "experience" PR was opened after the related one. This matters
most for end-to-end runs, where an oracle can hand over the answer. For retrieval it is closer
to production: a duplicate bug fixed last month is exactly what recall should find.

Fit: **high for M1** (real repos, real issue text, Claude Code transcripts as the haystack,
and the gold session known), medium for M2 (past fixes only; no conventions or decisions).
recall plugs in as an index over the transcripts, queried with the related issue; section 4
measures it.

### 2.2 agent-memory-bench, "AMB"

Maintained by Giulio D'Erme on GitHub (Apache-2.0, head `c1b1572`, 2026-10-06), mirrored to a
Hugging Face Space [amb-repo], [amb-space]. The maintainer builds RE-call, which competes in it
as the `recall` arm; the README discloses this.

- 34 executable tasks (26 in the official grid) on real fixture repos. Each depends on a fact
  from an earlier session: a governing decision or convention (30 tasks), facts combined or
  revised across sessions (3), or a failed approach to avoid (1) (docs).
- Five corpus conditions per task: the fact present, absent, superseded, contradicted, or
  belonging to another subsystem. Checkers run the artifact; there is no LLM judge (docs).
- Corpus: verbatim Claude Code sessions, 40 with planted facts and 156 distractors on
  Hugging Face (1.3 MB) [amb-corpus]; the official run used a larger generated haystack (about
  4,900 documents).
- Products enter through their shipped Claude Code integration (plugin, MCP server or hooks),
  which suits recall's MCP server and skill as they are (docs).
- Official runs execute on a Linux host with rootless Docker [amb-exec]. The control arms need
  only the Claude Code CLI (2.1.221 or later) and a model key (docs).

Its published run, `official-003` (deepseek-v4-flash, one seed per cell, 317 paired cells),
is a null: no arm's interval excludes zero, and the inert placebo arm scored highest
[amb-repo], [amb-status]. Its own status page lists further defects: corpus file names gave away
which documents were planted until 2026-09-26, the preregistration was committed after the run
began, and the write path (note taking) is not measured [amb-status].

Fit: **high**, the closest public design to recall's job, with three cautions: a competitor
runs it, its numbers so far are noise, and a non-Docker run on macOS would be an extension of
its protocol, not a replication. For M1 the task's planted session is the gold; for M2 the
checker is the grader.

### 2.3 MemoryCode (arXiv 2502.13791, ACL 2025)

Cohere Labs and others [memorycode]. 360 synthetic mentor-mentee histories of 1 to 100 sessions,
in which 51 coding conventions ("start argument names with `e_`") are introduced and 16 of them
updated up to seven times, among filler talk. The model then writes code for an unrelated
request, and regexes check that it follows the latest conventions. Sessions are labelled as
instruction-add, instruction-update or filler, which gives gold sessions for M1 and gold
memories for M4 [memorycode-data]. GPT-4o follows 79.6% of conventions over short histories and
30.5% over long ones; session-level RAG helped little (docs).

Fit: **medium**. Toy code and a non-agentic setup, but the only public coding set where
conventions change over time, and cheap: no Docker, no judge.

### 2.4 Promising, not runnable

- **DreamBench-SWE** (arXiv 2608.20664, one author) [dreambench]: 60 three-session traps in
  five families (conventions, generated files, stale architecture, reviewer preferences, flaky
  tests); the third session's diff is checked by hidden oracles. The public package leaves out
  the oracles and reference solutions, so no one else can score a new system. In its primary
  fold (GPT-5.5 through Codex), a verbatim event log passed 49.4% and hosted Mem0 with fact
  inference off 11.1%; its v2.1 successor audit reports no memory 11.7%, verbatim event memory
  45.6% and Mem0 with inference off 53.9% (independent). The two disagree on Mem0, so neither
  ranks the approaches.
- **VibeMemBench** (arXiv 2609.23570, Alibaba and others) [vibemem]: 111 tasks from 90 repos
  with 3,634 earlier same-repo agent trajectories as history; the repo says "Coming". Its
  failure analysis is the useful part: of 231 failures, 16.0% were retrieval coverage misses,
  1.3% ranking misses, 69.3% the retrieved memory in an unusable form, and 13.4% the agent not
  using it.
- **AML Coding track** [aml-docs]: a hosted challenge (150 tasks, relevant and noisy memory
  conditions, a locked answer model). The data is private; a system enters by exposing public
  HTTPS Add and Search endpoints. Cycle 2 materials are due 2026-10-31 (UTC+8), results
  mid-November.

### 2.5 Weak fit

- **MEMTRACK** (arXiv 2510.01353, Patronus) [memtrack]: 47 timelines of Slack, Linear and Git
  events, each small enough to fit in context; its tool servers are not released.
- **ETH "Evaluating AGENTS.md"** (arXiv 2602.11988) [agentsmd]: no memory corpus, but a finding
  that bears on recall's notes. LLM-generated context files changed resolution by about -0.5 to
  -2 points at 20-23% more cost; developer-written files gave about +2.4 (p = 0.21).
- **SWE-Bench-CL** (arXiv 2507.00014) [swebench-cl]: a course project with no memory results.
- **LoCoBench-Agent** (arXiv 2511.13998) [locobench-agent]: LLM-generated code, memory within one
  session, heuristic scores.
- **Letta Context-Bench** [context-bench]: ranks models navigating synthetic record files and
  skill libraries; only Letta agents can run it.
- **LongMemEval-V2** (arXiv 2605.12493) [lme-v2]: browser-agent trajectories (WebArena,
  WorkArena); the public files remove the answer-bearing labels, so no find-rate metric, and the
  comparable protocol needs a local Qwen3.5-9B reader over a 7.1 GB download.

## 3. Note-quality benchmarks (M4)

| Benchmark | What is scored | Domain | Notes |
| --- | --- | --- | --- |
| HaluMem [halumem] | Extraction recall and precision, update correct / hallucinated / omitted, QA | Synthetic personal life; Medium: 20 users, ~70 sessions each, 14,948 gold memory points, 3,467 questions | The harness takes each session's memories as a list of strings, so recall's notes fit. GPT-4o judge. CC-BY-NC-ND-4.0. MemTensor, which makes MemOS, co-authored it |
| STALE [stale] | Whether an agent notices a fact went stale without an explicit correction | Personal state; 400 scenarios of 50 sessions | Answer sessions given (M1); no extraction gold; CC-BY-4.0 |
| Memora (arXiv 2604.20006) [memora] | Remembering what should be kept and not repeating what was deleted or updated | 10 personas incl. a software engineer | Session types allow gold updates; three-model judge |
| MemoryAgentBench, Selective Forgetting [mab] | Later facts overriding earlier ones | Synthetic counterfactual triples | Substring match, no judge; BM25 48.0 vs Mem0 18.0 single-hop |
| Supersede (arXiv 2606.27472) [supersede] | Updating bounded notes | LongMemEval knowledge-update questions | No memory products tested; little beyond what recall runs |
| TEPA (arXiv 2608.07429) [tepa] | n/a | n/a | A revocable-memory method, no code or data |

HaluMem Medium, run by the paper authors through each product (docs; source for the README
table [halumem-repo]):

| System | Extraction recall | Target precision | F1 | Update correct | QA correct |
| --- | --- | --- | --- | --- | --- |
| MemOS (co-author's product) | 74.07 | 86.25 | 79.70 | 62.11 | 67.23 |
| Mem0-Graph | 43.28 | 87.20 | 57.85 | 24.50 | 54.66 |
| Mem0 | 42.91 | 86.26 | 57.31 | 25.50 | 53.02 |
| Supermemory | 41.53 | 90.32 | 56.90 | 16.37 | 54.07 |
| Memobase | 14.55 | 92.24 | 25.13 | 5.20 | 35.33 |
| Zep | no per-session memory API | | | 47.28 | 55.47 |

The pattern matches what recall's own memory evals look for: products keep precision high and
miss most facts (recall around 40%), and updates are where they fail.

## 4. recall on SWE-ContextBench Lite (measured)

Setup: recall 0.2.1 (`4c613c9`). Each of the 300 Lite transcripts is read by recall's own
Claude Code parser and indexed in memory; the query is the related task's issue text ("full",
median ~270 tokens) or its first line ("title"); a hit is any gold experience task in the top k.
"Same repo" searches only that repo's experience sessions, as an agent working in that project
would. Hybrid adds nomic-embed-text through Ollama at weight 1. n = 99, so one task is one
point and each score carries about ±10 points (95%); keyword search is deterministic. Measured
with a throwaway script; `recall eval swe-contextbench` (section 6) reproduces it.

| Query | Pool | Search | hit@1 | hit@3 | hit@5 | hit@10 | MRR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| full | all 300 | keyword | 0.273 | 0.424 | 0.434 | 0.465 | 0.346 |
| full | all 300 | hybrid | 0.354 | 0.444 | 0.465 | 0.737 | 0.437 |
| full | same repo | keyword | 0.343 | 0.455 | 0.495 | 0.556 | 0.406 |
| full | same repo | hybrid | 0.354 | 0.465 | 0.505 | 0.646 | 0.428 |
| title | all 300 | keyword | 0.293 | 0.374 | 0.414 | 0.465 | 0.345 |
| title | all 300 | hybrid | 0.313 | 0.424 | 0.485 | 0.727 | 0.409 |
| title | same repo | keyword | 0.323 | 0.414 | 0.525 | 0.626 | 0.403 |
| title | same repo | hybrid | 0.343 | 0.455 | 0.535 | 0.788 | 0.438 |

Against the paper's retrievers on the same 300 transcripts and 99 tasks ("Overall Matched",
independent, loosely comparable as section 2.1 explains): Mem0 39.4 (top 3), OpenViking 51.5
(top 3), Supermemory 59.6 (top 15), LangMem 73.3 (top 10 extracted memories, which may span
fewer than 10 sessions), the agent itself choosing among summaries 36.4 and among full
trajectories 18.2 [swecb-paper]. recall 0.2.1's hybrid hit@3 (0.444) falls between Mem0 and
OpenViking, and its hit@10 (0.737) matches LangMem. The others called an LLM at ingest (Mem0
$0.24 and 21 minutes, LangMem $0.98 and 58 minutes, OpenViking $18.96 and 570 minutes for the
300 transcripts); recall indexed them in under a second, plus about 35 seconds of local
embedding.

Why it is hard, and where to push:

- recall indexes what it indexes for real chats: text blocks only. A transcript averages 25,600
  tokens in the paper's count; recall keeps the task prompt and the agent's prose, about 4,100
  characters a session. The files the agent read and the edits it made are dropped.
- Issue texts are long queries, and recall's fusion was tuned on short questions.
- Misses are mostly ranking, not coverage: with hybrid in-repo search, hit@10 is 0.79 where
  hit@5 is 0.54.
- Some links are not about the same code at all (an issue that merely cites another), which
  caps any retriever below 100%.

## 5. Published numbers to compare against

Coding benchmarks, task success (all on small samples; see Summary point 3):

| Benchmark (model) | No memory | Products | Grade |
| --- | --- | --- | --- |
| SWE-ContextBench Lite, resolved % (Sonnet 4.5) [swecb-paper] | 26.26 | Mem0 24.24, OpenViking 29.20, Supermemory 30.30; oracle summary 34.34 | independent |
| SWE-ContextBench, 376 tasks, resolved %, no context vs oracle summary [swecb-paper] | Sonnet 4.5 19.68, GPT-5.3 Codex 22.60 | oracle summary 23.40 and 23.94 | independent |
| AMB, success [amb-status] | official-003 (deepseek-v4-flash, one seed per cell): CLAUDE.md 0.577, bare 0.659 | official-003: a memory layer 0.659 (RE-call, by inference), MemPalace 0.606, placebo 0.672. Separate runs joined to it in its STATUS.md (models not stated): Claude Mem 0.628, Cognee 0.612, Graphiti 0.606, Supermemory 0.248 | competitor |
| VibeMemBench, resolved % (deepseek-v4-pro) [vibemem] | 67.1 | Mem0 62.8, SimpleMem 66.9, MemoryOS 64.6, A-MEM 63.1 | independent |
| DreamBench-SWE, S3 pass % (GPT-5.5) [dreambench] | 11.7 (v2.1 audit) | primary fold: verbatim log 49.4, Mem0 (inference off) 11.1; v2.1 successor audit: verbatim memory 45.6, Mem0 (inference off) 53.9 | independent |
| MEMTRACK, correctness (GPT-5) [memtrack] | 0.601 | Mem0 0.610, Zep 0.601 | independent |
| AML Coding, taskSolve (locked model) [aml-coding] | none shown | MemoraX 62.0, claude-mem 52.0, MemOS 52.0, mem0-api 51.3, Cognee 50.7, SQLite-FTS baseline 50.0, Supermemory 49.3, MemPalace 49.3, Mem0 48.7, Hindsight Cloud 46.7 | mixed: entries marked "Evaluated" were run by the platform, "Submitted" by the entrant |

Memory and update benchmarks: HaluMem (section 3); STALE overall accuracy, all on GPT-4o-mini
and run by the authors: LightMem 17.8, Mem0 8.3, Zep 6.0, A-Mem 5.1 [stale]; MemoryAgentBench
Selective Forgetting, single-hop / multi-hop: BM25 48.0 / 3.0, MemGPT 28.0 / 3.0, Cognee 28.0 /
3.0, Mem0 18.0 / 2.0, Zep 7.0 / 3.0 [mab].

Token figures for M3: SWE-ContextBench reports 0.72-2.3 million tokens and $0.18-0.67 per task (oracle-summary rows at the low end)
across agents, and 4-5 minutes with a memory product against 6.4 without [swecb-paper]; AML
publishes input tokens per run (about 5-7 million for every entry) [aml-coding]; in one AMB pilot
the RE-call arm used 4.5 times the input tokens of the other arms [amb-repo].

## 6. Recommended suite

Ranked by fit to recall's job, then by what it takes to run on a laptop without Docker.
LongMemEval_S / _M and LoCoMo stay as no-regress guards for search.

**M1, find rate**

1. **SWE-ContextBench Lite, past-fix retrieval.** Real Claude Code transcripts over real repos,
   with the right session known; already measured (section 4) and far from ceiling. Needs a
   `recall eval swe-contextbench` command: read the transcripts with recall's Claude Code parser,
   query with each related issue, score hit@k and MRR by repo and over all 300, with a seeded
   train/test split. Reading the gold-link table needs `pyarrow` at eval time only. Minutes, $0.
   The 1,007-task full set, built as one issue-plus-patch session per experience task, can serve
   as a larger but less realistic guard.
2. **AMB corpus retrieval.** For each task and condition, is the planted session in the top 5,
   and is the superseded version ranked below the current one? Needs an importer for its session
   JSONL and the task-to-plant map from its repo. Minutes, $0. Small (26 tasks x 5 conditions).

**M2, answer or task success**

1. **AMB end to end.** Claude Code with recall's MCP server against its CLAUDE.md, bare and
   placebo arms, graded by its checkers. Needs Claude Code 2.1.221 or later run headless,
   several seeds per cell (its published run had one), and for a comparable run a Linux host
   with rootless Docker; on this Mac it would be a documented deviation. About $20-60 an arm.
2. **MemoryCode.** Code written with recall's tools or notes over the convention histories,
   regex-graded. No Docker, no judge, tens of dollars.

SWE-ContextBench end to end is worth one reported number if a Docker runtime that runs amd64
images (OrbStack or Colima with Rosetta) is installed: about 75-130 GB of image pulls for Lite,
one image on disk at a time, and roughly 600 agent runs for three arms and two seeds. At 99
tasks it cannot separate arms closer than about 8 points.

**M3, tokens per answer**

1. **From the AMB runs**: tokens per task per arm, as its harness records them.
2. **SWE-ContextBench, agent retrieval**: a headless agent with only recall's tools gets each
   related issue and names the past sessions it would rely on; score the gold session among them
   and count tokens. No Docker, a few dollars, and the closest public analogue of how agents call
   recall.

**M4, note quality**

1. **HaluMem Medium**: recall's extractor run session by session per user, its notes returned as
   strings, scored by the benchmark's judge. Comparable numbers need its GPT-4o judge (an OpenAI
   key, about $150-215 for Medium, a quarter of that for five users first); a Claude judge is
   cheaper but not comparable. The licence allows evaluation but not redistributing the data or
   anything derived from it.
2. **MemoryCode as an extraction test**: after each history, do recall's notes hold every
   convention in its latest form and none of the replaced ones? Gold comes from the session
   labels; scoring can be exact-match on the convention text plus a claim judge.

Secret leaks have no public benchmark; they stay with recall's own memory evals.

## References

- [agentsmd]: https://arxiv.org/html/2602.11988
- [aml-coding]: https://agentmemoryleaderboard.ai/static/coding-data.js
- [aml-docs]: https://agentmemoryleaderboard.ai/docs
- [amb-corpus]: https://huggingface.co/datasets/Gde05/agent-memory-bench-corpus
- [amb-exec]: https://github.com/GiulioDER/agent-memory-bench/blob/c1b15726f1153b02dda0490ca1936bb86a809514/docs/SECURE_EXECUTION.md
- [amb-repo]: https://github.com/GiulioDER/agent-memory-bench/tree/c1b15726f1153b02dda0490ca1936bb86a809514
- [amb-space]: https://huggingface.co/spaces/Gde05/agent-memory-bench
- [amb-status]: https://github.com/GiulioDER/agent-memory-bench/blob/c1b15726f1153b02dda0490ca1936bb86a809514/docs/STATUS.md
- [context-bench]: https://www.letta.com/blog/context-bench
- [dreambench]: https://arxiv.org/html/2608.20664v1
- [halumem]: https://arxiv.org/html/2511.03506v3
- [halumem-repo]: https://github.com/MemTensor/HaluMem
- [lme-v2]: https://arxiv.org/html/2605.12493v1
- [locobench-agent]: https://arxiv.org/abs/2511.13998
- [mab]: https://arxiv.org/html/2507.05257v4
- [memora]: https://github.com/geniesinc/Memora
- [memorycode]: https://arxiv.org/html/2502.13791v2
- [memorycode-data]: https://huggingface.co/datasets/CohereLabsCommunity/memorycode
- [memtrack]: https://arxiv.org/html/2510.01353v1
- [recall-rag]: https://pypi.org/project/recall-rag/
- [stale]: https://arxiv.org/html/2605.06527v1
- [supersede]: https://arxiv.org/abs/2606.27472
- [swebench-cl]: https://arxiv.org/abs/2507.00014
- [swecb-data]: https://huggingface.co/datasets/jiayuanz3/SWEContextBench/tree/12c65bd15e2559bc808065565e941ee7bbbd008f
- [swecb-images]: https://hub.docker.com/r/jiayuanz3/swecontextbench/tags
- [swecb-paper]: https://arxiv.org/html/2602.08316v3
- [swecb-repo]: https://github.com/jiayuanz3/SWEContextBench/tree/12ad6ab14e18e9378e1e293c9edbc3f7ce43d27b
- [tepa]: https://arxiv.org/abs/2608.07429
- [vibemem]: https://arxiv.org/html/2609.23570v1
