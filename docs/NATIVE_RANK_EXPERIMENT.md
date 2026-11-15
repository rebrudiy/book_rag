# Native FTS5 rank candidate

This is a separate read-only prototype in `scripts/benchmark_native_rank.py`.
It does not change `Library.search`, select a production strategy, access the
large live corpus, load a model or inspect final-test labels.

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
SQL ORDER BY tree for native rank. The original core uses its materialized CTE.
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
The package search path remains frozen pending a separate integration decision.
