# Scoped Russian word-form fallback

Use case: an edition has no semantic coverage and a natural question uses
inflected Russian forms absent from the literal FTS result. The implementation in
`russian_retrieval.py` expands only an **empty unquoted lexical result inside one
indexed Russian book**. Baseline, global queries, quoted phrases, other languages
and existing nonempty results remain unchanged. `Library.search` invokes it only
after the ordinary lexical rerank returns no results. The helper does not call
`Library.search`, so the integration cannot recurse. Its optional response
`lexical_fallback` metadata identifies the method and bounded candidate count.

## Dependency and primary sources

The base project dependency is pinned to **`snowballstemmer==3.1.1`** in the
project configuration and lockfile. It is a small pure Python package under
[BSD-3-Clause, as declared by the maintainer's release](https://pypi.org/project/snowballstemmer/3.1.1/).
The [upstream license](https://github.com/snowballstem/snowball/blob/main/COPYING)
requires preservation of its notices; the installed distribution includes them.

The [official Russian algorithm](https://snowballstem.org/algorithms/russian/stemmer.html)
performs suffix stripping, including е/ё folding. It is not a lemmatizer and may
produce different stems for related forms or conflate unrelated ones. The
[package API documentation](https://pypi.org/project/snowballstemmer/3.1.1/)
requires normalized lowercase input and warns against concurrent use of one
mutable stemmer. The implementation uses normalized tokens, one stemmer per
thread and a bounded shared string-result cache of 32,768 entries.

## Candidate and precision constraints

A query may contain at most 12 distinct content stems. Prefixes must have at
least three Cyrillic characters; at most four е/ё variants per stem are emitted.
FTS syntax comes exclusively from normalized tokens, with the wildcard outside
safely quoted prefixes. Native `_lexical_rows` retains exact book/language/status
and section ownership constraints and the existing 200-candidate bound.

Reranking requires exact Snowball stem equality in **source text**, not a matching
title/author. Mere prefix matches are insufficient. At least two distinct content
stems and 50% content-stem coverage are needed, except a one-stem query needs one.
Negations and numeric tokens are mandatory exact source tokens, do not count as
content matches and are never silently truncated. Oversized/too-short-stem queries
skip expansion. For a longer query reduced to a three-letter stem, a bare
three-letter source word alone is insufficient; this conservatively avoids some
short-stem collisions at a cost to recall. Homonyms remain an inherent limitation.

All returned fragments pass the native canonical slice/hash verification. Their
text, attribution, edition, offsets, citation and reader address remain unchanged.
`stem_coverage` is an overlap score, not a probability or entailment judgement.
This code supplies evidence; it does not generate or assert book facts.

## Open development results

The three audited RU originals use their public RAG-development questions. The
Ask layer independently softens question quotation delimiters for retrieval;
the original question still reaches generation/judgement, while Search quotes
remain strict. Results after that separate softening change:

| Open case | Normal count | Empty-only expansion | Exact reference quote in first eight sources |
| --- | ---: | --- | --- |
| Чехов, «Хамелеон» | 1 | Skipped | Yes, rank 1 |
| Лесков, «Левша» | 0 | 46 candidates → 1 verified fragment, 56.438 ms | Yes, rank 1 |
| Толстой, «Много ли человеку земли нужно» | 18 | Skipped | No, reference remains rank 14 |

Reference-anchor hit@8 moves from 1/3 after quote softening to 2/3 with this
fallback. This is not an answer-quality score. The Tolstoy candidate exclusion
is explicitly unresolved; an empty-only fallback cannot change a nonempty ranking.
Strict quoted Search stays empty for these original three questions.

The final independent CC0 fixture has five inflection/е–ё/prefix-collision cases
and six absent-topic, shared-anchor, number, negation and short-stem collision
negatives. All five positives are recovered, all six negatives remain empty,
and all five returned fragments match authored gold text (fixture precision 1.0).
This small constructed set does not establish general Russian-search precision.
23 unit tests and five public API/integration checks cover strict/unbalanced quotes, baseline/global/nonempty exclusions,
book availability, safe bounds, canonical offsets and concurrent stemming.

Selected live book headers, artifact/canonical hashes, section hashes, passage
IDs/spans/hashes and contributor roles matched before and after the run. Other
corpus ingestion continued; no whole-live-database hash or freeze is claimed.
The read-only adapter skips source constructors/migrations and uses ordinary
WAL-aware `mode=ro` plus `query_only`, not immutable reads of a changing database.
Only owned CC0 temporary imports write data. No LLM or embedding call ran.

```sh
uv run pytest -q tests/test_russian_retrieval.py
uv run python scripts/evaluate_russian_fallback.py \
  --report data/russian-fallback-reproduction.json
```

[`russian-fallback-v1.json`](../experiments/russian-fallback-v1.json) retains the
initial nine-case experiment. The hardened
[`russian-fallback-v2.json`](../experiments/russian-fallback-v2.json) adds two
short-stem collision negatives. The production proof
[`russian-fallback-v3-integrated.json`](../experiments/russian-fallback-v3-integrated.json)
compares integrated results with the prototype by canonical IDs, exact text and
citations. Raw lexical controls disable the helper with an ordinary monkeypatch;
there is no test-only API switch. Its selected-source guards also passed.

Internal integration hook:

```python
fallback = scoped_russian_fallback(
    library,
    query,
    book_id,
    language,
    prior_results=normal_results,
    mode="lexical",
    limit=limit,
)
if fallback["used"]:
    normal_results = fallback["results"]
    warnings.extend(fallback["warnings"])
```

Use it only after the normal lexical pass has completed. The required
`prior_results` argument and mode/scope guards prevent replacing existing results
or invoking the helper recursively. Canonical integrity still cannot certify
that a retrieved fragment supports a proposed answer; the answer layer must
continue its own validation and conservative abstention.
