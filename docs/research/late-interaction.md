# Late interaction on top of recall's hybrid search

Research note for `recall`, 2026-11-02. Question: does late interaction (a ColBERT model's one vector per token, scored by MaxSim) still add anything on top of recall's tuned BM25 plus dense fusion? Chris Simpson found that it keeps two-facet queries intact where a single dense vector averages them away, and noted that production stacks are hybrids [chris]. Method: an opt-in ranker in `search()`, in two modes, measured on SWE-ContextBench Lite and LongMemEval_S against the current hybrid on one Apple Silicon Mac (M4 Pro, 24 GB). Grades: `[observed]` for my measurements, `[src: ...]` for source read at a pinned version.

This note covers building the ranker and a first measurement. A per-query-profile run in Chris's format (literal, concept, conjunction) comes next and decides whether it ships.

## Answer

- **Yes, on both benchmarks, by a few points.**
  - Rerank, the fused top 20 chats scored again at query time, lifts SWE-CB Lite hit@5 from 0.798 to 0.859 (+6.1 [+1.0, +12.1]).
  - It lifts LongMemEval_S recall_all@5 from 0.906 to 0.934 (+2.8 [+1.5, +4.3]): 13 questions better, none worse.
  - Rank, a sixth ranking over stored token vectors, reaches 0.869 and 0.921.
- **The gain is not yet proven on held-out data.**
  - The settings (256 query tokens, weight 2) were picked on SWE-CB's train split.
  - SWE-CB's held-out test split (40 issues, already at 0.900) shows +2.5 [-5, +10].
  - LongMemEval is a guard, not a target.
- **The costs are large.**
  - Each agent's MCP server grows from 83 MB to between 0.7 and 2.3 GB, and its first search takes 8 to 14 s while torch and the model load.
  - A cold rerank takes 1.5 to 2.2 s on MPS.
  - Rank stores 33 to 60 MB per 1,000 messages: 22 to 38 times the dense vectors, and about 1.6 GB for a 26k-message index.
- **Default behaviour is unchanged.** With the switch off, both benchmarks reproduce the 0.5.1 numbers exactly and torch is never imported. Without the optional extra installed, recall prints one warning and searches as before.
- **Recommendation for the next run:** test rerank fully, with rank as the second arm. If late interaction wins there, it should ship only behind one shared process holding the model (see [shared-server-and-vectors.md](shared-server-and-vectors.md)), not inside every agent's server.

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
| `rerank query_tokens=256 weight=2` | 0.996 | **0.934** | **+2.8 [+1.5, +4.3]** | **13 / 0** | 0.970 | 0.962 | 2,218 / 2,417 ms cold; 70 / 90 ms warm |
| `rank query_tokens=256` | 0.989 | 0.921 | +1.5 [+0.4, +2.6] | 7 / 0 | 0.968 | 0.956 | 151 / 182 ms |
| small model, `rerank` tuned, CPU, cold | 0.989 | 0.915 | +0.9 [-0.6, +2.3] | 9 / 5 | 0.970 | 0.947 | 1,434 / 1,801 ms |

- **By question type** (off to rerank): multi-session 0.843 to 0.884, temporal reasoning 0.827 to 0.866, preference 0.933 to 1.000. The other types were already at or near 1.000.
- **Rerank beats rank** by 1.3 points of recall_all@5, paired [+0.4, +2.3]: 6 questions better, none worse.
- **Ceiling.** The hybrid already has every answer session in its top 30 for 99.4% of questions, so a top-20 pool rarely limits rerank.
- **The two message-pick rules scored the same.** An earlier pick rule (up to 4 BM25 hits) gave the same recall_all@5 as the current one (3 hits plus the closest message by meaning).

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
- **Not handled yet:**
  - the store is never pruned, so text from a forgotten chat stays in `*-late.db` until the file is deleted;
  - rank on an index on disk reads every message's vectors from SQLite on every search;
  - there is no `changes/` fragment, because nothing ships unless the profile run shows a win.

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

## References

- [chris]: https://chrissimpson.co.uk/near-both-on-neither-single-vector-retrieval.html, code at https://github.com/simplechris/near-both-on-neither-single-vector-retrieval (BM25 via `bm25s`, dense via Qwen3-Embedding-0.6B, late via Iso-ModernColBERT and `pylate`, on about 1,200 semi-synthetic SaaS tools)
- pylate: https://github.com/lightonai/pylate (1.6.0)
- Iso-ModernColBERT: https://huggingface.co/topk-io/Iso-ModernColBERT
- answerai-colbert-small-v1: https://huggingface.co/answerdotai/answerai-colbert-small-v1
