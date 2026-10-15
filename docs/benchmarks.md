# Benchmarks

The README gives the headline figures. This page has every result, how it was measured, and the
commands that reproduce it.

A coding agent needs three things from memory: to find the session where a problem was solved
before, to bring back only what it needs from it, and to start each session knowing the
conventions you set, in their latest form. recall is measured on public benchmarks built from
coding work for each of the three. Every number here is reproducible with `recall eval`; token
counts come from the runs' transcripts via
[docs/benchmarks/agent_tokens.py](benchmarks/agent_tokens.py)
([commands](#reproducing-the-numbers)).

- **Finds the past fix for a new bug report in its top five results 80% of the time**, up
  from 47% in 0.2.1, and in its top ten 93% of the time (SWE-ContextBench Lite: 99 real GitHub
  issues searched against all 300 real Claude Code sessions; hybrid search, deterministic, one
  run). The memory products the benchmark's authors tested found it 39–73% of the time, each at
  its own cutoff, after running an LLM over every session; at each of those cutoffs 0.3.0 is
  ahead. recall runs no LLM to index.
- **37% fewer tokens added to an agent's context per question than 0.2.1**: about 9.0k
  estimated Claude tokens against 14.4k on the 40 held-out SWE-ContextBench issues, counting
  recall's ~2.5k of tool definitions, with the same share of past fixes found (90%).
- **Notes hold 62% of the coding conventions you set, in their latest form**, up from 9%, and
  record 54% of changed conventions correctly, up from 5% (MemoryCode, 40 held-out histories,
  three runs each, notes written by gpt-6-luna).
- **The notes reach the agent.** With 0.3.0's notes loaded at session start, Claude Haiku 5.5
  follows the latest conventions on long MemoryCode histories (20 to 100 sessions) 62% of the
  time, against 40% with search alone (held-out histories, three runs each). Rereading the
  whole history gets 80% on the training histories.
- **No LLM calls to index or to search.** recall indexes on your machine (SQLite, plus an
  optional local embedding model). On my ~2,800 chats a search takes about 15 ms with
  keywords, or 50 ms with embeddings, including embedding the query (median of 50 searches, on
  a laptop busy with other evals, so an upper bound).
  Only the nightly notes need a language model, and you choose which.

Re-checked for 0.4.0: search is unchanged. The model-free runs on 0.4.0's code gave the same
figures as before (SWE-ContextBench Lite, below; LongMemEval_S and LoCoMo, keywords only: 97.4%
and 87.2%, 89.6% and 77.2%). The figures labelled 0.3.0 are those releases' runs; the notes
and agent results were not re-measured for 0.4.0, except where the new section says so.

## Finding past work

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/swecb-dark.svg"><img src="benchmarks/swecb-light.svg" alt="Bar chart of SWE-ContextBench Lite past-fix retrieval over all 300 sessions, each memory product at the cutoff its paper reports and recall at the same cutoff. Top 3: recall 0.3.0 76.8%, recall 0.2.1 44.4%, OpenViking 51.5%, Mem0 39.4%. Top 10: recall 0.3.0 92.9%, recall 0.2.1 73.7%, LangMem 73.3%. Top 15: recall 0.3.0 97.0%, recall 0.2.1 86.9%, Supermemory 59.6%" width="760"></picture></p>

SWE-ContextBench pairs GitHub issues with an earlier issue in the same repo whose fix helps, and
its Lite set ships the 300 Claude Code sessions in which those earlier fixes were made. recall
indexes the sessions as it would your own chats, and searches all 300 sessions, as the products
did, with the new issue's full text.

| SWE-ContextBench Lite, all 300 sessions: past fix in the top k | Top 1 | Top 3 | Top 5 | Top 10 | Top 15 | To take in the 300 sessions |
| --- | --- | --- | --- | --- | --- | --- |
| **recall 0.3.0** | **58.6%** | **76.8%** | **79.8%** | **92.9%** | **97.0%** | **no LLM calls** |
| recall 0.3.0, keywords only | 53.5% | 68.7% | 77.8% | 86.9% | 90.9% | no LLM calls |
| recall 0.2.1 | 35.4% | 44.4% | 46.5% | 73.7% | 86.9% | no LLM calls |
| LangMem | | | | 73.3% | | LLM calls: $0.98, 58 minutes |
| Supermemory | | | | | 59.6% | LLM calls: 370k tokens, under an hour |
| OpenViking | | 51.5% | | | | LLM calls: $18.96, 570 minutes |
| Mem0 | | 39.4% | | | | LLM calls: $0.24, 21 minutes |

**How to read this.** recall's rows use hybrid search unless marked, over all 99 issues; search
is deterministic, so each is one run. The products' figures are the benchmark paper's own runs
("Overall Matched", its Table 5), each at the cutoff the paper reports. The paper does not define
that metric, and it describes the query for Mem0 and Supermemory but not for OpenViking and
LangMem; LangMem's ten results are extracted memories, not sessions. So compare loosely: 0.2.1
already matched LangMem at top 10 and beat Supermemory at top 15, and 0.3.0 leads at every
cutoff. Searching only the issue's own repo, as an agent working in that project would, puts
the past fix in 0.3.0's top five 82.8% of the time (0.2.1: 50.5%). The ranking change behind
the gain was chosen on 59 of the 99 issues; on the other 40, held out, the repo-scoped top-five
rate rose from 50.0% to 92.5%. With 99 issues, each figure carries about ±10 points. Method and
the benchmark's quirks:
[docs/research/coding-memory-hillclimb.md](research/coding-memory-hillclimb.md) and
[docs/research/coding-memory-benchmarks.md](research/coding-memory-benchmarks.md).

## Tokens per answer

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/agent-tokens-dark.svg"><img src="benchmarks/agent-tokens-light.svg" alt="Bar chart of the context added to an agent per question, in Claude tokens including recall's tool definitions. SWE-ContextBench Lite, 40 held-out issues: recall 0.2.1 14.4k (5 runs), recall 0.3.0 9.0k (1 run). My questions about past work, 47 held out: recall 0.2.1 19.1k, recall 0.3.0 16.0k (3 runs each)" width="760"></picture></p>

recall does not hand an agent a fixed block of context: the agent searches and reads what it
needs, and what matters is how much that adds. Claude Haiku 5.5, running in Claude Code with
only recall's tools, took in about 9.0k Claude tokens per held-out SWE-ContextBench issue with
0.3.0 (the release run) against 14.4k with 0.2.1 (five runs), and named the past fix for 90% of
them both times. The 0.2.1 runs used 0.2.1's tools with the first version of the new title fade,
which almost never touches the short queries agents send (under 1% of the agents' searches in
these runs were over 12 words), so the difference is what the tools return. On 47 held-out
questions about my own past work (three runs each), it took in 16.0k tokens a question against
19.1k, and answered 85.1% correctly both times. The counts are estimates in Claude tokens (word
pieces of tool output times a factor fitted to Claude's own per-call counts, R² of 0.98 or
better) and include recall's tool definitions, measured directly (2,591 tokens in 0.2.1, 2,480
in 0.3.0). Search results now leave out fields that repeat others, one search can try several
phrasings at once, and `read_chat` clips a long message to its start, the passage that matches,
and its end.

Memory products report the context they give their answer model on chat benchmarks: Mem0 6,956
tokens a query on LoCoMo (its own figure), EverMemOS 2.8k (its paper's token column, which it
does not define) and Zep 1.6k on LongMemEval. Those are other benchmarks and setups, so they
place recall's figure rather than rank it.

## Notes that keep up

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/notes-dark.svg"><img src="benchmarks/notes-light.svg" alt="Grouped bar chart of note quality, recall 0.2.1 against recall 0.3.0. MemoryCode, 40 held-out histories, 3 runs: conventions in notes in their latest form 9% to 62%, changed conventions recorded 5% to 54%. A fresh holdout of my chats, 6 runs: expected memories found 48% to 61%, memories worth keeping 83% to 88%" width="760"></picture></p>

MemoryCode gives a model long histories in which a mentor sets coding conventions ("start
function names with `y_`"), changes some of them later, and talks about other things in
between. After each history, recall's notes are checked against the benchmark's labels: is
every convention there in its latest form, and is a replaced form still stated as current? On
40 held-out histories, three runs each, with notes written by gpt-6-luna through a ChatGPT plan:

| MemoryCode: notes after each history | recall 0.2.1 | recall 0.3.0 |
| --- | --- | --- |
| Conventions held in their latest form | 9.1% | **61.6%** |
| Changed conventions recorded correctly | 4.6% | **53.6%** |
| A replaced form still stated as current (lower is better) | 2.7% | 8.4% |
| Notes learned only from unrelated talk (lower is better) | 45% | 30% |
| Model calls, three runs | 780 | 1,927 |

The extractor now answers chat by chat (handed a dozen chats and one list to fill, a model reads
the first few), and decides first whether a memory outlasts the task at hand. More notes means
more to keep current: 8% of replaced forms are still stated as current.

I also checked it on my own chats, labelled by hand. The change was first judged on a test set
of my chats that by then had informed several decisions, and there it did not help: recall was
flat (44% to 45%) and precision fell (98% to 92%). So the decision was made once, on a fresh
holdout of my chats (six runs: expected memories found 48% to 61%, and 88% of what the notes
store worth keeping, against 83%) together with MemoryCode's held-out histories above.

More candidates also mean more consolidation calls. On MemoryCode, where nearly every session
sets a rule, the three runs took 1,927 model calls instead of 780 (input 34.0M tokens instead of
14.3M, output 608k instead of 135k); on my holdout chats, three runs took 40 calls instead of 37,
with 1.4 times the output. The new extraction prompt (v4) makes the nightly run read past chats
again with it, newest first, within the nightly call budget (80 by default), so a large history
takes several nights.

**Do the notes reach the agent?** The same benchmark ends each history with coding requests, and
checks the code against the latest conventions. Claude Haiku 5.5 wrote it in Claude Code with
recall's search alone, or with 0.3.0's notes loaded at session start the way recall's hook loads
them, plus search. Claude Code loaded recall's tools as it does by default, through tool search:

| MemoryCode: code that follows the latest conventions | All histories | 20 to 100 sessions | 1 to 15 sessions |
| --- | --- | --- | --- |
| No memory (held out, three runs) | 2.4% | | |
| Search alone (held out, three runs) | 73.2% | 39.5% | 93.5% |
| **0.3.0's notes at session start, plus search (held out, three runs)** | **77.5%** | **61.9%** | 86.8% |
| The whole history in the prompt (training histories, one run) | 90% | 80% | 96% |

On short histories the notes did worse, 86.8% against 93.5%: with the notes in hand, the agent
reads fewer chats. The 0.3.0 release run loaded recall's tools up front instead, and gave 79.7%
(64.8% on long histories, 88.6% on short) with notes, one run, against 75.8% (45.3%, 94.2%)
with search alone, three runs; with the notes the agent took in about a third fewer input
tokens. The session-start block has changed since both runs, and 0.2.1's notes at session start
were not measured.

## Which model writes your notes

The notes are written by whichever model you pick, and the choice matters. MemoryCode's test
split (40 histories), through the same extraction and consolidation pipeline, scored by the share
of facts the notes hold:

| Model that writes the notes | Facts in the notes |
| --- | --- |
| gpt-6-luna (Codex) | 59% to 68% across runs |
| Claude Haiku 4.5 (what `claude:haiku` ran before 0.4.0) | 80.9% |
| Claude Haiku 5.5, API | 94.6% |
| Claude Haiku 5.5, Claude subscription CLI (one run) | 97.6% |

Sonnet 5.5 for consolidation added nothing over Haiku 5.5 alone (-2 points, within run noise),
and packing 8 chats to a call instead of 12 made no difference. So `claude:haiku` on a Claude
subscription, or Haiku 5.5 on an API key, is the better start; 0.4.0's Claude provider now runs
the 5.5 models. These are one benchmark's synthetic coding histories, and the Haiku 5.5 CLI
figure is a single run.

The consolidation change in 0.4.0 (notes hold rules, changed one at a time) was measured on a
consolidation-only replay of the same extracted facts: facts lost when notes merge fell from
12.5% to 8.1%, recall rose from 70.5% to 75.3%, and stale notes fell from 9.3% to 4.8%.

## Chat-memory benchmarks

LongMemEval and LoCoMo are the benchmarks memory products usually report. They are built from
synthetic personal-assistant chats rather than coding work, so recall runs them as a check that
its search stays sound, with each benchmark's own metrics and prompts. In the 0.3.0 release run
their keyword retrieval was identical to 0.2.1's (and 0.4.0's is the same), and the title change cannot reach them: their
chats have no titles. Retrieval is deterministic (one run); each answer-accuracy figure is one
run.

- **Finds a conversation that answers the question 98.7% of the time, and every one that does
  90.6% of the time** (top five, LongMemEval_S, 470 questions): among the highest session-retrieval
  scores published for the benchmark.
- **87.4% of answers correct with Claude Haiku 5.5** across all 500 questions; handed exactly
  the right sessions, the same model scores 92.0%. Reading recall's top 20 chats, it reaches
  90.5% for under a cent a question, on a 200-question sample where the right sessions alone
  score 91.0%. Answer accuracy was last measured on 9 October, before 0.3.0.

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/accuracy-dark.svg"><img src="benchmarks/accuracy-light.svg" alt="Bar chart of LongMemEval_S answer accuracy: Mastra 94.9%, Mem0 94.4%, Hindsight 91.4%, Honcho 90.4%, recall 87.4%, Supermemory 85.2%, EverMemOS 83.0%, ICM folders 74.4%, Zep 71.2%, full history with no memory (Honcho's run with Haiku 4.5) 62.6%; a dashed line marks 92%, Haiku 5.5 given only the answer sessions" width="760"></picture></p>

| Session retrieval | Answer session in top 5 | Every answer session in top 5 |
| --- | --- | --- |
| recall, LongMemEval_S (470 questions) | **98.7%** | **90.6%** |
| recall, LongMemEval_S, keywords only | 97.4% | 87.2% |
| recall, LongMemEval_M (~475 sessions a question), keywords only | 90.9% | 72.1% |
| LongMemEval paper's best setup, LongMemEval_M | | 73.2% |
| recall, LoCoMo (1,536 questions) | **92.1%** | **80.1%** |
| AgentIR, LoCoMo (BM25 / hybrid) | 87.5% / 79.7% | |
| memini, LongMemEval (BM25 / hybrid) | 97.6% / 98.4% | |

The other systems' figures are their own, measured with their own answer models and judges; a
one-harness rerun of five other memory products (none of those charted here) moved their scores
by +3 to −28 points. recall's
runs use Claude Haiku 5.5 as the judge, with the benchmark's prompts. Answer models, context
read per question, costs, question types and confidence intervals:
[docs/research/retrieval-benchmarks.md](research/retrieval-benchmarks.md).

## Reproducing the numbers

Run these from a clone of this repo. The benchmark readers need `pyarrow`, which recall does not
install; `--hybrid` needs an `[embeddings]` table in config.toml; and the agent evals run Claude
Code headless with only recall's tools. Where to get each dataset:
[docs/research/coding-memory-hillclimb.md](research/coding-memory-hillclimb.md#reproducing-it).

```bash
uv run --with pyarrow recall eval swe-contextbench --data DIR --scope all --hybrid --split all   # past fixes found
uv run --with pyarrow recall eval swe-contextbench --data DIR --hybrid --agent --split test      # an agent finding them
uv run --with pyarrow recall eval memorycode --data DIR --split test --limit 40 --repeats 3 --fresh
uv run --with pyarrow recall eval memorycode --data DIR --agent --arm hook --notes RUN_DIR --hybrid --split test
uv run recall eval amb --data DIR                                                                 # agent-memory-bench retrieval
uv run recall eval longmemeval --cases longmemeval_s_cleaned.json --hybrid [--qa claude:claude-haiku-5-5]
uv run recall eval locomo --cases locomo10.json --hybrid
python docs/benchmarks/agent_tokens.py DEFS_BEFORE DEFS_AFTER before:RUN_DIR ... after:RUN_DIR ...   # Claude tokens per question
```

Each run writes a folder with every case's transcript, the scores with bootstrap confidence
intervals, and a results page; `--against RUN` adds a paired comparison with an earlier run.

## On your own chats

There are no unit tests by design. recall is checked with evals that drive the real
pipeline, plus an end-to-end smoke test in CI. The public benchmarks and the commands that
reproduce them are above. These run on your own chats:

```bash
recall eval search                 # "find that chat" queries generated from your chats
recall eval memory --fresh         # extraction against hand-labelled chats; average several runs
recall eval search --v2            # your questions, and the chats that answer them
recall eval agent                  # an agent answers your questions about past work with recall's tools
recall eval memory --v2            # your chats, and the memories they should and should not yield
```

The `--v2` evals and `recall eval agent` read cases you write, from `~/.agents/recall/evals/v2/`
(or `RECALL_EVAL_CASES`), split into train and test (`--split`) so that what you tune on is not
what you judge by. recall ships no cases, since they name your chats. Answers and memories are
graded against checkable claims by Claude Sonnet 5.5, which must differ from the model under
test.

On my ~2,800 chats with 0.3.0: search finds the intended chat in the top five for 86.7% of 60
held-out hand-written questions (hybrid, one deterministic run; 92.2% of the 90 training
questions); Claude Haiku 5.5 with recall's tools answers 85.1% of 47 held-out questions about
past work correctly (three runs); and notes written by gpt-6-luna find 61% of the memories in a
fresh hand-labelled holdout, 88% of what they store worth keeping (six runs). Model-based evals
vary from run to run, so compare averages of several.
