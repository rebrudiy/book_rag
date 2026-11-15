# Scoped vector scoring: frozen matrix proof

`scripts/benchmark_scoped_vectors.py` compares the former full matrix dot product
followed by the same passage eligibility mask against `_score_vectors` with
`ordered_ids=True`. It reads the completed frozen1308 generation directly through
NumPy read-only memmaps. Passage IDs, book status and language come from immutable,
query-only SQLite; the script never constructs `Library` on the source, migrates
it, reads chapter text, runs a model or touches a live database.

The original database SHA before and after was
`2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0`.
Full SHA and file identity guards also matched for vectors, IDs, active metadata,
checkpoint, generation metadata, reuse receipt and key map when present.
The matrix contained **1,865,985 × 384 float32 values**, 2,866,152,960 payload bytes.
IDs were independently checked positive and ascending before enabling the optimized
binary-search bounds. NumPy arrays were read-only and exact-length.

One stored matrix row (793288, passage 1051134) supplied the query, without a model
or normalization change. Its own passage was retained. Thus this is a numeric and
storage-access experiment rather than a relevance/heldout or user-query evaluation.

| Scope | Eligible windows | Old dot+mask ms | Scoped ms | Old/scoped peak RSS MiB | Identical candidate IDs |
| --- | ---: | ---: | ---: | ---: | ---: |
| Meditations `pg2680-full` | 1,476 | 408.735 | 1.266 | 2,794.34 / 45.36 | 100 / 100 |
| Darwin `pg1228-full` | 2,970 | 544.612 | 2.118 | 2,787.39 / 49.92 | 83 / 83 |
| German source language | 206,051 | 385.955 | 38.247 | 2,821.05 / 388.98 | 100 / 100 |

All three complete score arrays matched **bit for bit**, with maximum absolute
error zero. Out-of-scope entries remained negative infinity. The current fixed
candidate selector at the unchanged 0.30 floor returned identical ordered lists,
including the 83 qualifying Darwin passages rather than inventing a full 100.
This does not prove identical floating-point results for every BLAS implementation,
query, shape or score boundary; the runner records exact and allclose separately.

The reported trial includes the exact production section-owner eligibility JOIN.
An earlier three-scope trial omitted this redundant-on-the-valid-source check;
its masks, query, source inputs and numerical results were confirmed identical.
Both trials are retained separately in the receipt, without pooling timings or
selecting best results. Each method ran once per trial in a fresh, sequential CPU subprocess. Process-local BLAS
thread limits were set to one before importing NumPy. Timings include the dot,
eligibility mask or gather, and score allocation. They exclude process startup,
source hashing, metadata SQL, candidate sorting and output serialization. The
receipt's 4.255-second elapsed field covers worker measurements and final guards;
it excludes initial hashing/metadata preparation. Full hashes and the first old
pass warmed the file cache, and concurrent ingestion/other host work was not
isolated. These are paired observations, not repeated throughput estimates or
an end-to-end request latency guarantee.

Peak `ru_maxrss` is the process high-water RSS through scoring, including mapped
resident pages, imports and score buffers. It is neither allocation accounting nor
whole-host memory. The optimization retains a full 7,463,940-byte score array and
bounds gathered matrix blocks; it avoids mapping all vector pages into resident
working sets for these scopes. No index bytes, ranking floor or model defaults
were changed by this experiment.

The recorded macOS pairs were bit-exact. This is an observed property of those
inputs/backend, not a cross-platform arithmetic guarantee. Linux OpenBLAS CI
observed one float32 ULP (5.96e-8) on a tiny four-dimensional final block. The
portable regression therefore checks rounding-level score tolerance, exact
eligible masks, unchanged candidate IDs/order and bounded matrix reads. Very
near score ties or cutoffs may differ across BLAS kernels/platforms; this change
does not establish universal bit-exact ranking at such boundaries.

```sh
uv run python scripts/benchmark_scoped_vectors.py --self-test
uv run python scripts/benchmark_scoped_vectors.py
```

The real run requires the pinned source SHA and 1,308 indexed editions. It refuses
nonempty source WALs and existing output/report paths. Reproductions should use
new `--output` and `--report` paths to retain the original receipt. The tiny self-test
creates its own temporary SQLite/NumPy fixture and checks all subprocess/guard and
exact comparison paths without original-library access.

Results: [`experiments/scoped-vector-v1.json`](../experiments/scoped-vector-v1.json).
Scratch score arrays and per-worker metrics remain under ignored
`data/bench-scoped-vector-v1-ownedmask/` (final trial) and
`data/bench-scoped-vector-v1/` (initial trial), roughly 45 MB of score payloads per trial.
