# Late interaction on top of recall's hybrid search

Research note for `recall`, 2026-11-02, in two rounds. Question: does late interaction (a ColBERT model's one vector per token, scored by MaxSim) still add anything on top of recall's tuned BM25 plus dense fusion? Chris Simpson found that it keeps two-facet queries intact where a single dense vector averages them away, and noted that production stacks are hybrids [chris]. Method: an opt-in ranker in `search()`, in two modes, measured against the current hybrid on one Apple Silicon Mac (M4 Pro, 24 GB). Round one built it and measured SWE-ContextBench Lite and LongMemEval_S. Round two added the personal search evals, the per-profile split, LoCoMo, the qa_v2 agent eval, replays of real searches at live size, and settings that cut the cost. Round three (night of 2 to 3 Nov) added SMART, controls that score the same candidates without token vectors, a sweep of the dense ranking's weight, and the exact-wording known-item cases. **Round three changes how rounds one and two should be read: see the Answer.** Grades: `[observed]` for my measurements, `[src: ...]` for source read at a pinned version.

## Answer

- **Much of the gain on coding and personal searches is not late interaction.** Scoring the same 20 candidates with the dense vector alone (no token vectors) gets most of it, and so does raising the dense ranking's weight in the fusion from 1 to 3, with no model at all (round three). recall's fusion underweights its dense ranking for natural-language queries.
- **But that free fix has a price the large ColBERT does not pay.** A higher dense weight, the small ColBERT, SMART and the pooled control all lose exact-wording searches (known-item MRR 0.856 down to 0.647 to 0.813). Iso-ModernColBERT is the only arm that gains on natural-language searches and on the two chat benchmarks while holding exact-wording searches (0.867, within noise of 0.856).
- **Late interaction's own contribution shows on two-facet chat questions.** On LongMemEval's two-facet questions the large model gains 10 points; the best dense weight gains 5, SMART 5.
- **SMART on text** (nomic-embed's own token states, no second model): small real gains on conversational memory (LongMemEval +1.3, 6 better / 0 worse; LoCoMo +1.2 over its pooled control), about half the large ColBERT's, and nothing beyond its pooled score on coding searches.

The round-one and round-two answer follows. Its numbers stand; its attribution to late interaction is corrected above.

- **Rerank (the fused top 20 chats scored again at query time) lifts every search benchmark with the large model, and the coding ones with the small model.**

  | Benchmark | Off | Small model, CPU | Large model |
  |---|---|---|---|
  | Personal search cases (search_v2, 150), hit@5 | 0.900 | **0.953** (8 better, 0 worse) | 0.947 (7 / 0), CPU |
  | the same, test split only (60) | 0.867 | 0.950 (5 / 0) | 0.933 (4 / 0), CPU |
  | SWE-CB Lite, issue title as the query (99), hit@5 | 0.485 | 0.677 (19 / 0) | **0.697** (21 / 0), MPS |
  | the same, test split only (40) | 0.550 | 0.800 (10 / 0) | 0.800 (10 / 0), MPS |
  | SWE-CB Lite, whole issue (99), hit@5 | 0.798 | 0.848 (6 / 1) | 0.859 (7 / 1), MPS |
  | LongMemEval_S (470), recall_all@5 | 0.906 | 0.904 (6 / **7**) | **0.934** (13 / 0), MPS |
  | LoCoMo (1,536), recall_any@5 | 0.921 | 0.915 (37 / **47**) | **0.935** (47 / 26), CPU |
  | qa_v2 agent accuracy (47 x 3, Haiku 5.5 reader) | 0.851 | 0.858 (20 x 4) | not run |

  Small: answerai-colbert-small-v1, 20 chats x 2 messages, `query_tokens=256 weight=2`. Large: Iso-ModernColBERT, 20 x 4, same settings; CPU and MPS gave identical ranks on all 150 search_v2 cases. `query_tokens=256 weight=2` came from SWE-CB's train split in round one, and 2 messages from search_v2's train split in round two. So the clean held-out rows are the two test-split rows and LoCoMo. LongMemEval informed one change to the message pick rule, which did not move its score.
- **The large model holds every column; the small one trades.** The small model's LongMemEval (-0.2 [-1.7, +1.3]) and LoCoMo (-0.7 [-1.9, +0.5]) drops are inside noise, but they are drops. The large model gains on both (+2.8 [+1.5, +4.3]; +1.4 [+0.3, +2.5]).
- **The gain is biggest for short queries.** Issue titles are a few words searched against long coding sessions; hit@5 rises 19 points there, and 25 on the 40 test issues.
- **By Chris's query profiles:** the large model lifts LongMemEval's two-facet questions (P3) from 0.750 to 0.850, as his article predicts. On the personal cases, the gain is in literal queries and in "diffuse" ones, where many near-duplicate chats share the query's words. There is only one concept query (P2) among the 150.
- **The costs came down a long way, but not to "every agent's server".**
  - The GPU (MPS) path is a trap on a Mac: an 8.5 GB footprint on search_v2. On CPU the large model peaks at 1.7 GB, the small one at 0.77 GB.
  - Cold rerank on search_v2: 0.42 s with the small model (20 x 2), 2.3 s with the large one on CPU. With the token-vector cache warm (small, 20 x 4), 0.1 s. Off is 0.08 s.
  - Replaying 104 real searches at live size, with the cache warm: 111 ms median with the small model and 175 ms with the large, against 124 ms off. Half those searches send several phrasings, and before the cache warms they cost the large model 3 to 9 s each.
  - The first search in a new process loads torch and the model: 8 to 9 s for the small model and 10 to 13 s for the large on the live replay.
  - Rank's index: 26 KB a message with the small model, 7 KB with token pooling 2 and int8, at the same score.
- **Default behaviour is unchanged.** With the switch off, SWE-CB, LongMemEval and search_v2 reproduce the 0.5.1 numbers exactly and torch is never imported. SWE-CB and search_v2 were checked again after round two's edit.
- **Recommendation:** don't ship it inside each agent's `recall serve`. Five open agents would hold 4 to 8.5 GB between them, and each would pay the model load. Ship it behind one shared process that holds the model (see [shared-server-and-vectors.md](shared-server-and-vectors.md)), with the large model on CPU and a warm cache, kept off the hooks.

## What changed

| File | Change |
|---|---|
| `src/recall/late.py` (new) | Settings and switch, the pylate model wrapper, MaxSim, rerank, rank, a token-vector store, the rank backfill |
| `src/recall/chats/store.py` | `search()`: the fusion loop moves into `_fuse()`. With late on, rank adds a sixth ranking before fusion, and rerank adds one after it (or reorders the candidates). `_late()` makes any model failure fall back to the hybrid |
| `src/recall/config.py` | `late = {}` in `DEFAULTS`, so the `[late]` table is read |
| `src/recall/cli.py` | `recall embed` also encodes token vectors when `mode = "rank"` |
| `pyproject.toml`, `uv.lock` | Optional extra `late = ["numpy>=2", "pylate>=1.6"]` (torch 2.11, transformers 5.3, sentence-transformers 5.3). The lock only adds packages: none removed, and no existing version changed |

No eval code changed. Evals turn the ranker on with `RECALL_LATE`, and runs are labelled with `--note`.

### The switch

```toml
[late]
mode = "rerank"                        # or "rank"; anything else: off
model = "topk-io/Iso-ModernColBERT"    # any pylate ColBERT checkpoint
candidates = 20                        # rerank: fused top chats scored
messages = 4                           # rerank: messages scored per chat
weight = 1.0                           # the late ranking's weight in the fusion
reorder = false                        # rerank: pure MaxSim order for the candidates instead of fusing
query_tokens = 0                       # 0: the model's own (32 for Iso-ModernColBERT)
document_tokens = 0                    # 0: the model's own (300)
device = "auto"                        # cuda, then mps, then cpu
store = true                           # keep token vectors; false: rerank encodes every time
pool = 1                               # merge a document's similar token vectors, this many to one
bits = 16                              # stored vectors: 16 (float16) or 8 (int8, a scale per token)
```

`RECALL_LATE` overrides the table: `RECALL_LATE=off`, `RECALL_LATE=rerank`, `RECALL_LATE="rank query_tokens=256 weight=2"`.

### How the modes work

- **Rerank.** Runs after the five existing rankings are fused, on the top `candidates` chats.
  - Each chat is scored on up to `messages` of its messages: its best BM25 hits (up to 3, from the pool search already holds), its message closest to the query's dense vector, then its other hits and close messages. A chat with neither is scored on its first messages.
  - Each message is encoded as a document and scored by MaxSim against the query's tokens. The chat takes its best message's score.
  - That order is fused as one more RRF ranking (`weight / (20 + rank + 1)`), so it can lift candidates but never drop one.
- **Rank.** Every message passing the chat filters and date window is scored by MaxSim from stored token vectors. Chats are ordered by their best message, and the top 200 are fused like the dense ranking.
  - An index in memory (the evals) encodes whatever is missing at search time.
  - An index on disk encodes at most 200 messages per search and leaves the rest to `recall embed`.
- **Store.** Token vectors are float16, one row per token, keyed by sha1 of (model, document length, text). They live in `<index>-late.db` beside the index, or in `$RECALL_HOME/late-vectors.db` for an index in memory, so each text is encoded once.
- **Local only, no LLM calls.** The model runs in-process. The only network access is Hugging Face downloading the checkpoint on first use: 298 MB for Iso-ModernColBERT, 134 MB for answerai-colbert-small-v1. After that it works with `HF_HUB_OFFLINE=1` [observed].
- **stdout.** `recall serve` speaks MCP on stdout, so model loading and encoding write to stderr. Over stdio, every stdout line stayed JSON-RPC with the model loaded [observed].

## Results

### SWE-ContextBench Lite: 99 issues searched against all 300 sessions, hybrid (nomic) plus late

The baseline is this branch with late off: hit@5 0.798, hit@1 0.586, hit@10 0.929, MRR 0.696, the same as 0.5.1. Each Δ is paired against it, with the harness's bootstrap 95% interval and the count of issues better and worse.

**Settings were picked on the train split (59 issues, baseline 0.729):**

| Setting | hit@5 | Δ | better / worse |
|---|---|---|---|
| `rerank` with the model's defaults (32 query tokens) | 0.763 | +3.4 | 2 / 0 |
| `rerank query_tokens=256` | 0.797 | +6.8 [+1.7, +13.6] | 4 / 0 |
| ... `weight=2` (chosen) | 0.814 | +8.5 [+1.7, +16.9] | 5 / 0 |
| ... `candidates=40` | 0.814 | +8.5 | 5 / 0 |
| ... `reorder=true` | 0.814 | +8.5 | 6 / 1 |
| ... `weight=0.5`, `candidates=10`, `document_tokens=512` | 0.763 to 0.797 | | |

**All 99 issues, and the held-out test split:**

| Arm | Split | hit@5 | Δ hit@5 | better / worse | hit@1 | MRR | search p50 / p95 |
|---|---|---|---|---|---|---|---|
| late off | all | 0.798 | | | 0.586 | 0.696 | 53 / 203 ms |
| `rerank` (defaults) | all | 0.828 | +3.0 [+0.0, +7.1] | 3 / 0 | 0.657 | 0.743 | |
| `rerank query_tokens=256 weight=2`, cold | all | 0.859 | +6.1 [+1.0, +12.1] | 7 / 1 | 0.626 | 0.736 | 1,546 / 1,675 ms |
| same, pure MaxSim reorder | all | 0.848 | +5.1 [-1.0, +12.1] | 8 / 3 | 0.636 | 0.739 | |
| `rank` (defaults) | all | 0.859 | +6.1 [+2.0, +11.1] | 6 / 0 | 0.657 | 0.749 | 76 / 167 ms |
| `rank query_tokens=256` | all | 0.869 | +7.1 [+2.0, +13.1] | 8 / 1 | 0.636 | 0.737 | 109 / 187 ms |
| small model, `rerank` tuned, CPU, cold | all | 0.848 | +5.1 [+1.0, +10.1] | 6 / 1 | 0.646 | 0.739 | 497 / 685 ms |
| small model, `rank`, CPU | all | 0.859 | +6.1 [+1.0, +12.1] | 7 / 1 | 0.626 | 0.722 | 70 / 164 ms |
| late off | test | 0.900 | | | 0.625 | 0.753 | |
| `rerank query_tokens=256 weight=2` | test | 0.925 | +2.5 [-5.0, +10.0] | 2 / 1 | 0.650 | 0.783 | |

"Cold" means every candidate is encoded at query time (`store=false`). Other latencies are with the store already holding the vectors. The first search, which loads the model, is left out of p50 and p95.

### LongMemEval_S: 470 questions, both sides indexed, hybrid (nomic) plus late

The baseline is late off: recall_any@5 0.987, recall_all@5 0.906, recall_all@10 0.955, the same as 0.5.1. Each Δ is paired per question (bootstrap 95% interval, 2,000 resamples).

| Arm | recall_any@5 | recall_all@5 | Δ recall_all@5 | better / worse | recall_all@10 | ndcg_any@10 | search p50 / p95 |
|---|---|---|---|---|---|---|---|
| late off | 0.987 | 0.906 | | | 0.955 | 0.942 | 9 / 26 ms |
| `rerank query_tokens=256 weight=2`, store warm | 0.996 | **0.934** | **+2.8 [+1.5, +4.3]** | **13 / 0** | 0.970 | 0.962 | 70 / 90 ms |
| same, cold (`store=false`) | 0.996 | 0.932 | +2.6 [+1.3, +4.0] | 12 / 0 | 0.970 | | 4,200 / 5,360 ms, sharing the GPU, with 13 embedding timeouts (those questions ran as keywords plus late); 2,218 / 2,417 ms with the earlier pick rule |
| `rank query_tokens=256`, store warm | 0.989 | 0.921 | +1.5 [+0.4, +2.6] | 7 / 0 | 0.968 | 0.956 | 151 / 182 ms |
| same, encoding as it went | 0.989 | 0.919 | | 7 / **1** | | | |
| small model, `rerank` tuned, CPU, cold | 0.989 | 0.915 | +0.9 [-0.6, +2.3] | 9 / 5 | 0.970 | 0.947 | 1,434 / 1,801 ms |
| small model, the same with 2 messages a chat (round two) | 0.989 | 0.904 | -0.2 [-1.7, +1.3] | 6 / **7** | 0.964 | | |

Cold and warm runs of the same setting differ by a question or so: vectors encoded fresh on MPS and vectors read back from the float16 store are not bit-identical.

- **By question type** (off to rerank): multi-session 0.843 to 0.884, temporal reasoning 0.827 to 0.866, preference 0.933 to 1.000. The other types were already at or near 1.000.
- **Rerank beats rank** by 1.3 points of recall_all@5, paired [+0.4, +2.3]: 6 questions better, none worse.
- **Ceiling.** The hybrid already has every answer session in its top 30 for 99.4% of questions, so a top-20 pool rarely limits rerank.
- **The two message-pick rules scored the same.** An earlier pick rule (up to 4 BM25 hits) gave the same recall_all@5 as the current one (3 hits plus the closest message by meaning).

## Round two

### Personal search cases (search_v2)

150 hand-labelled searches over a frozen copy of my own index (2,798 chats, 20,951 messages), 90 train and 60 test. The baseline is late off: hit@5 0.900, hit@1 0.687, MRR 0.774, p50 77 ms. Every setting below is paired against it; better / worse counts hit@5.

| Setting | hit@5 | Δ hit@5 | better / worse | hit@1 | MRR | p50 | Peak footprint |
|---|---|---|---|---|---|---|---|
| Large, MPS, 20 x 4, `q256 w2` | 0.947 | +4.7 [+2.0, +8.0] | 7 / 0 | 0.713 | 0.810 | 1,406 ms | **8.5 GB** |
| Large, CPU, the same | 0.947 | +4.7 | 7 / 0 | 0.713 | 0.810 | 2,307 ms | 1.7 GB |
| Small, CPU, the model's own settings | 0.927 | +2.7 [+0.7, +5.3] | 4 / 0 | 0.713 | 0.799 | 803 ms | 0.84 GB |
| Small, CPU, 20 x 4, `q256 w2` | 0.953 | +5.3 [+2.0, +9.3] | 8 / 0 | 0.733 | 0.817 | 735 ms | 0.84 GB |
| ... 20 x 2 (**chosen**) | 0.953 | +5.3 [+2.0, +9.3] | 8 / 0 | 0.740 | 0.821 | 422 ms | 0.77 GB |
| ... 10 x 4 | 0.927 | +2.7 | 4 / 0 | 0.713 | 0.801 | 399 ms | 0.91 GB |
| ... 10 x 2 | 0.927 | +2.7 | 4 / 0 | 0.720 | 0.805 | 305 ms | 0.72 GB |
| ... 20 x 4, token pooling 2 | 0.940 | +4.0 | 7 / **1** | 0.733 | 0.816 | 782 ms | 0.84 GB |
| ... 20 x 4, int8 vectors | 0.953 | +5.3 | 8 / 0 | 0.720 | 0.813 | 729 ms | 0.84 GB |
| ... 20 x 4, store filling | 0.953 | +5.3 | 8 / 0 | 0.733 | 0.817 | 469 ms | 1.1 GB |
| ... 20 x 4, store warm | 0.953 | +5.3 | 8 / 0 | 0.733 | 0.817 | **100 ms** | 0.55 GB |
| Small, `rank`, `q256` | 0.953 | +5.3 | 8 / 0 | 0.727 | 0.816 | 645 ms | 2.6 GB |
| ... token pooling 2 and int8 | 0.953 | +5.3 | 8 / 0 | 0.733 | 0.821 | 827 ms | 1.2 GB |

- **Held out.** `q256 w2` came from SWE-CB's train split in round one, so all 150 cases are held out for the 20 x 4 settings. On the 60 test cases: large 0.867 to 0.933 (+6.7 [+1.7, +13.3], 4 / 0), small 0.867 to 0.950 (+8.3 [+1.7, +16.7], 5 / 0).
- **Choosing 20 x 2.** I picked it from the train split alone: MRR 0.823 against 0.817 for 20 x 4, at 0.42 s instead of 0.74 s. On test it scores the same as 20 x 4 (0.950).
- **By tag** (small, 20 x 2), hit@5: fix 0.811 to 0.946 (37 cases), buried 0.781 to 0.875 (32), paraphrase 0.889 to 1.000 (18). Near-duplicate stays at 0.981, but its MRR rises from 0.835 to 0.907 (52): the right chat moves up among its look-alikes.
- **CPU against MPS.** The two give the same ranks. MPS is faster for the large model but compiles and keeps a graph for every batch shape, which reached 8.5 GB here (trap 4 below).

### Short queries: SWE-CB with the issue title as the query

The title is the issue's first line, a few words against whole coding sessions. Keyword and hybrid search both struggle here (hit@5 0.414 and 0.485).

| Arm | hit@5 | Δ hit@5 | better / worse | hit@1 | hit@10 | MRR |
|---|---|---|---|---|---|---|
| late off | 0.485 | | | 0.313 | 0.727 | 0.415 |
| small, CPU, 20 x 2 | 0.677 | +19.2 [+12.1, +26.3] | 19 / 0 | 0.343 | 0.788 | 0.477 |
| large, MPS, 20 x 4, store warm | 0.697 | +21.2 [+14.1, +29.3] | 21 / 0 | 0.343 | 0.818 | 0.475 |
| small, `rank`, `q256` | 0.646 | +16.2 [+9.1, +23.2] | 16 / 0 | 0.333 | 0.778 | 0.466 |

### By Chris's query profiles

Section 2 of this experiment labelled every case by rules over words, with no model: P1 literal (one query word singles out the target), P3 conjunction (two words jointly do), P2 concept (the target holds under half the query's weight), and unlabelled (the target holds the words, but so do several other chats). The labels are the same for every arm. Cells are MRR / hit@5 (LongMemEval: recall_all@5).

| Benchmark | Profile | n | Off | Small | Large |
|---|---|---|---|---|---|
| search_v2 | P1 | 22 | 0.859 / 0.955 | 0.933 / 1.000 | 0.941 / 1.000 |
| | P3 | 35 | 0.758 / 0.943 | 0.774 / 1.000 | 0.777 / 1.000 |
| | unlabelled | 92 | 0.757 / 0.870 | 0.810 / 0.924 | 0.789 / 0.913 |
| | P2 | 1 | | | |
| SWE-CB titles | P1 | 50 | 0.571 / 0.640 | 0.644 / 0.900 | 0.639 / 0.900 |
| | P2 | 28 | 0.118 / 0.143 | 0.159 / 0.214 | 0.160 / 0.250 |
| | P3 | 17 | 0.506 / 0.647 | 0.585 / 0.882 | 0.584 / 0.882 |
| LongMemEval_S | P1 | 389 | 0.938 | 0.925 | 0.954 |
| | P2 | 12 | 0.583 | 0.667 | 0.667 |
| | P3 | 60 | 0.750 | 0.800 | **0.850** |

Small here is 20 x 2 for search_v2 and LongMemEval and 20 x 4 for titles (the titles score the same with 20 x 2). Large is 20 x 4 on CPU for search_v2 and warm on MPS for the others. Unlabelled cases are left out of the title and LongMemEval rows (4 and 9; neither moves).

- LongMemEval's P3 is where Chris's result shows most clearly: the large model lifts two-facet questions by 10 points.
- The small model loses 1.3 points on LongMemEval's literal questions (P1), which is where its overall LongMemEval drop comes from.
- Real coding-chat queries are rarely concept queries. In my own cases the target almost always holds the query's words; what is hard is telling it apart from near-duplicates, and that is where the unlabelled group gains.

### Guards: LoCoMo and the qa_v2 agent eval

| Arm | LoCoMo recall_any@5 | Δ | better / worse | recall_all@5 | Δ |
|---|---|---|---|---|---|
| late off | 0.921 | | | 0.801 | |
| small, CPU, 20 x 2 | 0.915 | -0.7 [-1.9, +0.5] | 37 / **47** | 0.796 | -0.5 [-1.8, +0.7] |
| small, CPU, 20 x 4 | 0.911 | -1.0 [-2.2, +0.1] | 34 / **50** | 0.796 | -0.5 [-1.7, +0.7] |
| large, CPU, 20 x 4 | 0.935 | +1.4 [+0.3, +2.5] | 47 / 26 | 0.820 | +2.0 [+0.7, +3.2] |

The small model's LoCoMo loss is in single-hop questions (category 4, 841 questions, -1.7 points).

qa_v2 runs Claude Code headless with Haiku 5.5 and only recall's tools, on 47 test questions x 3 repeats, graded by Sonnet 5.5. Same code and day, each arm on its own copy of the frozen home. The late arm used the small model at 20 x 4; its server's token store grew to 244 MB during the run, so late interaction was on.

| Arm | Accuracy | Answerable (38) | Unanswerable (9) | Median tokens in | Median seconds a question |
|---|---|---|---|---|---|
| late off | 0.851 [0.766, 0.922] | 0.851 | 0.852 | 40.7k | 14.2 |
| small, CPU | 0.858 [0.773, 0.929] | **0.833** | 0.963 | 42.7k | 23.1 |

- The difference is inside noise: a single pair needs about 8 points to clear it (28 Oct noise note). Answerable questions dropped 1.8 points; unanswerable ones rose.
- Each question starts a fresh agent and server, so every question paid the model load: that is most of the 9 extra seconds.
- Tool errors: 51 in each arm. In the off arm, 50 are the agent asking for disabled built-ins (Bash, Grep, Glob) and 1 for Read. In the late arm, 49 are built-ins and 2 are recall calls rejected for a missing argument (`read_note` and `project_memory`) before recall's code ran. No recall call failed inside recall.

### Real searches at live size

Every distinct `search_chats` and CLI search in the live usage log (104, from 9 Oct to 2 Nov), replayed on a read-only copy of the live index (3,719 chats, 26,232 messages) the way the MCP tool runs them. Half of them (51) send several phrasings, each searched and reranked on its own, then merged. "Fill" starts with an empty token store and keeps what it encodes; "warm" runs again on the full store.

| Arm | p50 | p95 | p50, 1 phrasing | p50, several | First search | Peak footprint | Same #1 | Opened chat: up / down |
|---|---|---|---|---|---|---|---|---|
| late off | 124 ms | 350 ms | 61 ms | 259 ms | 1.1 s | 0.09 GB | | |
| small, 20 x 2, cold | 462 ms | 2,063 ms | 405 ms | 1,670 ms | 9.4 s | 0.83 GB | 82% | 6 / **10** |
| small, fill | 420 ms | 1,510 ms | 357 ms | 860 ms | 8.8 s | 0.88 GB | | |
| small, warm | 111 ms | 445 ms | 84 ms | 351 ms | 8.0 s | 0.55 GB | 82% | 6 / **10** |
| large, 20 x 4, fill | 1,838 ms | 6,750 ms | 1,416 ms | 3,022 ms | 12.7 s | 1.7 GB | 88% | 7 / 4 |
| large, warm | 175 ms | 479 ms | 91 ms | 385 ms | 10.0 s | 1.1 GB | 88% | 7 / 4 |
| large, cold (first 22 searches only: 15 with one phrasing, 7 with several) | | | 2,189 ms | 8,630 ms | | | | |

- **Relevance.** There are no labels for these searches. The nearest thing is the chat the agent opened with `read_chat` within 15 minutes, one of that search's results (48 searches have one). It favours the old order, because the agent chose from it. Even so, the large model moved that chat up in 7 searches and down in 4 (MRR 0.587 to 0.603); the small model moved it up in 6 and down in 10 (0.582).
- Both rerankers change about one result in eight in the top 8.
- **Store.** After the 104 searches: small 138 MB for 4,313 messages (32 KB each), large 311 MB for 6,423 messages (48 KB each), both float16 without pooling.
- I stopped the large model's cold run after 22 searches: several phrasings, each encoding its own 80 messages on CPU, took up to 13 s.

### A torch-free encoder

The small model exports to ONNX with identical vectors (cosine 1.0000 on 256 real messages). Measured on CPU:

| | torch (pylate) | ONNX Runtime, fp32 |
|---|---|---|
| Import and load | 6.4 to 9.3 s | 0.15 to 1.0 s |
| Documents a second | 72 (batch 32), 97 to 109 (batch 8) | 35 (batch 32), 49 (batch 8) |
| Peak memory, 128 documents | 0.93 GB (batch 32), 0.73 GB (batch 8) | 1.3 GB (batch 32), 0.50 GB (batch 8) |
| Installed size | torch alone 354 MB, about 830 MB with its dependencies | onnxruntime 75 MB |

- Dynamic int8 quantization of the ONNX model is not usable: top-10 overlap with the float model is 0.78 and it is no faster.
- ONNX Runtime does not use the Mac's matrix units, so it encodes at half torch's speed. Its gains are the load time and the install.
- Batches of 8 instead of 32 are smaller and faster in both. `late.py` still uses 32.

## Round three: SMART, controls and the dense weight

### What was added

- **SMART** (Zhang et al., arXiv 2605.24938) reads late interaction out of a single-vector embedder. The final layer's hidden state of every token is normalised; a message scores its pooled cosine plus the mean, over query tokens, of each one's best match among the message's tokens. `late.py` runs it for `model = "smart:<repo>"` from the repo's ONNX export (onnxruntime, no torch). With `nomic-ai/nomic-embed-text-v1.5`, the model recall's dense side runs through Ollama, the ONNX pooled vector matched recall's stored vectors at cosine 1.0000 on every sample.
- **A pooled-only control** (`smart = 0`) rescores the same candidates by the pooled vector alone. Because a candidate's closest message by meaning is always among those scored, this is close to adding the dense ranking a second time, at weight 2, over the top 20.
- **A sweep of the dense ranking's fusion weight** (`WEIGHTS["dense"]` in `store.py`, 1 today), late interaction off, set in-process with no product change.
- **Known-item search**, the older personal eval behind the choice of weight 1: 150 queries of three words copied from a chat's opening message, plus 8 hand-written ones.

Every arm: 20 candidate chats x 4 messages, fusion weight 2, the settings frozen in round one. CPU unless noted; the large model's titles and LongMemEval arms ran warm on MPS (identical ranks to CPU on search_v2).

### Results

hit@5 unless noted, Δ against late off; better / worse in brackets where paired.

| Arm | search_v2 (150) | SWE-CB whole issue | SWE-CB titles | LongMemEval all@5 | LoCoMo any@5 | Known-item MRR (hit@1) |
|---|---|---|---|---|---|---|
| late off, dense weight 1 | 0.900 | 0.798 | 0.485 | 0.906 | 0.921 | 0.856 (0.793) |
| dense weight 2 | 0.927 (5/1) | 0.818 (2/0) | 0.626 (14/0) | 0.909 (3/2) | 0.923 (22/20) | 0.797 (0.693) |
| dense weight 3 | 0.953 (8/0) | 0.838 (4/0) | 0.697 (21/0) | 0.911 (6/4) | 0.914 (26/**37**) | 0.720 (0.593) |
| dense weight 4 | 0.960 (9/0) | 0.828 (4/1) | 0.747 (26/0) | 0.913 (9/6) | 0.911 | 0.647 (0.533) |
| pooled-only rerank (control) | 0.940 (7/1) | 0.848 (5/0) | 0.667 (18/0) | not run | 0.915 (26/**36**) | 0.805 (0.713) |
| SMART, nomic-embed-text-v1.5 | 0.933 (6/1) | 0.848 (6/1) | 0.657 (17/0) | 0.919 (6/0) | 0.926 (35/28) | 0.810 (0.720) |
| small ColBERT | 0.953 (8/0)* | 0.848 (6/1)* | 0.677 (19/0) | 0.904 (6/**7**)* | 0.915 (37/**47**)* | 0.813 (0.713)* |
| Iso-ModernColBERT | 0.947 (7/0) | 0.859 (7/1) | 0.697 (21/0) | **0.934 (13/0)** | **0.935 (47/26)** | **0.867 (0.807)** |

\* 20 x 2 messages. LoCoMo recall_any@1 also falls with the dense weight: 0.697, 0.687, 0.679, 0.670 at weights 1 to 4 (Iso: 0.742). LongMemEval improves on every column at every weight (all@10 0.955 to 0.962–0.964, ndcg_any@10 0.942 to 0.944–0.946).

- **Choosing a weight on train splits only:** search_v2 train hit@5 0.922 → 0.933 / 0.967 / 0.967 at weights 2 / 3 / 4; SWE-CB train 0.729 → 0.763 / 0.780 / 0.780 (whole issue) and 0.441 → 0.559 / 0.644 / 0.712 (titles). Weight 3 or 4 wins on train; on test, search_v2 0.867 → 0.933 / 0.950 and titles 0.550 → 0.775 / 0.800 at weights 3 / 4. Weight 2 is the only one that holds LoCoMo's recall_any@5, and no weight holds known-item or LoCoMo's top-1.
- **By query profile** (recall_all@5 on LongMemEval's 60 two-facet questions): weight 3 0.783, weight 4 0.800, SMART 0.800, Iso 0.850, from 0.750. On search_v2 and the titles, weight 4 matches or beats Iso on every profile, two-facet included.
- **SMART against its control:** its token term adds nothing on search_v2 (hit@5 0.933 against 0.940; MRR 0.809 against 0.799), nothing on whole issues, and a point less on titles. It adds on LoCoMo (+0.5 against -0.7 for the control). On LongMemEval, with no control run, it beats every dense weight (+1.3 against +0.6 at best). Its known-item cost equals the control's.
- **SMART's cost:** nomic is 137M parameters at 768 dimensions, so a token vector is 6 times ColBERT's 128 dimensions: about 0.5 MB a message in float16 against 48 KB (Iso). ONNX on CPU encodes about 12 messages a second (355 tokens on average), so a cold rerank takes 3.6 s at the median; warm, 0.13 s. Peak footprint 2.3 GB, and onnxruntime replaces torch.
- **How SMART was run, against the paper:** the paper scores every candidate with its hybrid score. Here it rescored the hybrid's top 20 chats (each by its best of 4 messages) and that order joined recall's fusion as one more ranking at weight 2, settings frozen for ColBERT in round one. Inference-only, no adapter, nomic-embed-text-v1.5 through its ONNX export.
- **How representative the known-item cost is:** those queries are synthetic, three words copied verbatim from a chat's opening message, sampled with a fixed seed (the same 150 in every arm). Literal queries in search_v2, written as a person would, improved at weights 3 and 4. The cost may be specific to verbatim fragments; replaying logged real searches at each weight (top-8 churn, the opened chat, queries quoting chat text) is the test for a product change.
- **Not run:** a ColBERT rerank on top of a higher dense weight; the pooled control on LongMemEval; the subagent cases (`subagent_search.jsonl`), because the frozen eval home holds no Claude subagents.

### What it means for recall

- **A higher dense weight is free and real**, on natural-language and short coding searches, and **costs exact-wording lookups**: known-item MRR 0.856 → 0.797 at weight 2, 0.720 at 3. That is the trade the weight of 1 was set for in October, and it still holds. A product change needs Ali's call on that trade, and its own branch and gate.
- **The large ColBERT is the only arm that holds every column**, including known-item and both chat benchmarks, which makes it the one worth the engineering (a shared process, an index, compression), if any is.
- **SMART is not a substitute for a trained ColBERT here.** It is the cheapest way to get some late interaction without a second model, but its gains are confined to conversational memory and are half the size.

## Costs

| | Iso-ModernColBERT | answerai-colbert-small-v1 |
|---|---|---|
| Parameters, vector size | ~149M, 128 dims | ~33M, 96 dims |
| Documents encoded per second (136-token messages) | 19 to 46 on MPS, 14.5 on CPU | 65 on MPS, 138 on CPU |
| Query encoding | 35 to 100 ms on MPS, 9 to 40 ms on CPU | 9 ms on CPU |
| `recall serve` footprint after two searches (83 MB with late off) | 1.9 to 2.3 GB on MPS, 1.0 GB on CPU | 0.7 GB on CPU |
| First search in a new process (import and load) | 8 to 14 s | 8 s |

| Rank's index | Tokens per message | Per 1,000 messages | Dense vectors per 1,000 |
|---|---|---|---|
| SWE-CB coding sessions | 244 | 59.6 MB | ~1.6 MB |
| LongMemEval_S (180,645 messages, 5.85 GB) | 136 | 33.2 MB | ~1.5 MB |

For a 26k-message coding index that means:
- about 1.6 GB of token vectors;
- a 13 to 22 minute backfill on MPS;
- at least 0.8 to 1.3 s per rank search. That figure comes from a synthetic store of the same size: about half reading the blobs, half MaxSim. The real code also reads and hashes every message's text on each search, so it is a floor.

Load averages were between 5 and 17 during these runs, with other jobs on the machine, so timings are indicative.

## Traps found along the way

Each of these would have quietly spoiled the numbers.

1. **bfloat16 on CPU.** Iso-ModernColBERT's checkpoint loads as bfloat16. On this Mac's CPU, 32 documents took 29 s, against about 1 s on MPS [observed]. `late.py` forces float32 on CPU and float16 elsewhere.
2. **32 query tokens.** The model reads 32 query tokens and does not expand queries. SWE-CB's queries are whole GitHub issues (median 306 tokens), so every query was cut to its first sentence. `query_tokens = 256` was worth +3.4 points on train. LongMemEval's questions are short, so the setting doesn't matter there.
3. **300 document tokens.** 86% of SWE-CB messages run past 300 tokens, so the model sees only their opening. 512 tokens did no better on train. At 1,024, MPS ran out of memory in batches of 32; the searches fell back to the hybrid, as designed.
4. **MPS memory grows.**
   - The first rank run over LongMemEval reached a 27 GB footprint and pushed the machine into swap.
   - MPS keeps a compiled graph for every input shape, and pylate pads each batch only to its longest text. `torch.mps.empty_cache()` does not release these graphs [observed].
   - Padding every batch to a multiple of 64 tokens cut the growth. Over 5,000 documents, driver memory rose to 2.5 GB with padding against 5.4 GB without, at the same speed, and all 99 SWE-CB ranks stayed identical. pylate already does the same on Habana HPUs, padding to a power of two [src: lightonai/pylate@1.6.0:pylate/models/colbert.py, `encode`].
   - Even with padding, a full LongMemEval rank run still grew from 2.8 to 7.1 GB, because batch sizes vary. A long-lived server on MPS would need a restart policy or the CPU.

## What reviewers should look at

- **`store.search()`.**
  - The refactor into `_fuse()` must leave the off path exactly as it was. It does on both benchmarks, but read it anyway.
  - The late weight is added to the `weights` dict that `explain` returns.
- **`jit.py` reads `explain`.**
  - Summing weights over active rankings works with the extra `late` entry.
  - `reorder = true` lifts candidates past the fused score scale, which would skew the hook's margins.
  - The hook passes `vector=False`, so rerank there gets no closest-by-meaning pick.
  - With late on, the hook would also load the model, which a hook cannot afford.
- **`late._by_best()` and `late.ranking()`.** Check the store's keys, the inline limit for an index on disk, and the chunked MaxSim (`np.maximum.reduceat` over token offsets).
- **Thread safety.** One module lock serialises encoding and store access across the server's worker threads.
- **`_bucketed()`.** It wraps pylate's `tokenize` on MPS only. Padding is masked out of documents, and out of queries unless the model expands queries itself.
- **`_packed()` and `_unpacked()` (round two).** With `bits = 8`, each token row is a float16 scale then int8 values. Vectors are packed and unpacked even when nothing is stored, so `store=false` arms score exactly what a stored vector would give. `pool` and `bits` join the store key only when they differ from the defaults, so round one's stores stay valid.
- **Blockers before any release, even opt-in:**
  - `[late]` in config.toml reaches every `store.search` caller, including `jit.py`, which runs in a fresh hook process on every prompt and would load torch and the model each time (8 to 13 s on the live replay). Late interaction has to be limited to the `search_chats` tool.
  - The store is never pruned, so vectors of a forgotten chat's messages stay in `*-late.db` until the file is deleted. `forget` must clear them.
  - Loading the model checks Hugging Face over the network. It should run with the hub offline once the model is cached.
- **Not handled yet:**
  - SMART needs onnxruntime, which is not in the `late` extra or the lockfile (it was installed into the venv by hand); `uv sync` removes it and SMART turns itself off with a warning;
  - SMART's token vectors are 768-dim, about 0.5 MB a message in float16: with `store = true` its cache reached 4.9 GB over search_v2's candidates;
  - rank on an index on disk reads every message's vectors from SQLite on every search;
  - `BATCH` is 32; 8 is smaller and faster on CPU;
  - there is no `changes/` fragment.

## Reproducing it

Both datasets are described in [coding-memory-hillclimb.md](coding-memory-hillclimb.md#reproducing-it). Use a scratch `RECALL_HOME` with an `[embeddings]` table, and pass the same extras on every `uv run`, or uv resyncs the venv and drops pylate.

```bash
RECALL_LATE="rerank query_tokens=256 weight=2" uv run --extra late --extra embeddings --with pyarrow \
  recall eval swe-contextbench --data DIR --scope all --hybrid --split all --note "rerank q256 w2" --against BASELINE_RUN
RECALL_LATE="rank query_tokens=256" uv run --extra late --extra embeddings \
  recall eval longmemeval --cases longmemeval_s_cleaned.json --hybrid
RECALL_LATE=rank uv run --extra late --extra embeddings recall embed     # rank's backfill on an index on disk
```

Add `store=false` to `RECALL_LATE` for cold rerank timings. Latencies here came from a throwaway wrapper that timed `store.search` inside the same CLI.

Round two ran each arm through the eval team's `run.sh` on a copy of the frozen eval home, one arm at a time, with a watchdog that sampled the process tree's footprint every 5 s:

```bash
RECALL_LATE="rerank model=answerdotai/answerai-colbert-small-v1 device=cpu query_tokens=256 weight=2 messages=2" \
  recall eval search --v2 --split all --against BASELINE_RUN
recall eval swe-contextbench --data DIR --scope all --query title --hybrid --split all    # the title runs
recall eval locomo --cases locomo10.json --hybrid
recall eval agent --split test --repeats 3 --workers 2                                    # qa_v2, one arm per setting
```

The real-search replay and the ONNX measurements were throwaway scripts. The replay read the live usage log, ran each logged `search_chats` and CLI search the way the MCP tool does (every phrasing, then merged), and made relative windows absolute from the time of the original search. It ran against a read-only copy of the live index.

## References

- [chris]: https://chrissimpson.co.uk/near-both-on-neither-single-vector-retrieval.html, code at https://github.com/simplechris/near-both-on-neither-single-vector-retrieval (BM25 via `bm25s`, dense via Qwen3-Embedding-0.6B, late via Iso-ModernColBERT and `pylate`, on about 1,200 semi-synthetic SaaS tools)
- pylate: https://github.com/lightonai/pylate (1.6.0)
- Iso-ModernColBERT: https://huggingface.co/topk-io/Iso-ModernColBERT
- answerai-colbert-small-v1: https://huggingface.co/answerdotai/answerai-colbert-small-v1
