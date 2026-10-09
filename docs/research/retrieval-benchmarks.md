# Retrieval: what the open-source memory systems do, what the benchmarks show, what recall takes

Research note for `recall`, 2026-10-09; section 5 holds the full benchmark run. Question: which retrieval methods from open-source
memory systems (Honcho, Mem0, agentmemory, and the tools that search coding-agent chats) have
evidence behind them, and which fit a local tool built on SQLite FTS5 and markdown notes?
It extends [agent-memory-prior-art.md](agent-memory-prior-art.md), which covers storage,
consolidation and the LoCoMo audit.

Evidence grades: **source** (read in code at a pinned commit), **docs**, **vendor** (a score
reported by the party selling the system), **independent**. Section 4 holds recall's own
measurements, which are reproducible with `recall eval longmemeval`.

## Summary

1. **Every "state of the art" claim is vendor-reported, and they contradict each other.**
   Honcho, Mem0, Hindsight, EverMemOS, Supermemory and ByteRover each claim the top of
   LongMemEval or LoCoMo, with different answer models and judges (section 2).
2. **Headline scores mostly measure the answering model, not retrieval.** Honcho's
   LongMemEval-S result answers 66% of questions with no retrieval call at all, and its LoCoMo
   run puts the whole conversation in the prompt [honcho-stats], [honcho-loc]. Mem0's numbers
   come from its managed platform, and its committed result files do not reproduce the README
   figures [mem0-readme], [mem0-results].
3. **Controlled retrieval ablations exist in only a handful of places**: the LongMemEval paper,
   ReFind, engram, AgentIR, memini and agentmemory's own harness (section 3). None of the vendor
   systems publishes one.
4. **Porter-stemmed BM25 is a strong baseline on chat sessions**, and much of the reported
   advantage of dense retrieval comes from the LongMemEval reference code tokenising BM25 on
   spaces with no stemming [lme-code], [engram].
5. **What does help, in order of evidence:** scoring a session on all its matching turns, not
   just the best one [refind]; a date window when the question names a time [lme-paper],
   [refind]; fusing BM25 with a small embedding model when the fusion is tuned [agentmemory-bench],
   [engram]; indexing distilled facts in the same index as the text they came from [lme-paper].
   Cross-encoder reranking and LLM query expansion have little evidence at this scale, and the
   agent calling recall is already an LLM that can rephrase.

## 1. How the systems retrieve (source-verified)

| System | Unit indexed | Lexical | Dense | Fusion | Other |
| --- | --- | --- | --- | --- | --- |
| Honcho `cb8ab1a` | Messages, and extracted facts | Postgres `ts_rank` full-text, or `ILIKE` when the query has punctuation | text-embedding-3-small, pgvector HNSW | RRF k=60, unweighted, on the public `/search` [honcho-search] | The answering agent's tools are vector-only plus grep and date-range tools; hits come with 2 messages either side, overlapping windows merged [honcho-tools], [honcho-msg] |
| Mem0 library `b7ad69a` | Extracted facts (ADD-only) | BM25 sparse vectors, spaCy lemmas | text-embedding-3-small by default | Additive: (semantic + BM25 + entity) / 1–2.5, but BM25 only re-scores the semantic candidate pool [mem0-search] | Entity boost, scaled down for common entities; time decay is platform-only [mem0-notices] |
| agentmemory `df3d4a8` | LLM-compressed observations | In-memory BM25 (k1 1.2, b 0.75), synonyms, prefixes | all-MiniLM-L6-v2, opt-in | Weighted RRF k=60 (BM25 0.4, vector 0.6, graph 0.3) [agentmemory-hybrid] | At most 3 results per session; optional MiniLM cross-encoder |
| cass `6bb53db` | One document per message | BM25 with prefix matching | MiniLM-L6-v2, opt-in | RRF k=60, lexical-only until vectors are ready [cass-query] | Optional cross-encoder; recency blend in the TUI only |
| claude-mem `fa8ab09` | Facts and narratives | SQLite FTS5 as fallback | Chroma | None: semantic first, FTS5 only if that fails [claude-mem-search] | Last 90 days by default |

## 2. Benchmark claims

| System | LongMemEval-S | LoCoMo | BEAM 1M | Answer model | Notes |
| --- | --- | --- | --- | --- | --- |
| Mem0 platform (vendor) | 94.4 | 92.5 | 64.1 | GPT-5 (also judge) | Platform only; committed files recount to 93.4 / 91.6 [mem0-results] |
| Mem0 library (vendor) | 88.6–91.0 | | | | [mem0-results] |
| Honcho (vendor) | 90.4 | 89.9 | 0.631 | Claude Haiku 4.5 | Whole LoCoMo transcript in the prompt; full-context Haiku on the same LoCoMo questions scores 83.1 [honcho-loc] |
| Hindsight (vendor) | 91.4 | 89.6 | 73.9 | Gemini-3 Pro | Dense + BM25 + graph + temporal, RRF, cross-encoder [hindsight] |
| EverMemOS (vendor) | 83.0 | 93.05 | | GPT-4.1-mini | A third party measured 38.38 on LoCoMo [locomo-audit] |
| Zep (paper) | 71.2 | | | GPT-4o | [zep-paper] |
| Letta filesystem (vendor) | | 74.0 | | GPT-4o-mini | grep plus embedded file search [letta-fs] |

LoCoMo's answer key is wrong on 6.4% of questions and its usual judge accepts 62.8% of
deliberately wrong answers [locomo-audit], so differences above about 90 are noise. A
one-harness rerun of commercial systems on LongMemEval moved scores by +3 to -28 points from
their published figures [redis-bench].

## 3. Ablations that isolate retrieval

LongMemEval session retrieval, answer-session recall@5 unless noted.

| Source | Comparison | Result |
| --- | --- | --- |
| LongMemEval paper, M set [lme-paper] | BM25 vs Contriever vs Stella (session R@5) | 0.634 / 0.723 / 0.720; BM25 uses whitespace tokens with no stemming [lme-code] |
| LongMemEval paper [lme-paper] | Facts prepended to each indexed item | BM25 0.634 → 0.683; facts in a separate index merged afterwards did worse than none |
| LongMemEval paper [lme-paper] | LLM-inferred date window on temporal questions | +6.8% (session), +11.3% (round) with GPT-4o; worse with Llama-8B |
| engram [engram] | Fixing BM25's tokeniser | recall_all@5 0.513 → 0.673; hybrid with z-score fusion 0.802 |
| agentmemory harness [agentmemory-bench] | BM25 vs BM25 + MiniLM (RRF) | recall_any@5 86.2 → 95.2 |
| AgentIR [agentir] | BM25 vs BGE-small vs RRF | LongMemEval 0.909 / 0.886 / 0.929; on LoCoMo fusion hurt (0.875 / 0.632 / 0.797) |
| ReFind, iterative BM25 agent [refind] | Removing parts of the method (S / M) | ±2-turn context −9.2 / −4.9; session fusion −3.9 / −4.9; date filter −1.9 / −4.9; skipping seen sessions −1.2 / −9.3 |

## 4. What recall adopted, and how it measured

Measured with `recall eval longmemeval` on `longmemeval_s_cleaned.json` (470 non-abstention
questions, both sides of each chat indexed, as recall indexes real chats), and with the
personal search evals (8 hand-written cases, 150 sampled known-item queries) on one
developer's 2,778 chats.

**LongMemEval_S, all 470 questions, keyword search only**

| Search | recall_any@5 | recall_all@5 | recall_all@10 | ndcg_any@10 |
| --- | --- | --- | --- | --- |
| Before (best message plus title, AND pass then OR pass) | 0.894 | 0.717 | 0.872 | 0.785 |
| Fusion with every untitled chat's opening counted as a title | 0.936 | 0.787 | 0.934 | 0.873 |
| Fusion, titles only from agents, best and sum weighted 1 / 1 | 0.972 | 0.864 | 0.917 | 0.917 |
| **Fusion as shipped** (k=20; best 0.5, sum 1, title 2, all-words 1) | **0.974** | **0.872** | **0.926** | **0.915** |
| Same, user turns only (the paper's setting) | 0.960 | 0.840 | 0.913 | 0.895 |

Two findings decided the design. Scoring a chat on its best three messages, not only its best
one, is worth about 2 points of recall_all@5 (0.851 with the best message alone). Counting an
untitled chat's opening message as its title cost 7 points: LongMemEval's sessions have no
titles, so the "title" duplicated a message the other rankings already counted. Real chats
mostly do have titles (Cursor titles nearly all of them, Codex two thirds, Claude Code a
third), so titles still count, but only when an agent wrote them.

**Hybrid, on the 164 LongMemEval questions embedded so far (nomic-embed-text through Ollama)**

| Search | recall_any@5 | recall_all@5 | ndcg_any@10 |
| --- | --- | --- | --- |
| Keyword fusion | 0.988 | 0.921 | 0.927 |
| + dense ranking, weight 1 | 0.988 | 0.921 | 0.956 |
| + dense ranking, weight 3 | 0.994 | 0.939 | 0.949 |
| Dense alone | 0.976 | 0.939 | 0.941 |

The sample leans towards the easier question types, because the dataset is sorted by type and
embedding all 189,000 turns takes hours on a laptop.

**One developer's ~2,800 chats** (MRR, with hit@5 in brackets; final code, 2026-10-09)

| Search | Hand-written (8) | Known-item (150) | Paraphrased (100) |
| --- | --- | --- | --- |
| Before | 1.000 (1.00) | 0.916 (0.967) | 0.591 (0.79) |
| Keyword fusion | 0.938 (1.00) | 0.884 (0.960) | 0.668 (0.83) |
| + embeddings, weight 1 (the default when on) | 0.938 (1.00) | 0.859 (0.947) | 0.710 (0.88) |

Known-item queries are three words copied from a chat's opening message, which favours exact
matching. The paraphrased queries were written by a local model (qwen3:8b) from each chat's
title and opening, in other words; on the 21 that share fewest words with the chat, hit@5 rose
from 0.52 with keywords to 0.71 with embeddings at weight 1 (0.81 at weight 2). Weight 1 is the
default: it gains where queries are paraphrased and costs a few points where they are exact
words, which keyword search already finds.

Adopted:

- **Session fusion.** Each chat is ranked several ways and the rankings fused with RRF: its
  best message, the summed BM25 of its best three messages, its title when an agent wrote
  one, its opening message, and a message holding every query word.
- **Query words.** Words such as "what", "did" and "we" are dropped once two others remain.
- **Date windows and exclusions.** `since` and `until` filter by message time, and `exclude`
  skips chats already read, for the calling agent's follow-up searches.
- **Hits.** Each result carries its best three matching messages with their positions, so the
  agent can open a chat where the answer is.
- **Optional embeddings.** A local model through Ollama or any OpenAI-compatible endpoint,
  one vector per message, fused as one more ranking.

Not adopted: cross-encoder reranking (no ablation at session level, and the session rankings
are already near the ceiling), entity graphs (agentmemory's own benchmark shows its graph
hurting [agentmemory-quality]), and query expansion by an LLM inside recall (the calling agent
already rephrases, and the skill tells it to). Distilled facts as extra index keys remain a good
candidate: recall's nightly run already extracts them, but only for about 10% of chats so far.

## 5. Full benchmark with embeddings on (2026-10-09)

Embeddings: nomic-embed-text through Ollama, fused at weight 1. Every number below is
reproducible with `recall eval longmemeval|locomo [--hybrid] [--official] [--qa MODEL]`.

### 5.1 Retrieval

| Benchmark | Questions | Search | recall_any@5 | recall_all@5 | recall_all@10 | ndcg_any@10 |
| --- | --- | --- | --- | --- | --- | --- |
| LongMemEval_S, both sides indexed | 470 | keyword | 0.974 | 0.872 | 0.926 | 0.915 |
| | | **hybrid** | **0.987** | **0.906** | **0.955** | **0.942** |
| LongMemEval_S, official subset, user turns only | 419 | keyword | 0.962 | 0.828 | 0.909 | 0.892 |
| | | **hybrid** | **0.983** | **0.883** | **0.933** | **0.932** |
| LongMemEval_M (~475 sessions a question) | 470 | keyword | 0.909 | 0.721 | 0.817 | 0.792 |
| | 19 (every 24th) | keyword, same 19 | 0.947 | 0.895 | 0.895 | 0.857 |
| | | hybrid, same 19 | 0.947 | 0.842 | 0.895 | 0.840 |
| LoCoMo, categories 1-4 | 1,536 | keyword | 0.896 | 0.772 | 0.860 | |
| | | **hybrid** | **0.921** | **0.801** | **0.885** | |

On the 19-question LongMemEval_M sample, embeddings lost one question (recall_all@5 0.895 to
0.842): a sample that small cannot separate the two, and embedding all of M (millions of
turns) was out of reach on a laptop, so whether the dense ranking helps at ~475 sessions a
question is open. Paired bootstrap 95% intervals for hybrid minus keyword on LongMemEval_S (470): recall_any@5
+1.3 [+0.2, +2.6], recall_all@5 +3.4 [+1.5, +5.3], ndcg@10 +2.7 [+1.8, +3.7]; on the
official subset with user turns only, recall_all@5 +5.5 [+3.3, +7.9]. Every interval excludes
zero.

Latency on a laptop (Apple Silicon): a search over one LongMemEval_S history (~50 sessions)
takes 5.8 ms at the median and 69 ms at p95 with keywords, 7.0 / 87 ms with the dense ranking
added; over LongMemEval_M histories (~475 sessions) keyword search takes 7 / 14 ms. Embedding
the query through Ollama adds roughly 20-40 ms once the model is loaded. Indexing needs no
model call; embedding runs at about 50 messages a second (nomic) on the same machine.

**Against published retrieval results** (same metrics; rows marked * use another question set
or dataset version, so compare loosely):

| System | Embedder | LongMemEval session retrieval | LoCoMo session Hit@5 |
| --- | --- | --- | --- |
| **recall, hybrid** | nomic-embed-text (local) | S: any@5 0.987, all@5 0.906; official user-only all@5 0.883 | 0.921 |
| recall, keyword | none | S: any@5 0.974, all@5 0.872; M: all@5 0.721 | 0.896 |
| LongMemEval paper, best setup [lme-paper] | Stella 1.5B + LLM facts | M: all@5 0.732, all@10 0.862 | |
| engram* [engram] | bge-small (local) | S, user turns, 343-question split: all@5 0.813 dense, 0.802 hybrid | |
| memini* [memini] | MiniLM (local) | any@5 0.976 BM25, 0.984 hybrid | |
| AgentIR* [agentir] | BGE-small (local) | R@5 0.909 BM25, 0.929 RRF | 0.875 BM25, 0.797 RRF |
| agentmemory* [agentmemory-bench] | MiniLM (local) | 0.862 BM25, 0.952 hybrid (metric undefined) | |

None of the vendor systems in section 2 publishes retrieval metrics, only end-to-end answer
accuracy, so they appear in 5.2.

### 5.2 End to end: retrieve, answer, judge

Claude Haiku 5.5 (`claude-haiku-5-5`) answers with the benchmark's step-by-step reading
prompt and judges with the benchmark's per-type judge prompts; every call stays under 100k
tokens. Costs are list prices for answering ($0.10 per million input tokens and $0.50 per
million output below 100k for Haiku 5.5; $2 and $10 for Sonnet 5.5), from measured token
counts; the runs themselves went through a Claude subscription.

All 500 questions (including the 30 abstention questions), Haiku 5.5:

| What the model reads | Accuracy | Prompt tokens a question | Answer latency (median) |
| --- | --- | --- | --- |
| recall: matched windows of the top 5 chats | 0.824 | ~17,300 | 7.1 s |
| recall: the top 10 chats in full | **0.874** | ~44,100 | 7.3 s |
| Oracle: only the sessions holding the answer | 0.920 | ~10,600 | 7.1 s |

Accuracy against cost, on the 200-question stratified sample (each accuracy ±~5 points),
judged by Haiku 5.5 throughout so that only the answering model changes:

| What recall hands the model | Haiku 5.5 | Cost a question | Sonnet 5.5 | Cost a question |
| --- | --- | --- | --- | --- |
| Matched windows of the top 5 chats | 0.830 | $0.0022 | 0.890 | $0.037 |
| Top 5 chats in full | 0.875 | $0.0028 | | |
| Top 10 chats in full | 0.870 | $0.0049 | | |
| Top 20 chats in full | 0.905 | $0.0088 | | |
| Oracle | 0.910 | $0.0015 | | |

With the top 20 chats, Haiku 5.5 comes within half a point of its own oracle and beats Sonnet
5.5 reading windows at a quarter of the cost. A Sonnet 5.5 run over the top 10 chats was
stopped for time. With the top 10 chats every answer session was in what the model read; with
windows, nearly all misses still had the answer in front of the model. The remaining errors are
reading and reasoning, which matches the LongMemEval paper's finding that 40-50% of errors happen
with the right sessions retrieved [lme-paper]. Longer context helped multi-session (0.68 →
0.80) and temporal questions (0.84 → 0.91) and hurt abstention (0.87 → 0.77): with more to read,
the model more often answers a question it should decline.

**Against the published end-to-end numbers on LongMemEval_S** (vendor-reported unless noted;
different answer models and judges, so these are context, not a ranking):

| System | Accuracy | Answer model / judge | Context tokens a query | LLM calls at ingest |
| --- | --- | --- | --- | --- |
| Mem0 platform | 94.4 | GPT-5 / GPT-5 | ~7,000 (LoCoMo) | 1 extraction call per add |
| Mastra observational memory | 94.9 | gpt-5-mini | ~30,000 | observer and reflector |
| Hindsight | 91.4 | Gemini-3 Pro / GPT-OSS-120B | token budget | extraction per message |
| Honcho | 90.4 | Haiku 4.5 / GPT-4o | median ~5% of history | deriver per message batch |
| **recall (this run)** | **87.4** | **Haiku 5.5 / Haiku 5.5** | **~44,100** | **none** |
| Supermemory | 81.6-85.2 | GPT-4o to Gemini-3 / GPT-4o | not reported | extraction |
| EverMemOS | 83.0 | GPT-4.1-mini | ~6,700 | extraction, plus a verifier at query time |
| ICM folders, "Cost of Remembering" (n=39) | 74.4 | Sonnet 5 | 5,314 | none |
| Zep | 71.2 | GPT-4o / GPT-4o | ~1,600 | several |
| Full context, Haiku 4.5 (Honcho's baseline) | 62.6 | Haiku 4.5 / GPT-4o | ~115,000 | none |
| Full context, Sonnet 5 ("Cost of Remembering") | 87.2 | Sonnet 5 | 184,186 | none |

Sources: [mem0-results], [mastra], [hindsight], [honcho-stats], [supermemory], [evermemos],
[cost-of-remembering], [zep-paper]. The full-context baseline with Haiku 5.5 was not run:
LongMemEval_S histories exceed the 100k-token cap this run kept to.

What this says about recall: its retrieval is as good as anything published, and better than
the published retrieval-only projects; the end-to-end number sits a few points under the
leaders, of which a part is the judge and sample and a part is reading, not finding. recall
spends no model calls at ingest (the others extract facts from every message with an LLM) and
none at query time; the calling agent is the reader. The cost of that is context: an agent
reading recall's top 10 chats reads ~44k tokens where Mem0's reader gets ~7k of distilled facts.
Reading matched windows instead cuts that to ~17k for 5 points of accuracy.

### 5.3 Which embedding model

| Model | Size | Indexing speed here | LongMemEval_S hybrid ndcg@10 (118 shared questions) |
| --- | --- | --- | --- |
| nomic-embed-text v1.5 | 274 MB, 768 dims | ~50 messages/s | 0.942 |
| qwen3-embedding 0.6B | 639 MB, 1024 dims | ~25-30 messages/s | 0.946 (difference +0.003, 95% CI −0.009 to +0.016) |

recall uses nomic-embed-text: the same accuracy at half the indexing time and less than half
the size. EmbeddingGemma was pulled but not run to completion. Gemini Embedding 2
(`gemini-embedding-2-preview`, March 2026: text, image, video, audio and PDF in one space,
3,072 dimensions, $0.20 per million tokens) was not tested: it is API-only, so it would send
chat text to Google, and these benchmarks are text-only, where open models already match it on
MTEB. recall's `openai-compatible` embeddings provider can point at it for anyone who wants that
trade.

### 5.4 Folder-structured context: ICM (arXiv 2603.16021) and Google's Open Knowledge Format

Interpretable Context Methodology (Van Clief and McDermott, March 2026; v1 called it the Model
Workspace Protocol) replaces multi-agent orchestration with a folder tree: an always-loaded
`CLAUDE.md` (~800 tokens), a routing `CONTEXT.md`, numbered stage folders whose contracts name
exactly which reference files to read, and plain files as the only state [icm]. Its evaluation
is practitioner self-report; the authors' follow-up on 39 LongMemEval_S questions with Sonnet 5
scored 0.744 with folders against 0.872 for full context (p=0.23), at 5,314 against 184,186
tokens a query, and 0.615 for the same agent without filing conventions [cost-of-remembering].
Google Cloud's Open Knowledge Format (June 2026) formalises the same pattern: one concept per
markdown file, per-folder `index.md` for progressive disclosure, and since v0.2 `sources`,
`verified` and `stale_after` fields; it reports no benchmarks [okf].

recall shares the premise (markdown in git, a small always-on index, detail read on demand)
and differs on retrieval: agents search an index over every chat rather than walking folders.
The evidence favours keeping the search: ICM's own folder agent trails long context, and a
study of agent-maintained file memory found the organisation erodes unless the maintaining
model is the strongest available [fsm]. Worth taking from both: OKF's `verified` and
`stale_after` fields (a hand-edited note is human-verified; the nightly run can expire notes by
date), and ICM's loop of turning a correction that keeps recurring into a change to the rules
file, which `recall rules` is placed to do.

## References

- [agentir]: https://arxiv.org/html/2605.25092v1
- [agentmemory-bench]: https://github.com/rohitg00/agentmemory/blob/df3d4a83b966d8d415cb9180d5a4724b07f729dc/benchmark/LONGMEMEVAL.md
- [agentmemory-hybrid]: https://github.com/rohitg00/agentmemory/blob/df3d4a83b966d8d415cb9180d5a4724b07f729dc/src/state/hybrid-search.ts
- [agentmemory-quality]: https://github.com/rohitg00/agentmemory/blob/df3d4a83b966d8d415cb9180d5a4724b07f729dc/benchmark/QUALITY.md
- [cass-query]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/6bb53dbaac912646f262c6c1abf231798fc3baef/src/search/query.rs
- [claude-mem-search]: https://github.com/thedotmack/claude-mem/blob/fa8ab09f06aa05f958c5225cf3756ce52a3ebb96/src/services/worker/search/SearchOrchestrator.ts
- [cost-of-remembering]: https://github.com/RinDig/cost-of-remembering
- [engram]: https://github.com/zaineli/engram
- [evermemos]: https://arxiv.org/html/2601.02163v2
- [fsm]: https://arxiv.org/html/2607.26637v1
- [hindsight]: https://arxiv.org/html/2512.12818v1
- [honcho-loc]: https://github.com/plastic-labs/honcho-benchmarks/blob/main/a1d689b/locomo_baseline_results_20251215_184837.json
- [honcho-msg]: https://github.com/plastic-labs/honcho/blob/cb8ab1a1b7e25db9b8559f8a72d107dc195b7e00/src/crud/message.py
- [honcho-search]: https://github.com/plastic-labs/honcho/blob/cb8ab1a1b7e25db9b8559f8a72d107dc195b7e00/src/utils/search.py
- [honcho-stats]: https://github.com/plastic-labs/honcho-benchmarks/blob/main/a1d689b/longmemeval_s_stats_20251212_201328.json
- [honcho-tools]: https://github.com/plastic-labs/honcho/blob/cb8ab1a1b7e25db9b8559f8a72d107dc195b7e00/src/utils/agent_tools.py
- [icm]: https://arxiv.org/html/2603.16021v2
- [letta-fs]: https://www.letta.com/blog/benchmarking-ai-agent-memory
- [lme-code]: https://github.com/xiaowu0162/LongMemEval/blob/main/src/retrieval/run_retrieval.py
- [lme-paper]: https://arxiv.org/html/2410.10813v2
- [locomo-audit]: https://github.com/dial481/locomo-audit
- [mastra]: https://mastra.ai/research/observational-memory
- [mem0-notices]: https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/mem0/memory/notices.py
- [mem0-readme]: https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/README.md
- [mem0-results]: https://github.com/mem0ai/memory-benchmarks/tree/4b61c5d31b9c668a12b4f5e78064248a02c82d2b
- [mem0-search]: https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/mem0/memory/main.py
- [memini]: https://pkg.go.dev/github.com/eleboucher/memini/bench
- [okf]: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
- [redis-bench]: https://redis.io/resources/agent-memory-benchmark-report/
- [refind]: https://arxiv.org/html/2608.12888v2
- [supermemory]: https://supermemory.ai/research/longmembench
- [zep-paper]: https://arxiv.org/html/2501.13956v1
