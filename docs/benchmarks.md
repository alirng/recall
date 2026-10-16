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
  its own cutoff, after running an LLM over every session; at each of those cutoffs 0.4.1 is
  ahead. recall runs no LLM to index.
- **About 7.8k tokens added to an agent's context per question**, counting recall's ~3.3k of
  tool definitions, on the 40 held-out SWE-ContextBench issues, with the past fix named 87% of
  the time. That is 7% more than 0.3.0 run the same day (7.3k, 84% found): what the tools
  return is about the same, and the extra is the definitions of the tools 0.4.0 added.
- **Notes hold 91% of the coding conventions you set, in their latest form**, and record 90% of
  changed conventions correctly, when Claude Haiku 5.5 writes them (MemoryCode, 40 held-out
  histories, three runs each). Written by gpt-6-luna they hold 67%, up from 62% in 0.3.0. On
  my own hand-labelled chats the two models were within noise of each other.
- **The notes reach the agent.** With those notes loaded at session start, Claude Haiku 5.5
  follows the latest conventions on long MemoryCode histories (20 to 100 sessions) 75% of the
  time, against 32% with search alone, and takes in 41% fewer tokens (both on 0.4.1, the same
  day, three runs each). Rereading the whole history got 80% on long training histories.
- **No LLM calls to index or to search.** recall indexes on your machine (SQLite, plus an
  optional local embedding model). On my ~2,800 chats a search takes about 11 ms with
  keywords, or 47 ms with embeddings, including embedding the query (0.4.1, median of 50
  searches, run twice, on a laptop).
  Only the nightly notes need a language model, and you choose which.

Re-checked for 0.4.1: search is unchanged. Every model-free run on 0.4.1's code gave the same
figures as 0.3.0 and 0.4.0 (SWE-ContextBench Lite in both scopes, LongMemEval_S and _M, LoCoMo,
and search over my chats' 60 held-out questions), so those figures carry 0.4.1's label. Each
figure names the release that measured it, and compares with the last release that scored
differently. 0.4.2 fixes an MCP server bug in 0.4.0 and 0.4.1 that, on a machine with two or
more Cursor builds, left the index locked and made many of an agent's calls to recall fail; the
agent figures on my own chats are 0.4.2's. Its re-runs of search, SWE-ContextBench and
LongMemEval retrieval match 0.4.1.

## Finding past work

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/swecb-dark.svg"><img src="benchmarks/swecb-light.svg" alt="Bar chart of SWE-ContextBench Lite past-fix retrieval over all 300 sessions, each memory product at the cutoff its paper reports and recall at the same cutoff. Top 3: recall 0.4.1 76.8%, recall 0.2.1 44.4%, OpenViking 51.5%, Mem0 39.4%. Top 10: recall 0.4.1 92.9%, recall 0.2.1 73.7%, LangMem 73.3%. Top 15: recall 0.4.1 97.0%, recall 0.2.1 86.9%, Supermemory 59.6%" width="760"></picture></p>

SWE-ContextBench pairs GitHub issues with an earlier issue in the same repo whose fix helps, and
its Lite set ships the 300 Claude Code sessions in which those earlier fixes were made. recall
indexes the sessions as it would your own chats, and searches all 300 sessions, as the products
did, with the new issue's full text.

| SWE-ContextBench Lite, all 300 sessions: past fix in the top k | Top 1 | Top 3 | Top 5 | Top 10 | Top 15 | To take in the 300 sessions |
| --- | --- | --- | --- | --- | --- | --- |
| **recall 0.4.1** | **58.6%** | **76.8%** | **79.8%** | **92.9%** | **97.0%** | **no LLM calls** |
| recall 0.4.1, keywords only | 53.5% | 68.7% | 77.8% | 86.9% | 90.9% | no LLM calls |
| recall 0.2.1 | 35.4% | 44.4% | 46.5% | 73.7% | 86.9% | no LLM calls |
| LangMem | | | | 73.3% | | LLM calls: $0.98, 58 minutes |
| Supermemory | | | | | 59.6% | LLM calls: 370k tokens, under an hour |
| OpenViking | | 51.5% | | | | LLM calls: $18.96, 570 minutes |
| Mem0 | | 39.4% | | | | LLM calls: $0.24, 21 minutes |

**How to read this.** recall's rows use hybrid search unless marked, over all 99 issues; search
is deterministic, so each is one run. The products' figures are the benchmark paper's own runs
("Overall Matched", its Table 5), each at the cutoff the paper reports. The paper does not define
that metric, and it describes the query for Mem0 and Supermemory but not for OpenViking and
LangMem; LangMem's ten results are extracted memories, not sessions. So compare loosely: 0.2.1,
the last release that scored differently, already matched LangMem at top 10 and beat
Supermemory at top 15, and 0.4.1 leads at every cutoff, as 0.3.0 did. Searching only the
issue's own repo, as an agent working in that project would, puts the past fix in 0.4.1's top
five 82.8% of the time (0.2.1: 50.5%). The ranking change behind the gain shipped in 0.3.0 and
was chosen on 59 of the 99 issues; on the other 40, held out, the repo-scoped top-five
rate rose from 50.0% to 92.5%. With 99 issues, each figure carries about ±10 points. Method and
the benchmark's quirks:
[docs/research/coding-memory-hillclimb.md](research/coding-memory-hillclimb.md) and
[docs/research/coding-memory-benchmarks.md](research/coding-memory-benchmarks.md).

## Tokens per answer

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/agent-tokens-dark.svg"><img src="benchmarks/agent-tokens-light.svg" alt="Bar chart of the context added to an agent per question, in Claude tokens including recall's tool definitions. both releases run on 16 October, 3 runs each. SWE-ContextBench Lite, 40 held-out issues: recall 0.4.1 7.8k, recall 0.3.0 7.3k. My questions about past work, 47 held out: recall 0.4.2 13.6k, recall 0.3.0 13.4k" width="760"></picture></p>

recall does not hand an agent a fixed block of context: the agent searches and reads what it
needs, and what matters is how much that adds. Claude Haiku 5.5, running in Claude Code with
only recall's tools, loaded up front, took in about 7.8k Claude tokens per held-out
SWE-ContextBench issue with 0.4.1 and 7.3k with 0.3.0's code run the same day (three runs each),
and named the past fix for 87% and 84% of them (within run noise). What the tools returned was
about the same, 4.6k tokens an issue against 4.8k; the difference is the tool definitions, 3,260
tokens against 2,480, because 0.4.0 added tools. On 47 held-out questions about my own past work,
also the same day, 0.4.2 took in 13.6k tokens a question and 0.3.0's code 13.4k, and they
answered 84.4% and 86.5% correctly (within run noise). 0.4.1 is not shown there: in its run most
questions hit an MCP server bug, fixed in 0.4.2, that left the index locked, so recall's tools
returned errors instead of results.

Compare these figures only within a day. On 12 October, 0.3.0's release runs took in 9.0k and
16.0k tokens; on 16 October the same code took in 7.3k and 13.4k, because the agent called the
same tools less. Something on the model's side changed in between, as with MemoryCode below.

The counts are estimates in Claude tokens (word pieces of tool output times a factor fitted to
Claude's own per-call counts, R² of 0.98 or better) and include recall's tool definitions,
measured directly as the first call's input with recall connected minus without (2,591 tokens in
0.2.1, 2,480 in 0.3.0, 3,260 in 0.4.1 and 0.4.2). The factor is fitted over the runs compared,
so the releases in one comparison share it.

Memory products report the context they give their answer model on chat benchmarks: Mem0 6,956
tokens a query on LoCoMo (its own figure), EverMemOS 2.8k (its paper's token column, which it
does not define) and Zep 1.6k on LongMemEval. Those are other benchmarks and setups, so they
place recall's figure rather than rank it.

## Notes that keep up

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/notes-dark.svg"><img src="benchmarks/notes-light.svg" alt="Grouped bar chart of MemoryCode note quality on 40 held-out histories, 3 runs each. Conventions in notes in their latest form: recall 0.3.0 with gpt-6-luna 61.6%, recall 0.4.1 with gpt-6-luna 66.6%, recall 0.4.1 with Claude Haiku 5.5 90.6%. Changed conventions recorded: 53.6%, 61.7%, 90.0%" width="760"></picture></p>

MemoryCode gives a model long histories in which a mentor sets coding conventions ("start
function names with `y_`"), changes some of them later, and talks about other things in
between. After each history, recall's notes are checked against the benchmark's labels: is
every convention there in its latest form, and is a replaced form still stated as current? On
40 held-out histories, three fresh runs each:

| MemoryCode: notes after each history | 0.3.0, gpt-6-luna | 0.4.1, gpt-6-luna | **0.4.1, Claude Haiku 5.5** |
| --- | --- | --- | --- |
| Conventions held in their latest form | 61.6% | 66.6% | **90.6%** |
| Changed conventions recorded correctly | 53.6% | 61.7% | **90.0%** |
| A replaced form still stated as current (lower is better) | 8.4% | 6.0% | **1.7%** |
| Notes learned only from unrelated talk (lower is better) | 30% | 23% | **15%** |
| Model calls, three runs | 1,927 | 1,926 | 2,305 |

With the same model, 0.4.1 moves every measure the right way against 0.3.0, history by history.
Fewer notes come from unrelated talk (-6.2 points, 95% CI -8.8 to -3.3); the other changes are
within run noise (conventions held: +5.0 points, -0.7 to +10.9). 0.3.0's run is from 12 October
and 0.4.1's from 16 October, and a model's behaviour can shift between days (see the agent
results below), so read this comparison loosely.
Since 0.4.0 a note holds its rules one by one and consolidation changes only the one that
changed; replaying the same extracted facts through each release's consolidation, facts lost
when notes merge fell from 12.5% to 8.1% and stale notes from 9.3% to 4.8%. The bigger lever is
the model: Haiku 5.5 holds 24 points more of the conventions than gpt-6-luna on the same
histories, and states a replaced one as current a quarter as often
([which model writes your notes](#which-model-writes-your-notes)). Its three runs read 10.1M
tokens and wrote 2.5M, about $2.30 at Haiku 5.5's list price, or half that through the batch API
that recall uses with an API key.

I also check the notes on my own chats, against memories I labelled by hand: a holdout of 20
cases that no change has been tuned on. There the order between the models flips. On 0.4.1,
three runs each, notes written by gpt-6-luna find 69% of the expected memories, and 88% of what
they store is worth keeping (0.3.0: 61% and 88%, six runs); notes written by Haiku 5.5 find 57%,
and 83% is worth keeping. With 14 scorable cases that difference is within noise (-12.6 points,
95% CI -26.0 to 0.0), and Haiku's three runs spread from 43% to 68%, but MemoryCode's lead for
Haiku does not show on real chats. Neither model stored anything the labels forbid.

**Do the notes reach the agent?** The same benchmark ends each history with coding requests, and
checks the code against the latest conventions. Claude Haiku 5.5 wrote it in Claude Code with
recall's search alone, or with Haiku 5.5's notes loaded at session start the way recall's hook
loads them, plus search. Claude Code loaded recall's tools as it does by default, through tool
search. Both arms ran on 0.4.1 on the same day, three runs each, on the 40 held-out histories:

| MemoryCode, 0.4.1: code that follows the latest conventions | All histories | 20 to 100 sessions | 1 to 15 sessions | Input tokens a request |
| --- | --- | --- | --- | --- |
| Search alone | 67.0% | 32.3% | 87.8% | 61k |
| **Notes at session start, plus search** | **83.3%** | **75.0%** | 88.3% | **36k** |

With the notes in hand the agent follows the latest conventions on long histories 75% of the
time against 32% (every one of the 15 long histories improved), holds steady on short ones, and
makes 2.2 calls to recall a request instead of 7.3, so it takes in 41% fewer tokens. For scale,
measured on 12 October on the training histories, Haiku 5.5 with no memory got 2.1% right, and
with the whole history in its prompt 90% (80% on long histories).

Compare these arms only within a day. Search alone scored 73.2% (39.5% on long histories) when
measured on 13 October; the same code, re-run on 16 October, scores 66.8%, the same as 0.4.1, so
the model or the service behind it changed in between. On 13 October, 0.3.0's notes written by
gpt-6-luna lifted long histories from 39.5% to 61.9%.

## Which model writes your notes

The notes are written by whichever model you pick, and the choice matters. On 0.4.1, the same
40 held-out MemoryCode histories, three fresh runs each, through the same extraction and
consolidation pipeline:

| MemoryCode, 0.4.1: notes after each history | gpt-6-luna (Codex) | **Claude Haiku 5.5 (API)** |
| --- | --- | --- |
| Conventions held in their latest form | 66.6% | **90.6%** |
| Changed conventions recorded correctly | 61.7% | **90.0%** |
| A replaced form still stated as current (lower is better) | 6.0% | **1.7%** |
| Notes learned only from unrelated talk (lower is better) | 23.3% | **15.2%** |
| Model calls, three runs | 1,926 | 2,305 |
| Tokens in / out, three runs | 34.9M / 628k | 10.1M / 2.5M |

Haiku 5.5 is ahead on every measure, history by history (conventions held: +24.0 points, 95% CI
+16.7 to +29.8). Earlier runs on a different set of 40 test histories, before 0.4.0, put the
same pattern in order: gpt-6-luna 59% to 68%, Claude Haiku 4.5 (what `claude:haiku` ran before
0.4.0) 80.9%, Haiku 5.5 through the API 94.6%, and through the Claude subscription CLI 97.6%
(one run). Sonnet 5.5 for consolidation added nothing over Haiku 5.5 alone (-2 points, within
run noise), and packing 8 chats to a call instead of 12 made no difference. So `claude:haiku` on
a Claude subscription, or Haiku 5.5 on an API key, is the better start; since 0.4.0 the Claude
provider runs the 5.5 models. These are one benchmark's synthetic coding histories: on my own
hand-labelled chats gpt-6-luna leaned ahead (69% of expected memories against 57%, within noise;
[above](#notes-that-keep-up)), so check the notes your model writes on your own history.

The consolidation change in 0.4.0 (notes hold rules, changed one at a time) was measured on a
consolidation-only replay of the same extracted facts: facts lost when notes merge fell from
12.5% to 8.1%, recall rose from 70.5% to 75.3%, and stale notes fell from 9.3% to 4.8%.

## Chat-memory benchmarks

LongMemEval and LoCoMo are the benchmarks memory products usually report. They are built from
synthetic personal-assistant chats rather than coding work, so recall runs them as a check that
its search stays sound, with each benchmark's own metrics and prompts. Their retrieval on 0.4.1
is identical to 0.2.0's, where it was first measured, and the title change cannot reach them:
their chats have no titles. Retrieval is deterministic (one run); each answer-accuracy figure is
one run.

- **Finds a conversation that answers the question 98.7% of the time, and every one that does
  90.6% of the time** (top five, LongMemEval_S, 470 questions): among the highest session-retrieval
  scores published for the benchmark.
- **88.4% of answers correct with Claude Haiku 5.5** across all 500 questions on 0.4.1, reading
  recall's top 10 chats (about 44k tokens a question; 87.4% when first measured on 0.2.0).
  Reading only matched windows of the top 5 chats, it scores 84.2% on about 17.5k tokens. Handed
  exactly the right sessions, the same model scored 92.0% (measured on 9 October; that figure
  does not depend on recall). On 0.2.0, reading the top 20 chats reached 90.5% for under a cent
  a question, on a 200-question sample where the right sessions alone scored 91.0%.

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="benchmarks/accuracy-dark.svg"><img src="benchmarks/accuracy-light.svg" alt="Bar chart of LongMemEval_S answer accuracy: Mastra 94.9%, Mem0 94.4%, Hindsight 91.4%, Honcho 90.4%, recall 0.4.1 88.4%, Supermemory 85.2%, EverMemOS 83.0%, ICM folders 74.4%, Zep 71.2%, full history with no memory (Honcho's run with Haiku 4.5) 62.6%; a dashed line marks 92%, Haiku 5.5 given only the answer sessions" width="760"></picture></p>

| Session retrieval | Answer session in top 5 | Every answer session in top 5 |
| --- | --- | --- |
| recall 0.4.1, LongMemEval_S (470 questions) | **98.7%** | **90.6%** |
| recall 0.4.1, LongMemEval_S, keywords only | 97.4% | 87.2% |
| recall 0.4.1, LongMemEval_M (~475 sessions a question), keywords only | 90.9% | 72.1% |
| LongMemEval paper's best setup, LongMemEval_M | | 73.2% |
| recall 0.4.1, LoCoMo (1,536 questions) | **92.1%** | **80.1%** |
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
python docs/benchmarks/agent_tokens.py 0.3.0=2480 RUN_DIR ... 0.4.1=3260 RUN_DIR ...   # Claude tokens per question, one release per group
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

On my ~2,800 chats: on 0.4.1, search finds the intended chat in the top five for 86.7% of 60
held-out hand-written questions, as 0.3.0 did, and 91.1% of the 90 training questions (0.3.0:
92.2%; hybrid, one deterministic run each). Claude Haiku 5.5 with recall's tools answers 84.4%
of 47 held-out questions about past work correctly on 0.4.2 (three runs; 0.3.0's code, run the
same day, 86.5%). On a fresh hand-labelled holdout, notes written by gpt-6-luna find 69% of the
memories, 88% of what they store worth keeping, and notes written by Haiku 5.5 57% and 83%
(three runs each, on 0.4.1). Model-based evals vary from run to run and from day to day, so
compare averages of several, run on the same day.
