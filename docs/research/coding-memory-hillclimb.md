# Evals and hillclimbing for coding-agent memory

Research note for `recall` 0.3.0, written 2026-10-12 and 13. One day of building evals that
mirror how coding agents use recall, then changing recall one root cause at a time against them.
It follows [coding-memory-benchmarks.md](coding-memory-benchmarks.md), which chose the public
benchmarks, and [retrieval-benchmarks.md](retrieval-benchmarks.md), which covers LongMemEval and
LoCoMo. Every number names its eval, split and number of runs; search is deterministic, so a
search figure is one run. Token counts are estimates in Claude tokens, computed from the runs'
transcripts by [docs/benchmarks/agent_tokens.py](../benchmarks/agent_tokens.py) (word pieces of
tool output times a factor fitted to Claude's own per-call counts, R² of 0.98 or better, plus
the tool definitions measured directly) unless a row says otherwise.

## Summary

1. **Search: one ranking change, a large gain.** A chat's title now counts for less as a query
   grows past a title's length. On SWE-ContextBench Lite, searching all 300 sessions with all 99
   issues, the past fix is in 0.3.0's top five for 79.8% of issues, up from 46.5% in 0.2.1
   (hybrid). Queries of 12 words or fewer rank as before; LongMemEval and LoCoMo did not move.
2. **Tokens: five changes to what recall's tools return.** On the 40 held-out SWE-ContextBench
   issues an agent using recall takes in 9.0k Claude tokens per issue with 0.3.0 (one run)
   against 14.4k with 0.2.1's tools (five runs), 37% fewer, and names the past fix for 90% of
   issues both times. On 47 held-out questions about my own past work (three runs each): 16.0k
   against 19.1k, 17% fewer, with 85.1% answered correctly both times.
3. **Notes: extraction reads every chat.** On MemoryCode's 40 held-out histories (three runs
   each), conventions held in their latest form rose from 9.1% to 61.6% and changed conventions
   recorded correctly from 4.6% to 53.6%. On a fresh holdout of my chats (six runs), memories
   found rose from 48% to 61% and precision from 83% to 88%. On an earlier, much-used test set
   of my chats it was flat (section 3).
4. **The notes reach the agent.** With 0.3.0's notes loaded at session start, Claude Haiku 5.5
   follows the latest conventions on long MemoryCode histories 64.8% of the time (held out, one
   run), against 45.3% with 0.3.0's search alone (held out, three runs). This measures the
   session-start path as it stands; it is not a code change, and 0.2.1's notes were not measured
   this way.
5. **More candidates were reverted than kept**, most because they gained on train and not on
   test, or saved tokens by changing what agents did (section 4).
6. **Still open:** a consolidation fix whose gain sat inside the noise, an untested retry for
   extraction batches that come back empty, and short histories, where notes made the agent
   slightly worse (section 5).

## 1. What was measured

Four product metrics: find rate (M1), answer success (M2), tokens per answer (M3) and note
quality (M4). Public benchmarks rank recall against others; my own labelled chats (private
cases, aggregates only here) check that gains carry over to real use.

| Metric | Eval | Size (train / test) | Graded by |
| --- | --- | --- | --- |
| M1 | SWE-ContextBench Lite: the related issue's text as the query, over 300 Claude Code sessions | 99 issues (59 / 40) | the benchmark's gold links |
| M1 | agent-memory-bench (AMB) corpus: the session that settles a task, and current above superseded | 34 tasks (20 / 14) | its planted sessions |
| M1 | my search cases: hand-written questions and the chats that answer them | 150 (90 / 60) | my labels |
| M1 guard | LongMemEval_S and _M, LoCoMo | 470, 470, 1,536 | each benchmark's labels |
| M2 | SWE-ContextBench agent: Claude Code names the past sessions it would rely on | 99 (59 / 40) | gold links |
| M2 | my questions about past work, some with no answer in the history | 110 (63 / 47) | Claude Sonnet 5.5 against checkable claims |
| M2 | MemoryCode agent: code that follows the latest conventions, with no memory, recall's tools, its notes at session start plus tools, or the whole history | 40 / 40 histories, 3 requests each | the benchmark's own checks |
| M3 | Claude tokens of recall output the agent takes in per question, plus recall's tool definitions, from the M2 runs | | `agent_tokens.py` |
| M4 | MemoryCode extraction: notes after each history | 40 / 40 histories | exact match on a convention's token, else Claude Sonnet 5.5 |
| M4 | my memory cases: chats and the memories they should and should not yield | 13 / 11 sets, 14 holdout | Claude Sonnet 5.5 |

The agent under test was Claude Haiku 5.5 in Claude Code, run headless with my hooks, MCP
servers, plugins and settings off, and with only recall's MCP tools, so no answer could be read
any other way. The judge, Claude Sonnet 5.5, was never the model under test. Notes were written
by gpt-6-luna through a ChatGPT plan, the model my own nightly run uses.

## 2. Method

The process follows Anthropic's guidance on automating eval design and hillclimbing:

- **Headroom.** The public sets were chosen where 0.2.1 was far from its ceiling: 46.5% on
  SWE-ContextBench (top five, all 300 sessions), 9.1% on MemoryCode notes, against 98.7% on
  LongMemEval_S, which became a guard.
- **A train/test split.** Each eval's cases are split 60/40 with a fixed seed, stratified by
  repo, history length or tag. Tuning reads train outputs only. Test scores print as aggregates,
  and were run once per round. The memory holdout of my chats was scored once, at the end, for
  the final candidates.
- **Noise first.** Deterministic evals (search) use paired bootstrap intervals on the same
  cases. Model-based evals were run three or more times before any change, and a change had to
  beat that spread: MemoryCode extraction recall has a standard deviation of about 2 points
  between runs, and my memory eval moves by 5 to 10 points.
- **One root cause per round.** Each change came from reading train failures and naming why
  they failed. Later in the day, several fixes for one cause were screened at once on the
  cheapest faithful proxy (replaying logged tool calls, extraction without consolidation), and
  only the best went to a full run.
- **Keep or revert.** The rule was to keep a change only if train improved beyond noise, test
  improved or held at its bar, and no guard regressed. Four landed changes did not pass it as
  written: three in search and the tools, and the notes change, which failed on the test split
  of my chats before the decision moved to a fresh holdout. Section 3 says which and why.
- **General mechanisms only.** No change mentions a word, repo, id or phrasing seen in a
  failure; failure text never went into a prompt, tool description or code; gold labels and
  benchmark artefacts (such as matching issue numbers) were never used.
- **Checked graders.** On MemoryCode the judge agreed with exact matching on 781 of 795
  convention checks. The judges for my cases were revised twice, both times before any decision
  rested on them, after reading where they mis-scored.

## 3. What landed

| Area | Change | Root cause | Train | Test |
| --- | --- | --- | --- | --- |
| Search | A title's weight in the ranking fades past 12 query words (squared) | An issue's text (~270 tokens) shares a word with nearly every title, so the title ranking rewarded chats for having a title | Repo-scoped hybrid top 5: 50.8% → 76.3% (59 issues) | 50.0% → 92.5% (40 issues) |
| Search | The fade steepened to the fourth power | Same cause | Top 5 flat in 3 of 4 configurations; keyword MRR 0.574 → 0.621; repo-scoped hybrid MRR 0.673 → 0.670 | Keyword MRR 0.573 → 0.643, hybrid 0.702 → 0.738 |
| Tokens | Search results drop fields that repeat others | The agent name repeats the chat id's prefix, and the top snippet repeats the first hit | SWE-ContextBench input tokens −10.5% (3 runs a side) | −10.0% (1 run a side) |
| Tokens | Search results reach clients that read text (Codex, Cursor) as compact text | Those clients got one indented JSON block per chat | 29% fewer characters per search, replaying 481 logged searches | |
| Tokens | Tool schemas published without their "title" keys | Every request carries the definitions; titles only repeat property names | 272 fewer tokens in Claude Code's first request, measured directly | |
| Tokens | `search_chats` takes `alternatives`: several phrasings in one call, fused, each chat once | Agents searched about four times a question, rephrasing after a miss, and each search returned a full page of results | SWE-ContextBench tool output −20.6% (3 runs of the change) | −19.3% (3 runs of the change) |
| Tokens | `read_chat` clips a long message to its start, the passage matching the query, and its end | Reads returned long messages (pasted logs, long replies) in full; clipping cut a logged read by about a quarter in replay | Tool output: SWE-ContextBench −5.5% (3 runs a side), my questions −9.8% (1 run of the change) | −9.1% (5 runs a side), −7.4% (1 run of the change) |
| Notes | The extractor answers with one entry per chat; each memory says first whether it outlasts the task; a preference needs evidence beyond the task | Asked for one list over a batch of about 12 chats, the model answered for the first few | MemoryCode conventions held: 10.6% → 69.3% (3 runs) | 9.1% → 61.6% (3 runs) |

The per-round token figures are the estimates each round used (total input tokens for the first
row, an estimate of tool output for the last two); section "The release run" below gives the
release totals in Claude tokens.

**Where the keep rule bent.** The fourth-power round kept the top five where it was in three of
four configurations, and was kept for ranking the right chat higher (keyword MRR up on train and
test), although repo-scoped hybrid MRR on train dipped (0.673 → 0.670). The notes change failed
on the test split of my chats (recall flat, precision 98% → 92%); that split had been used for
several decisions by then, so the decision moved once to a fresh holdout and MemoryCode's held-out
histories, where it passed (section 3). The compact-text and schema-title rounds landed on deterministic replay without eval
runs: the eval agent is Claude Code, which reads the structured results rather than the text
block, so an agent run could not see the first change; the second is a strict subset of a round
whose runs showed no change in behaviour, and its saving is fixed per request.

**Guards.** The search changes left LongMemEval_S and _M and LoCoMo keyword retrieval identical
to the last decimal (their chats have no titles, so the title change cannot reach them). Queries of 12 words or fewer rank as before; on my search cases a few longer held-out
questions moved up (keyword top 5 78.3% → 80.0%, hybrid top 1 68.3% → 70.0%). For the token
changes, the agent's hit rate on SWE-ContextBench and its accuracy on my questions stayed within
their paired intervals.

**How the notes change was decided.** It was first judged on a test set of my chats that by then
had informed several decisions, and there it did not help: recall was flat (44% to 45%) and
precision fell (98% to 92%, six runs each). Reading more into that set would have meant tuning on
it, so the decision was made once, on a fresh holdout of my chats (six runs: recall 48.2% →
61.0%, precision 83.2% → 88.1%) together with MemoryCode's held-out histories (three runs: 9.1%
→ 61.6%). The batching diagnosis behind it was direct: on four long MemoryCode training
histories, the production batching found 3 of 64 conventions as candidates; the same batches with
one entry per chat found 33.

**What it costs.** More candidates mean more consolidation calls. On MemoryCode's 40 held-out
histories, three runs, where nearly every session sets a rule: 780 → 1,927 model calls, input
14.3M → 34.0M tokens, output 135k → 608k. On my holdout chats, three runs: 37 → 40 calls, with 1.4
times the output. The v4 prompt makes the nightly run read past chats again with it, newest
first, within the nightly call budget (80 by default), so a large history takes several nights.

**The release run**, on 0.3.0's final code:

| Eval (held-out split) | 0.2.1 | 0.3.0 |
| --- | --- | --- |
| SWE-ContextBench agent: past fix named | 90.0% (5 runs) | 90.0% (1 run) |
| SWE-ContextBench agent: Claude tokens per issue, tool definitions included | 14.4k (5 runs) | 9.0k (1 run), −37% |
| My questions: answered correctly | 85.1% (3 runs) | 85.1% (3 runs) |
| My questions: Claude tokens per question, tool definitions included | 19.1k (3 runs) | 16.0k (3 runs), −17% |
| My search cases, top 5: hybrid / keywords | 86.7% / 78.3% | 86.7% / 80.0% |
| LongMemEval_S, LoCoMo, AMB | | unchanged |

The 0.2.1 SWE-ContextBench runs used 0.2.1's tools with the first version of the title fade
(squared), which almost never touches the short queries agents send (1 of 157 searches on
SWE-ContextBench and 5 of 282 on my questions were over 12 words), so the difference is what the
tools return. recall's tool
definitions are 2,591 Claude tokens in 0.2.1 and 2,480 in 0.3.0.

**Notes at session start** (measured, no code change). recall's hook loads the notes at the start
of every session, so the question that matters is whether 0.3.0's notes change what an agent
does. A new arm of `recall eval memorycode --agent` (`--arm hook`) puts the notes an extraction
run wrote into the session-start block, built by the same code as the hook, and gives the agent
recall's tools as well. Claude Haiku 5.5:

| MemoryCode: code that follows the latest conventions | All | 20 to 100 sessions | 1 to 15 sessions |
| --- | --- | --- | --- |
| 0.3.0's tools alone (held out, 3 runs) | 75.8% | 45.3% | 94.2% |
| 0.3.0's notes at session start, plus tools (held out, 1 run) | 79.7% | 64.8% | 88.6% |
| No memory (training histories, 1 run, for reference) | 2% | 3% | 2% |
| Whole history in the prompt (training histories, 1 run, for reference) | 90% | 80% | 96% |

With the notes, the agent took in about a third fewer input tokens (median, one run). The
training histories (three runs each) point the same way: 74.4% → 81.1% overall and 45.6% →
59.2% on long histories. On short histories the notes cost accuracy: with the notes in hand, the
agent reads fewer chats. Of the long-history requests that still fail on the training histories,
about a third have no note for the convention, and about a quarter have the right note in the
session-start block and the agent does not apply it.

## 4. What was reverted, and why

| Candidate | Result | Why it went |
| --- | --- | --- |
| Search: the opening request also takes the faded title weight | +5 points top 5 on train, within noise; top 10 fell | Not beyond noise; test not run |
| Search: a newer restatement of the same thing moves above the older one | On LongMemEval's knowledge-update questions, the newest session came first 33 points more often on train and 46 on test | My search cases leaned worse on exactly the questions about reversed decisions, AMB lost a case, MemoryCode end to end was flat; LongMemEval is a guard, not a goal |
| Tokens: search results as plain text only | Input tokens −5.5% on train (5 runs against 4), within noise on test; 4 issues better, 7 worse | Replaced by the text block for text-reading clients only |
| Tokens: shorter tool descriptions | Input tokens −7 to −9% | Agents searched less and found the past fix less often (3 better, 8 worse) |
| Tokens: `alternatives` returning every chat any phrasing found | Agents sent about five phrasings a call; input tokens +11% (3 runs a side) | Replaced by fusing to the usual result count |
| Tokens: a `max_chars` parameter on `read_chat` | The agent set it on every read, often higher than the default | No saving; clipping without a parameter kept instead |
| Notes: one entry per chat, nothing else | Recall up, but precision on the test set of my chats fell from 95% to 85% (six runs) | Replaced by the version with the temporal call and the preference bar |
| Notes: an instruction that an empty list is the default | Candidate recall on MemoryCode training histories collapsed (40% to 6% on short histories) | Dropped after the screen |
| Notes: stricter or softer bars for decisions and open threads; the temporal call as each memory's last field | Precision under the bar set for test, or recall lost | Dropped; the landed version asks for the temporal call first, before the memory is written |
| Notes: the temporal call without the preference bar | Passed every bar, with a smaller gain (MemoryCode held out 34.0%, three runs) | The landed version won on the same runs |
| Tools: wording that tells the agent to keep searching until nothing new comes back and to prefer the newer chat | Long MemoryCode histories +6 points on train and +4.9 held out (one variant +13 on train) | 5–25% more tokens on every eval; notes at session start were the cheaper route |
| Delivery: the agent told to read the project's notes before writing code, which then lists every note | Long-history accuracy flat on train (one run) | Cost on my questions +11% (one run), failing its guard |

Three lessons generalise. An agent uses any budget knob it is given, so token savings belong in
defaults. Fewer tokens in a tool's description can change what the agent does with the tool.
And a large gain on one benchmark is not a reason to ship when the evals closest to real use
lean the other way.

## 5. Still open

- **Consolidation keeps the rules that still hold.** On MemoryCode's training histories (three
  runs), 86 of 97 conventions that extraction found but the notes lost were in a note that
  bundled several rules and was superseded by a note carrying only the rule that changed. A
  replaced form now lingers as current for 8.4% of changed conventions (2.7% in 0.2.1, which
  recorded few of them at all). The fix (a superseding note keeps the parts that did not change)
  leaned right on every metric but inside the noise: on 40 fresh MemoryCode held-out histories,
  recall 68.3% (0.3.0, three runs) against 70.0% (two runs); on my holdout chats, 61.0% against
  63.2% (six runs each), with one more leak. It was not landed and needs more test runs.
- **Extraction batches that come back empty.** In the landed MemoryCode training run, 9 of 291
  extraction batches returned nothing for any chat, and held 100 of 2,181 convention statements.
  Reading such a batch once more recovered memories for 8 of the 9 in a one-run replay, at about
  3% more extraction calls. Untested end to end.
- **Short histories.** With notes at session start, accuracy on 1-to-15-session MemoryCode
  histories fell from 94.2% to 88.6% (held out; three runs and one): with the notes in hand, the
  agent reads fewer chats.
- **Notes the agent has but does not apply.** About a quarter of the long-history failures that
  remain on the training histories have the right note in the session-start block.

## 6. Caveats

- Samples are small: 99 SWE-ContextBench issues carry about ±10 points, 40 MemoryCode histories
  about ±6. Every comparison above is paired, on the same cases.
- About a third of SWE-ContextBench's links pair two issues fixed by the same pull request, and
  some "past" fixes were opened after the issue they help with. For retrieval this is close to
  real use (a duplicate bug fixed last month is what recall should find); for end-to-end runs
  it would hand over the answer.
- The memory products' SWE-ContextBench figures are the paper's own runs of an undefined metric
  at different cutoffs, and the paper describes the queries for only two of the four; see the
  README's table. 0.2.1 already matched LangMem at top 10 and beat Supermemory at top 15.
- recall's ranking was tuned on 59 of SWE-ContextBench's 99 issues. The other 40 confirm the
  gain, but future changes should keep the same split.
- MemoryCode is synthetic: toy conventions in mentor chats, not real repos.
- The notes-at-session-start arm ran once on the held-out histories, to spare the laptop; its
  three training runs point the same way.
- My evals cover my own chats. They show direction, not a general rate.

## Reproducing it

From a clone of the repo. The SWE-ContextBench and MemoryCode readers need `pyarrow`, which
recall does not install, and `--hybrid` needs an `[embeddings]` table in config.toml.

| Benchmark | `--data` folder | Command |
| --- | --- | --- |
| SWE-ContextBench | `repo/` from [github.com/jiayuanz3/SWEContextBench](https://github.com/jiayuanz3/SWEContextBench/tree/12ad6ab14e18e9378e1e293c9edbc3f7ce43d27b) and `hf/` from [huggingface.co/datasets/jiayuanz3/SWEContextBench](https://huggingface.co/datasets/jiayuanz3/SWEContextBench/tree/12c65bd15e2559bc808065565e941ee7bbbd008f) | `uv run --with pyarrow recall eval swe-contextbench --data DIR --scope repo\|all [--hybrid] [--agent] [--split train\|test\|all]` |
| MemoryCode | the parquet file from [huggingface.co/datasets/CohereLabsCommunity/memorycode](https://huggingface.co/datasets/CohereLabsCommunity/memorycode) | `uv run --with pyarrow recall eval memorycode --data DIR --split test --limit 40 --repeats 3 --fresh [--model PROVIDER:NAME]`, then `--agent --arm none\|tools\|notes\|hook\|full [--notes RUN_DIR] [--hybrid]` (`hook`: notes at session start plus tools) |
| AMB | `hf/corpus/` from [huggingface.co/datasets/Gde05/agent-memory-bench-corpus](https://huggingface.co/datasets/Gde05/agent-memory-bench-corpus) and `repo/` from [github.com/GiulioDER/agent-memory-bench](https://github.com/GiulioDER/agent-memory-bench/tree/c1b15726f1153b02dda0490ca1936bb86a809514) | `uv run recall eval amb --data DIR [--hybrid]` |
| LongMemEval, LoCoMo | the datasets' JSON files | see [retrieval-benchmarks.md](retrieval-benchmarks.md) |
| Claude tokens per question | the agent runs' folders | `python docs/benchmarks/agent_tokens.py DEFS_BEFORE DEFS_AFTER before:RUN_DIR ... after:RUN_DIR ...` (method in its docstring) |

`--split` defaults to train; add `--against RUN_DIR` for a paired comparison with an earlier
run. `--agent` runs Claude Code headless once per issue or request. A MemoryCode extraction run
over 40 histories, three times, made 1,927 model calls with 0.3.0. The evals on my own chats
(`recall eval search --v2`, `recall eval agent`, `recall eval memory --v2`) need cases of your
own; the README's Quality section describes them.
