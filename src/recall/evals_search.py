"""M1, find rate: does the chat that answers a question come back near the top?

Each search_v2 case is a question asked the way an agent would pass it to search_chats,
with every chat that answers it:
  {"id": "s-001", "query": "...", "expect": ["<chat id>", ...], "agent": null, "tags": [...], "split": "train"}
A case is a hit at k when any expected chat is in the top k. Search runs exactly as the
MCP tool runs it (store.search, hybrid when embeddings are on), and is deterministic, so
there are no repeats; the latency of each search is recorded too.
"""

import sys
import time

from . import evals_harness as h
from .chats import store

K = 10  # search_chats returns 8 by default; ranking does not depend on the limit


def run(con, cases: list[dict], run: h.Run, against: str | None = None, keyword_only: bool = False) -> dict:
    """keyword_only: rank without embeddings even when they are configured (vector=False)."""
    ids = {r[0] for r in con.execute("select id from sessions")}
    by_native = {i.split(":", 1)[1]: i for i in ids}
    vector = False if keyword_only else None  # None: search embeds the query itself, as the MCP tool does
    store.search(con, "warm up the index and the embedding model", vector=vector)  # not part of latency
    for c in cases:
        expect = {by_native.get(e, e) for e in c["expect"]} & ids
        if not expect:
            run.record(c, {"status": "missing_chat", "detail": f"none of {c['expect']} is in the index"}, {})
            continue
        started = time.perf_counter()
        rows = store.search(con, c["query"], c.get("agent"), None, None, limit=K, vector=vector)
        ms = (time.perf_counter() - started) * 1000
        rank = next((i for i, r in enumerate(rows, 1) if r["chat_id"] in expect), None)
        row = {
            "status": h.OK,
            "rank": rank,
            "hit@1": rank == 1,
            "hit@5": rank is not None and rank <= 5,
            "hit@10": rank is not None,
            "rr": round(1 / rank, 3) if rank else 0.0,
            "ms": round(ms, 1),
            "query": c["query"],
        }
        results = [
            {
                "rank": i,
                "expected": r["chat_id"] in expect,
                **{k: r.get(k) for k in ("chat_id", "agent", "title", "project", "updated", "snippet")},
            }
            for i, r in enumerate(rows, 1)
        ]
        run.record(c, row, {"expect": sorted(expect), "results": results})
        if c["split"] != "test" and not row["hit@5"]:
            print(f"miss  {c['id']:10} rank {rank or '-':>3}  {c['query'][:90]}", file=sys.stderr)

    scored = [r for r in run.rows if r["status"] == h.OK]
    metrics = {
        "hit@1": h.ci([float(r["hit@1"]) for r in scored]),
        "hit@5": h.ci([float(r["hit@5"]) for r in scored]),
        "hit@10": h.ci([float(r["hit@10"]) for r in scored]),
        "mrr": h.ci([r["rr"] for r in scored]),
    }
    latency = h.spread([r["ms"] for r in scored])
    hits, mrr = h.by_tag(scored, lambda r: float(r["hit@5"])), h.by_tag(scored, lambda r: r["rr"])
    from . import embeddings

    mode = "keyword" if keyword_only or embeddings.configured() is None else "hybrid"
    summary = {
        "mode": mode,
        **metrics,
        "by_tag": {t: {"hit@5": hits[t], "mrr": mrr[t]} for t in hits},
        "latency_ms": {"p50": latency.get("median"), "p95": latency.get("p95")},
        "plumbing": h.plumbing(run.rows),
        "warnings": h.headroom(metrics),
    }
    if against:
        summary["against"] = {"run": against}
        summary["warnings"] += h.mismatch(against, run.meta)
        for metric, value in (("hit@5", lambda r: float(r["hit@5"])), ("mrr", lambda r: r["rr"])):
            summary["against"][metric] = h.paired({r["id"]: value(r) for r in scored}, h.against(against, value))
    headline = (
        f"{mode} hit@5={h.fmt(metrics['hit@5'])} hit@1={metrics['hit@1']['mean']} mrr={metrics['mrr']['mean']} "
        f"p50={summary['latency_ms']['p50']}ms"
    )
    if against:
        headline += f" Δhit@5={h.fmt(summary['against']['hit@5'], signed=True)}"
    columns = [("rank", "rank"), ("hit@5", "hit@5"), ("ms", "ms"), ("query", "query")]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))
