"""Query profiles: which kind of match a search case needs, labelled by written rules, no model.

The three profiles follow Chris Simpson's "near both, on neither" experiment
(chrissimpson.co.uk/near-both-on-neither-single-vector-retrieval.html):
  P1 literal      the query's deciding facet appears verbatim in the target chat, such as an
                  error string, identifier or file name
  P2 concept      the target shares the meaning but not the words
  P3 conjunction  two independent facets must both hold
A case that fits no rule stays unlabelled rather than being forced into a profile.

The rules, which a person can apply by hand given a search of the index:
* Pool: the chats the search ranks. Targets: the gold chats in the pool (R of them).
* Query terms: the query's words (runs of letters, digits and underscores), lowercased and
  deduplicated, less recall's search stopwords and one-character words; plus each compound
  literal, two or more words joined by `.`, `/`, `:` or `-` with no space (`models/query.py`,
  `wcs.all_pix2world`, `pre-commit`).
* A chat contains a term when the term matches as a phrase in one of its messages or its title,
  with the index's own tokens and stemming (porter unicode61), so exactly as BM25 matches it.
  S(t) is the set of pool chats that contain t; its decoys are the ones that are not targets.
* A term is shared when a target contains it, and narrow when it is shared and has at most
  DECOYS decoys: on its own it puts a target among the top DECOYS + 1 chats.
* Overlap: of the idf weight of the query's words, the share that a target holds, with
  idf(t) = ln(N / |S(t)|) over the N chats of the pool. A word found nowhere in the pool counts
  as if one chat held it (ln N): a concept the corpus never names is what P2 is.
Then, first rule that holds:
1. P1 literal: some query term is narrow.
2. P3 conjunction: two shared terms a and b, where a target contains both, the chats holding
   both have at most DECOYS decoys, and a and b are independent facets: they share at most half
   the chats of the rarer one, |S(a) & S(b)| <= min(|S(a)|, |S(b)|) / 2, so neither is merely
   the other half of a phrase.
3. P2 concept: overlap is below CONCEPT_OVERLAP.
4. Otherwise unlabelled: the target holds most of the query's words, but so do other chats, and
   no word or pair of words singles it out.

P3 here is a lexical conjunction: both facets appear verbatim. Chris's P3 pairs a literal facet
that many documents share with a concept that never appears in the text, which no rule over
words can see; cases of that kind have to be written by hand.

Scores per profile are Chris's: MRR (reciprocal rank of the first target) and Recall@R (the
share of the R targets in the top R), next to each eval's own hit rates. The ranked lists are
cut off (top 15 for SWE-ContextBench, 10 for search --v2, 50 for LongMemEval), so a target
ranked below the cut counts 0.
"""

import itertools
import math
import re

from . import evals_harness as h
from .chats import store

DECOYS = 2  # a narrow term (or pair) leaves a target among the top 3 chats
CONCEPT_OVERLAP = 0.5  # less than half the query's weight is in the target
PROFILES = ("P1", "P2", "P3", "unlabelled")
WORD = re.compile(r"\w+")
COMPOUND = re.compile(r"\w+(?:[./:\-]\w+)+")


def terms(query: str) -> tuple[list[str], list[str]]:
    """The query's words and compound literals, as the rules define them."""
    text = query.lower()
    words = [w for w in dict.fromkeys(WORD.findall(text)) if len(w) > 1 and w not in store.STOPWORDS]
    return words, list(dict.fromkeys(COMPOUND.findall(text)))


def label(con, query: str, targets: set[str], pool: set[str]) -> dict:
    """The case's profile, with the evidence for it. con: the index the search ran on;
    targets and pool: chat ids as the index holds them."""
    words, compounds = terms(query)
    found = {t: _chats(con, t) & pool for t in dict.fromkeys(words + compounds)}
    shared = {t: s for t, s in found.items() if s & targets}
    n = len(pool)
    weight = {w: math.log(n / max(len(found[w]), 1)) for w in words}
    total = sum(weight.values())
    overlap = sum(v for w, v in weight.items() if w in shared) / total if total else 0.0
    out = {"terms": len(found), "shared": len(shared), "overlap": round(overlap, 3)}
    narrow = sorted((t for t, s in shared.items() if len(s - targets) <= DECOYS), key=lambda t: len(shared[t]))
    if narrow:
        return {"profile": "P1", **out, "narrow": narrow[:5]}
    pairs = []
    for a, b in itertools.combinations(shared, 2):
        both = shared[a] & shared[b]
        if both & targets and len(both - targets) <= DECOYS and len(both) * 2 <= min(len(shared[a]), len(shared[b])):
            pairs.append((len(both), a, b))
    if pairs:
        size, a, b = min(pairs)
        return {"profile": "P3", **out, "pair": [a, b], "pair_chats": size, "pairs": len(pairs)}
    if overlap < CONCEPT_OVERLAP:
        return {"profile": "P2", **out}
    return {"profile": "unlabelled", **out}


def recall_at_r(ranked: list[str], relevant: set[str]) -> float:
    """Share of the R relevant chats in the top R, as Chris's metrics.py computes it."""
    return sum(x in relevant for x in ranked[: len(relevant)]) / len(relevant) if relevant else 0.0


def breakdown(rows: list[dict], metrics: dict) -> dict:
    """Per profile, each metric's mean and 95% interval. metrics: name -> row key."""
    out = {}
    for p in PROFILES:
        mine = [r for r in rows if r.get("profile") == p]
        if mine:
            out[p] = {name: h.ci([float(r[key]) for r in mine]) for name, key in metrics.items()}
    return out


def _chats(con, term: str) -> set[str]:
    phrase = '"' + term.replace('"', "") + '"'
    found = con.execute(
        "select distinct m.session_id from messages_fts f join messages m on m.id = f.rowid where messages_fts match ?",
        (phrase,),
    ).fetchall()
    titled = con.execute("select id from sessions_fts where sessions_fts match ?", (f"title : {phrase}",)).fetchall()
    return {r[0] for r in found} | {r[0] for r in titled}
