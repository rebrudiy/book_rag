# Read-only lexical SQL plan prototype

`scripts/benchmark_lexical_plan.py` compares candidate SQL without editing
`Library.search`. It always creates an owned original CC0 synthetic database.
An optional frozen20 comparison opens only the existing static SQLite file with
`mode=ro&immutable=1`, `query_only=ON`, and no `Library` constructor or migration.
It rejects another corpus size; it does not benchmark the live 3,991 editions.
No models, MPS, downloads or final-test queries are involved.

```sh
uv run python scripts/benchmark_lexical_plan.py --repeats 3
uv run python scripts/benchmark_lexical_plan.py --repeats 2 \
  --frozen20 data/snapshots/curated20-v3 \
  --output data/bench-lexical-plan-v1/frozen20.json
uv run pytest -q tests/test_lexical_plan_prototype.py
```

## Plans and correctness boundary

The control retains the current weighted BM25, joins, optional filters and
`ORDER BY score,p.id LIMIT …`. Lexical retrieval still selects 200 candidates
before its existing verification/coverage reranking and final output limit.

The materialized candidate CTE selects only passage IDs and BM25 scores. Indexed
book status, exact optional book/language filters and an existing section are
checked **before** its limit. The outer query hydrates only those candidates,
including canonical substrings, bibliographic metadata, contributor/translator
roles and latest-attempt fields, then repeats the exact score/ID ordering.
[SQLite documents MATERIALIZED as an optimization fence](https://sqlite.org/lang_with.html#materialization_hints);
the syntax requires SQLite 3.35 or later. This experiment used SQLite 3.53.2.

For a selected book, the envelope variant obtains its first and last passage IDs
through two ordered `passages_book` index seeks and adds inclusive FTS rowid
constraints. Their lookup cost is included in timings. The exact book filter
remains mandatory: reindexing and noncontiguous insertion can put other books
inside that range. Missing books use an empty range. Both scope/eligibility
filters and global FTS BM25 statistics remain unchanged.

The unscoped pure-FTS variant uses an eligibility `EXISTS` before its limit, then
hydrates the bounded IDs. Truncating the raw FTS ranking *before* eligibility is
unsafe: synthetic stale/orphan rows occupy its highest positions and would
consume the pool. This unsafe plan is not a candidate for integration.

Every accepted variant must preserve ordered IDs, bit-exact BM25 values (float
hex encoding), every hydrated field and canonical quote, and final verified
results/citation/reader URLs. Synthetic cases cover nonindexed books, missing
passages/books/sections, corrupt passage hashes, Unicode offsets, interleaved
rowids, editorial/translation credits and a failed refresh of an available old
edition. Four tests pass without initializing any source `Library`.

## Bounded frozen20 observation

The source contained 20 indexed editions and 12,108 passages. It predates
`source_pipeline` and `section_translator`; read-only projections supply the
equivalent empty/default values. Main-file identity is checked before/after;
the benchmark does not rehash the full database. The source is not migrated.

The versioned [small report](../experiments/lexical-plan-v1.json) retains exact
guards, both timed observations, query plans and implementation hashes. The full
ignored report additionally retains SQL strings and candidate IDs. Timings are
warm, paired by the **same query and filters**, with randomized variant order.
They include schema compatibility inspection and envelope lookup, but not Python
result verification. Quote counts come from a separate instrumented SQL pass.

| Query/scope | Control ms | CTE ms | CTE + range ms | Control / CTE quote evaluations |
| --- | ---: | ---: | ---: | ---: |
| `man`, global | 21.807, 21.893 | 11.723, 11.721 | — | 811 / 200 |
| `struggle for existence`, Darwin 1859 | 13.223, 13.193 | 7.550, 7.497 | 6.728, 6.828 | 451 / 200 |
| `man`, Meditations | 51.192, 8.959 | 11.791, 9.384 | 7.094, 7.360 | 197 / 197 |

All equivalence guards passed. Global `man` improved about 1.86× with the joined
CTE; scoped Darwin improved about 1.95× with CTE plus range. The Meditations
control has a large timing outlier, so its median ratio does not establish a
stable 4× improvement. Synthetic short/tied cases showed little benefit and
small overhead; a scattered book's envelope did not help.

`EXPLAIN QUERY PLAN` showed `MATERIALIZE candidates` before hydration and scoped
FTS `M4><` instead of `M4`, confirming the range constraints reached FTS5 in this
SQLite version. The pure-FTS/eligibility variant took 12.680–12.920 ms for global
`man`, slower than the joined CTE here. There is no reason to prefer it based on
this observation. [SQLite cautions that EXPLAIN output is version-dependent](https://sqlite.org/eqp.html);
these strings are diagnostics, not an application contract.

## Integrated query path

`Library.search` now uses the joined materialized candidate CTE and, for a
selected book, the rowid envelope. The production bounds are two scalar index
seeks **inside the same SQL statement**. Unlike the prototype's separate lookup,
they share the candidate query's read snapshot: a concurrent reindex cannot
replace the book's passage IDs between reading its bounds and selecting hits.
No extra autocommit bounds query is issued. Weighted global BM25, exact
book/language/status/section eligibility, candidate limits, score/ID ordering,
quote suppression and later lexical reranking remain unchanged.

All issuing paths additionally require `sections.book_id = passages.book_id`:
the candidate CTE checks it before LIMIT, hydration checks it again, and single
and batch passage reads enforce it too. A passage with a valid text/hash/slice
but a foreign book ID must not inherit that other book's author, edition or
artifact. Such damaged rows are now omitted, and saving them is refused. This
is an intentional correction for corrupted ownership; equivalence with the old
SQL applies to correctly owned source data. Already saved verified history keeps
its frozen attribution and loses an invalid current-reader link. Index cleanup
is separate and was not tightened by this retrieval change.

Sixteen integrated tests compare raw hydrated records, BM25 float hex values,
final results, warning propagation, editorial/translation credits, unavailable
books, orphan records, corrupt citations, interleaved rowids, Unicode, quoted
phrases and missing scopes with the preserved old SQL. A query trace also guards
that bounds/selection share a read snapshot. Together with existing retrieval,
library, pipeline and prototype checks, 85 focused tests passed.

A separate read-only frozen20 check copied the database into an owned temporary
directory before constructing the current `Library`. The original was opened
only with immutable/query-only SQLite. All raw/final guards matched the original
control for global `man`, scoped Meditations `man`, scoped Darwin
`struggle for existence`, baseline `"White Rabbit"`, French `Candide` and a
missing book. The original main-file SHA-256 before and after was
`ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8`, matching
the published frozen input. The integrated scoped query plan still used `M4><`.
Its ignored receipt is `data/bench-lexical-plan-v1/integrated20.json`.

These checks do not establish full-corpus latency. Compare identical development
queries against a captured large corpus with bounded execution before making a
large-library performance claim. The standalone benchmark retains its old SQL
control; running it never edits production code or a live library.

## One bounded large-library check

After ingestion is stopped and the source WAL is empty, the separate runner can
compare four declared queries once each against the quiet existing library:

```sh
uv run python scripts/benchmark_lexical_large.py data/full \
  --query-seconds 18 --budget-seconds 110 \
  --output data/bench-lexical-large-v1/report.json
```

It uses a read-only `Library.search` adapter without calling the constructor,
captures its actual raw `_result` rows and compares them with the preserved old
SQL control. The adapter uses immutable/query-only connections and SQLite
progress deadlines. Main-file and active-generation identities must remain
unchanged; a nonempty WAL rejects the run. Empty transient reader WAL/SHM files
are not treated as changed canonical bytes. Vector arrays are only statted;
models and embeddings are not loaded. Output stays outside the source library.

Each case runs old control then integrated search exactly once. The second run
may benefit from warmed OS caches, so its ratio is an observation, not a stable
throughput estimate. Timeouts, wall overruns and source-guard failures receive
no equivalence or speedup credit. SQLite progress callbacks cannot interrupt a
single blocking C/filesystem operation immediately. The runner reserves ten
seconds below 120 seconds and starts no further SQL after its overall budget.
Three tests verify exact real-core results on owned CC0 sources, actual SQL
deadline interruption, refusal of writes and invalid source/WAL guards.

One such run on the 3,991-edition library completed in 53.623 seconds. Every
case's raw/final guards matched exactly, no phase timed out, and source guards
passed. Its ignored receipt is `data/bench-lexical-large-v1/report.json`.

| Query/scope | Old control ms | Integrated search ms |
| --- | ---: | ---: |
| `man`, global | 17,448.015 | 14,996.563 |
| `man`, Meditations | 12,347.421 | 87.784 |
| `struggle for existence`, Darwin 1859 | 3,125.123 | 68.568 |
| `White Rabbit`, baseline/global | 4,610.601 | 348.885 |

The control timing covers raw SQL and read-only/schema checks; the integrated
timing also includes quote verification and lexical reranking. They are one
ordered pair each, with the integrated run second. The large scoped gains are
consistent with the observed FTS rowid pushdown, but the ratios are observations,
not repeated latency estimates. Global common-word search still took about
15 seconds and remains a product limitation. This run establishes equality for
these four known queries; it does not assess general retrieval relevance.
