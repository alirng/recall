# Benchmark results

Every figure recall publishes (README, [docs/benchmarks.md](../benchmarks.md), rererecall.com) is
one row in [results.jsonl](results.jsonl), together with the figures behind it and every other
system's published figure we compare against. A row is one figure: a metric of a benchmark,
measured on a known code version, with its model, judge, harness, conditions, date and the run
folders it came from. Rows are appended and never edited. The charts read their numbers from the
store, so nothing is copied by hand.

| File | What it is | Edited by |
| --- | --- | --- |
| `registry.json` | every benchmark and metric: unit, direction, public or private corpus, the command that runs it, and which fields must match for two figures to compare | hand |
| `results.jsonl` | the store, one row per figure ([schema.json](schema.json)) | `store.py record` / `add` only |
| `charts.json` | which rows each chart draws; holds no numbers | hand |
| `figures.json` | every chart with its numbers filled in and the row id behind each mark | `make_charts.py` |
| `*.svg` | the charts, light and dark | `make_charts.py` |
| `store.py` | reader, queries, recorder and checks (standard library, plus jsonschema for `check`) | |

## A row

```json
{"id": "e784aa6c526f", "kind": "measured", "date": "2026-10-16", "benchmark": "swecb-lite-retrieval",
 "metric": "hit@3", "value": 0.768, "ci": [0.677, 0.848], "n": 99, "repeats": 1, "split": "all",
 "system": "recall", "code": {"release": "0.4.1", "git": "144938f", "prod": "619344aa"},
 "conditions": {"scope": "all", "search": "hybrid"}, "runs": ["20261016-111052-release-swecb-041"],
 "public": true, ...}
```

Every row carries every field, in a fixed order; `null` (or `[]`) means unknown or not
applicable. The fields that matter most:

- **kind**: `measured` (a `recall eval` run), `delta` (a paired difference: `control` and
  `candidate` name the two rows, `value` and `ci` are candidate minus control), or `third_party`
  (copied from a publication: `source` URL, `source_detail`, `grade` vendor / paper / independent
  / mixed, and `reported`, the figure as the source prints it).
- **value** is in the metric's unit, fractions 0 to 1 for rates. `ci` is the 95% bootstrap
  interval over cases, `by_repeat` each repeat's value, `range` a spread when only that is known.
- **code**: `prod` is the run wrapper's product-code hash from `meta.txt`. `store.py` hashes git
  refs the same way (`prod_hash`), so `release` is the tag whose product code the run had, apart
  from `__version__`: release runs measure the commit before the version bump. `git` is a commit
  with exactly that code. A row added by hand has no `prod`, and its `release` is as published.
- **conditions**: the benchmark's knobs as `registry.json` declares them (search, scope, arm,
  context, subset, question type...). A breakdown (by history length, by question type) is a row
  per value with that condition set; the aggregate row leaves it out.
- **pair** and **role** group a same-day control, its candidate and their deltas.
- **runs** names run folders under `~/.agents/recall-eval/runs/` (`RECALL_EVAL_RUNS`), a private
  pointer: the folders never leave this machine.
- **public**: cleared for the README, docs and the site. Everything in the file is public (the
  repo is), so only aggregates go in; `public: false` marks a figure kept for the record, such as
  a run left out of the docs and why.
- **supersedes**: a correction is a new row listing the ids it replaces (a withdrawal has
  `value: null`). Readers drop superseded rows.

What never goes in: per-case data, case or chat ids, chat titles, questions, transcripts, paths,
or free text from a run folder. `check` enforces it: no paths or account names, no chat-id
shapes, run names only as folder basenames, and, where the private eval cases exist on this
machine, none of their ids or text.

## Comparing figures

Model-free retrieval benchmarks are deterministic: a figure compares with any other of the same
benchmark, metric and conditions. Agent and model evals drift from day to day (on 12 and 16
October the same code took in 9.0k and 7.3k tokens), so their benchmarks are `same_day` in the
registry: a figure compares only with one measured the same day, ideally as a planned pair.
`store.comparable(a, b)` lists the fields that differ among those the registry names, plus the
date for a same-day benchmark.

```python
import sys

sys.path.insert(0, "docs/benchmarks")
import store

rows = store.load()
store.latest_per_release(
    rows, "swecb-lite-retrieval", "hit@5", **{"conditions.scope": "all", "conditions.search": "hybrid"}
)
store.pairs(rows)["2026-10-16-memorycode-agent-hook"]  # control, candidate, deltas, same_day
store.one(rows, {"benchmark": "my-qa", "metric": "accuracy", "release": "latest"})
store.chart("swecb")  # a chart's marks with their numbers
```

From the command line: `python docs/benchmarks/store.py show --benchmark my-qa --metric accuracy`
and `python docs/benchmarks/store.py pairs`.

## Record a run

After an eval finishes (its `meta.txt` has an `exit=0` line):

```bash
uv run python docs/benchmarks/store.py record ~/.agents/recall-eval/runs/RUN --dry-run    # look first
uv run python docs/benchmarks/store.py record ~/.agents/recall-eval/runs/RUN --public
```

It reads `summary.json` (or the JSON an older eval printed to `output.txt`, or the harness folder
the wrapper's `output.txt` names), maps the eval name to a benchmark through the registry's
`evals` patterns, and writes one row per registered metric the run reports: only the numbers and
the settings the registry names, nothing else from the folder. It fills code identity from
`prod=`, the model, judge, harness, repeats and split from the run, conditions from the eval name
and the summary, and for private-corpus benchmarks the eval home's name and chat schema. It
refuses a run that is still going or failed, one the ledger marks `INVALID`, and an eval no
benchmark claims.

- Several folders that are repeats of one setting (`--agent` runs, one folder each) make one row:
  their mean, each folder's value in `by_repeat`.
- A planned same-day comparison: `~/.agents/recall-eval/bin/pair.sh` runs two refs side by side
  and prints both folders; `record-pair 2026-10-17-what CONTROL_DIR CANDIDATE_DIR --public`
  records them under one pair id, refuses arms of different benchmarks (or days, for a same-day
  benchmark), and names any other field in which they differ. (`--pair P --role R` on `record`
  does the same one arm at a time.) Then add the paired delta from `recall eval compare` with
  `add`.
- `--set key=value` adds a condition the run does not record (`--set notes="0.4.1 by
  claude-haiku-5-5"` for a hook arm); `--harness upfront-v0` names the harness of runs from before
  `agent_harness` was recorded; `--git SHA` names the commit when the code matches no tag, and is
  checked against `prod=`; `--metrics` keeps a subset.

Then `uv run --with fonttools --with brotli python docs/benchmarks/make_charts.py` if a chart
should show it, `uv run python docs/benchmarks/store.py check`, and commit.

## Add a figure by hand

Derived figures (`agent_tokens.py`'s context tokens), deltas and other systems' published figures
go in with `add`, from a JSON object or a list of them. `id`, missing fields and field order are
filled in, and the store is checked after appending:

```bash
uv run python docs/benchmarks/store.py add - <<'EOF'
{"kind": "third_party", "date": "2026-10-17", "benchmark": "longmemeval-qa", "metric": "accuracy",
 "system": "Example", "value": 0.901, "reported": "90.1", "model": "GPT-5", "judge": "GPT-5",
 "conditions": {"context": "own"}, "source": "https://example.com/paper", "source_detail": "Table 2",
 "grade": "vendor", "public": true}
EOF
```

For a derived figure, copy the identity of the rows it was computed from (`code`, `runs`,
`model`, `harness`, `pair`, `role`) and say how in `note`.

## Add a benchmark

One entry in `registry.json`, nothing else: `title`, `description`, `corpus` (`public`, or
`private` for the maintainer's own chats), `dataset`, the `run` command, `evals` (regexes over the
run's eval name; named groups become conditions), `conditions` (with `values` to restrict them,
and `from` / `map` / `default` to read them from the summary, `meta.json` or the command line),
`comparable_on`, `same_day`, and `metrics`. Each metric has a `unit`, `better` (`higher`, `lower`
or null), and, for the recorder, `key` (a dotted path into the summary; an interval
`{mean, lo, hi, n}` or a number), optionally `by_repeat` and `breakdown`. A benchmark that only
has other systems' figures needs no `evals` or keys. Then record a run.

## Draw a chart

Add an entry to `charts.json`; `make_charts.py` draws every chart it finds, and the entry's name
is the SVG's. A chart has a `kind` (`bars`, `grouped`, `columns`, `scatter`, `cost_curve`), its title
and subtitle, and selectors. A mark's selector merges the chart's `where`, its group's or
series', and its own, and must find exactly one public row: matching, carrying the fewest
conditions the selector leaves out, newest by date. `"release": "latest"` follows new releases.
Text that needs a figure names it, `{ref}` or `{ref:pct0}`, with `refs` holding selectors.
`check` resolves every chart, so a selector that finds nothing, or two different figures, fails
it.

## Reading from another repo (rererecall.com)

The store is plain files on `main`: `docs/benchmarks/results.jsonl`, `registry.json`,
`charts.json`, and `figures.json`. A local checkout can read the merged state without touching
the working tree: `git -C ../recall fetch -q && git -C ../recall show
origin/main:docs/benchmarks/figures.json`. A build in CI can fetch
`https://raw.githubusercontent.com/alirng/recall/main/docs/benchmarks/figures.json`. A site that
draws its own charts can read `figures.json` (each chart as drawn, with row ids for tooltips and
sources) or the rows themselves, and Python can import `store.py` from a checkout.

## Checks

```bash
uv run python docs/benchmarks/store.py check --base origin/main
```

checks every row against the schema and the registry, that each id matches its row (an edit
changes it), references between rows, private content, that every chart resolves and
`figures.json` is current, and with `--base` that no row of that revision was edited or removed.
CI runs it on every pull request. `.gitattributes` merges `results.jsonl` with `union`, so two
branches that each append rows merge without a conflict.

## Provenance of the first rows

The store started on 17 October 2026 from `results.json`, `docs/benchmarks.md` and the
competitor tables in [docs/research](../research). Each recall figure was traced to its run
folder where one exists, and recorded from that folder through the recorder; every recorded
value matched the published one. Where a figure predates the run ledger (9 October), or the runs
were not pinned to folders, it was added by hand with `runs: []` and a note. Tracing corrected
two labels: the MemoryCode "0.2.1" notes run and its "0.3.0 tools only" agent run ran commit
760f1b8, which is neither release, and the store says so. The 16 October MemoryCode agent token
and call figures (61k and 36k tokens, 7.3 and 2.2 calls) do not match their runs' summaries
(62.5k and 35.3k, 7.7 and 2.1); they are stored as published with both in the note. The 0.5.0
recheck and the 17 October 0.4.2-against-0.5.0 pair were then added the ordinary way, with
`record` on their run folders and `add` for the MCP call times from recall's usage log.
