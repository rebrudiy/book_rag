# Native FTS5 rank candidate

The initial read-only prototype is `scripts/benchmark_native_rank.py`.
The same native ordering strategy is now implemented by
`Library._lexical_rows`; the original weighted BM25 scores, eligibility,
candidate limits and score/ID ordering are retained. No embedding model,
semantic scoring threshold or final-test fixture changes are involved.

[SQLite documents the hidden `rank` column](https://sqlite.org/fts5.html#sorting_by_auxiliary_function_results)
as an alternative ordering path for BM25, with a per-query mapping for weighted
BM25. It may be faster when a caller stops early. The prototype requests
`bm25(1.0,2.0,1.5,1.2)` through `rank MATCH` and orders only by `rank`, allowing
the native FTS plan to provide score ordering without a secondary SQL rowid
sort. This is a hypothesis about execution cost, not a promised speedup.

## Exactness and bounds

The native SQL has no premature LIMIT. Its eligibility EXISTS requires an
indexed book, the selected book/language if any, and a section owned by the
same book as the passage. Scoped FTS rowid envelopes retain those exact
filters. The Python consumer maintains at most N `(score,id)` keys in a heap,
where N is the existing candidate limit (200 lexical, output limit baseline).
It consumes every equal-score row at the boundary, retaining smaller IDs, and
stops only on a strictly worse score. The selected IDs are finally sorted by
the exact `(score,id)` ordering. No entire tie group is stored in Python.

Ranking and bounded hydration run on one connection inside explicit BEGIN /
ROLLBACK. The native cursor is closed before hydration; the transaction keeps
the snapshot alive across that boundary.
[SQLite's transaction documentation](https://sqlite.org/lang_transaction.html)
states that a read transaction retains its historical snapshot until it ends.
An owned WAL test commits a changed author from another connection between
cursor close and hydration and verifies that the old attribution remains
coherent until the read transaction ends.

Python heap storage is bounded; tie scan time is not. An enormous equal-score
group must still be read to preserve exact IDs. SQLite may also compute/store
internal ranks before yielding rows. The bounded Python heap does not prove a
bound on total SQLite/OS cache memory, which was not measured here. SQL progress
deadlines abort incomplete experiments instead of publishing partial rankings.

## Reproduction and observations

```sh
uv run pytest -q tests/test_native_rank_prototype.py
uv run python scripts/benchmark_native_rank.py \
  --frozen20 data/snapshots/curated20-v3 \
  --output data/bench-native-rank-v1/frozen20.json
```

The original CC0 fixture contains tied scores, Unicode, missing/stale FTS rows,
nonindexed books, orphan sections and deliberately corrupt ownership. Six tests
pass, including a reversed 10,000-ID tie stream that retains only five heap
entries, rejects nonfinite/nonmonotonic scores, and stops before consuming a
strictly worse tail. All raw record/BM25 float-hex/citation guards match the
current core, including corrupted ownership omission.

The optional frozen20 path accepts only the 20-edition/12,108-passage corpus.
It reads the original with immutable/query-only SQLite, backs it up to an owned
temporary copy, and migrates only that copy for later translator/pipeline fields.
No original `Library` constructor or vector copy is used. The resulting source
is queried read-only for both control and candidate.

On SQLite 3.53.2, EXPLAIN reported `32:rM4` (scoped `32:rM4><`), with no temporary
SQL ORDER BY tree for native rank. The original control used its materialized CTE.
These strings are diagnostic and version-dependent.

| Frozen20 case | Current core ms | Native ms | Eligible rank rows read / pool size |
| --- | ---: | ---: | ---: |
| Global `man` | 78.765 | 77.792 | 201 / 200 |
| Scoped Meditations `man` | 79.035 | 79.468 | 197 / 197 |
| Scoped Darwin `struggle for existence` | 71.694 | 71.523 | 202 / 200 |
| Baseline `White Rabbit` | 2.804 | 2.643 | 6 / 5 |

These are single ordered control-then-candidate measurements, including quote
verification/reranking and connection overhead, with possible second-run cache
benefit. They establish no meaningful speedup on frozen20. The synthetic
2,107-row equal-score group had to be fully consumed: its baseline query took
1.716 ms in the core and 4.083 ms in the candidate. Exact ties can therefore
make the candidate slower.

The full ignored receipt is `data/bench-native-rank-v1/frozen20.json`. A separately
authorized, bounded large-library comparison is needed to test whether avoiding
metadata hydration for most global FTS matches helps the observed 15-second
common-word query. Production code and the large corpus remain untouched by
this experiment.

## Separately authorized large-library observation

`scripts/benchmark_native_large.py` performs exactly one current-CTE/native pair
for global `man`. It uses immutable/query-only connections without a source
constructor, checks the expected indexed-edition count, guards main/active
generation identities and refuses nonempty WALs. SQLite progress deadlines are
20 seconds per phase and 50 seconds overall. Timeout/source failure gets no
equivalence or speedup credit. Two owned-CC0 runner tests pass.

On the quiet **4,001-edition** source, the pair completed in 18.228 seconds:

| Current integrated CTE | Native rank | Raw/final equivalence |
| ---: | ---: | --- |
| 17,367.852 ms | 624.917 ms | Exact IDs, BM25 float hex, metadata, quotes and citations |

The native consumer saw 201 qualified rows, retained/hydrated 200 and stopped
at the first strictly worse score. Its EXPLAIN plan used `32:rM4` and correlated
eligibility. This count is not a count of internal FTS scoring work or rejected
eligibility checks. Python heap storage stayed at 200 keys; total SQLite/RSS
memory was not measured. All source guards passed and neither phase timed out.

E5-base encoding ran concurrently on MPS (PID 25613 was alive at start/end),
so CPU/I/O and unified memory were not isolated. The native phase also ran
second and may benefit from cache warming. The observed ratio, 27.792×, is a
single-pair observation, not a repeated throughput estimate or a production
promotion. It supports further consideration of avoiding most canonical-table
lookups until the native rank stream reaches a small eligible pool.

The ignored receipt is `data/bench-native-large-v1/report.json`, including
implementation hashes, guards, stream counts, plans and GPU-job context.
This observation motivated the separately verified package integration below.

## Package integration and reproducible comparison

`Library._lexical_rows` retains the existing lexical pool of 200 passages and
the baseline output-sized pool. It hydrates only the selected rows, including
the original bibliographic metadata, contributor/translator credits and latest
ingestion warnings. Both selection and hydration require indexed status,
matching book/language filters and strict section ownership. Hash/slice checks,
corrupt-quote suppression and lexical reranking continue after hydration.

The helper opens a read transaction only when it owns the snapshot; an existing
caller transaction stays open. Twenty-five integration checks cover exact raw
scores/records and output prefixes, reversed boundary ties, no-answer/Unicode
queries, corrupt ownership, invalid rank streams, and a real WAL reindex after
the rank cursor closes but before source hydration. The latter must preserve
the old source text and attribution for that response and show the replacement
only on the next query. These pass alongside the existing ownership, lexical,
pipeline, saved-history and semantic fake-model checks.

`scripts/compare_native_search.py` compares a preserved old control module with
the current candidate, using only lexical/baseline modes and the public 54-query
development fixture. It bypasses source constructors, uses immutable/query-only
SQLite, refuses nonempty WALs and guards source/active-generation identities.
It compares ordered raw IDs, exact BM25 float hex, hydrated records and complete
final responses except elapsed time, including warnings. Each case/mode runs
once; controls run first, so native timings may have warmer caches. Timeouts,
skipped cases and source-guard failures receive no equivalence/speedup credit.
The whole run is bounded to 20 seconds per query, 240 seconds of controls and
300 seconds overall. Seven tiny owned-CC0 runner checks include timeout handling
and refusal of model modes or nondevelopment fixtures.

```sh
mkdir -p artifacts/native-control
git show b8606c4:src/book_rag/library.py > artifacts/native-control/library.py
uv run python scripts/compare_native_search.py \
  --control artifacts/native-control/library.py \
  --output data/bench-native-dev54-reproduction
uv run pytest -q tests/test_native_search.py tests/test_native_dev_comparator.py
```

Gold hydrated records contain source text and remain in the ignored output
directory. Public reports contain only query IDs, timings and evidence hashes;
this equality experiment does not create a new relevance or final-test score.

## Bounded 4,001-edition dev54 comparison

The first integrated comparison finished in **274.749 seconds**, within its
300-second overall budget. Main/active-generation file identity and no-nonempty-WAL
guards passed. All 108 native cases completed; no model was loaded and no source
constructor, migration or corpus write ran.

| Mode | Exact completed pairs | Paired CTE / native total | Paired p50 ms CTE / native | Paired p95 ms CTE / native |
| --- | ---: | ---: | ---: | ---: |
| Lexical | 54 / 54 | 86.979 / 5.772 s | 83.519 / 64.826 | 11,979.423 / 422.351 |
| Baseline | 38 / 54 | 98.976 / 12.168 s | 159.105 / 156.019 | 14,797.731 / 1,344.806 |

Every completed pair matched raw ordered IDs, BM25 float hex, full hydrated
metadata/quote records and complete final responses including warnings. There
were no mismatches among those **92 pairs**. Lexical verification covers all 54
development cases. Full two-mode verification remains **incomplete**: three
baseline controls timed out (two at 20 seconds, one at the remaining 14.044-second
control budget) and 13 were skipped after the 240-second combined control budget.
Those 16 cases receive no equality or speedup credit. All 54 native baseline
cases completed in 24.542 seconds, including the unpaired cases.

Controls ran before all native cases, so caches may have favored the candidate.
These are single ordered measurements, not repeated isolated throughput estimates.
The result demonstrates a smaller expensive tail on this public development set;
it does not establish a general latency guarantee or improve the documented
semantic relevance/abstention scores. Source guards compared file identities and
nonempty WALs rather than recomputing all multi-GB source hashes.

The public receipt is
[`experiments/native-lexical-dev54-v1.json`](../experiments/native-lexical-dev54-v1.json).
It deliberately retains `status: incomplete` and `validated: false` for the whole
two-mode set, with separate per-mode exact-pair counts. Gold text stays only in
`data/bench-native-dev54-v1/unvalidated-control/`.
