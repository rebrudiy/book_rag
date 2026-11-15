# Reproducible retrieval evaluation

`evals/dev.json` is the development fixture. `evals/heldout.json` is a separate final check. Both are small, manually labeled queries over the demo excerpts; neither represents quality over a large general library. The held-out queries must not guide query expansion, synonyms, score weights, chunking, or prompt changes. Expand the test corpus and ground truth before making broader quality claims.

Run the `bookrag evaluate` command against a local index, select the dataset and search mode, and save JSON under `experiments/`. Compare the lexical baseline and any candidate on exactly the same corpus snapshot, fixture digest and `k`. Runtime reports include the fixture SHA-256 and library counts. Model identity and index revision must accompany semantic experiments. Run each mode in a fresh process for meaningful memory comparisons.

## Preserve frozen input bytes

`bookrag evaluate` opens an owned temporary evaluation copy. It does not construct
`Library` over the source directory: that constructor applies current schema
migrations, which would otherwise change a published SQLite byte hash even for
a read-only evaluation. Use a new report filename outside the source library:

```sh
BOOKRAG_DATA_DIR=data/snapshots/curated20-v3 \
BOOKRAG_MODEL_CACHE="$PWD/data/models" uv run bookrag evaluate \
  --dataset evals/dev-expanded.json --mode hybrid \
  --output experiments/expanded-hybrid-new-run.json
```

The evaluator copies only the SQLite main file and an existing nonempty WAL, without
opening either input through SQLite. It checks file identities around capture;
SQLite reconstructs shared memory and runs migrations only in the temporary
directory. Subsequent evaluation connections are read-only. A changing input
database, active vector selection or selected generation rejects the run. Use a
stable snapshot rather than an ongoing ingestion/build directory.

The selected `active.json` is captured as owned bytes. Its ready generation is
reused through a local reference; existing semantic retrieval opens arrays with
`mmap_mode='r'`. Arrays are neither rebuilt nor physically copied. The model
cache gets owned metadata and references to completed files larger than 1 MiB,
so existing weights are reused while loader metadata writes stay in the
temporary cache. `BOOKRAG_MODEL_CACHE` is restored when the context exits. The
temporary database, metadata and links are removed on success or failure; source
artifacts are not copied because quote checks use canonical SQLite sections.

Hugging Face snapshot files commonly use relative links into a sibling `blobs`
directory. The copy checks dangling targets relative to the original link's
parent, retaining valid snapshot paths. Small configuration files become owned
regular files; large completed targets are referenced read-only. This matters
offline: dropping valid relative links makes the model unavailable despite
having the weight blobs. An isolated offline real MiniLM load/encode and a
synthetic HF-layout regression verify this path. Requested semantic modes that
all fall back to words are explicitly reported and do not measure semantic
relevance.

Installed subset indexes retain their target database path/inode binding and
sealed generation receipts. They cannot be rebound implicitly to a temporary
database or validated through a symlinked generation directory. For an explicit
partial-index context, `EvaluationLibrary` therefore supplies the internal typed
`semantic_reference`: a read-only reference to the **actual original** directory
and database. Partial receipt checks, epoch guards and vector memory maps use
that original identity; quotations, lexical retrieval and bibliographic checks
continue reading the owned copy. The model metadata cache remains owned. No
temporary DB claims to have the original inode, and no index binding is rewritten.
Legacy/full-index evaluation does not receive this attribute and keeps its
existing owned-copy path.

Partial-index references require a stable checkpointed original with absent or
empty WAL. Their SQL connections use `mode=ro&immutable=1` and `query_only=ON`,
so SQLite cannot create or change original WAL/SHM files. Ordinary `mode=ro`
can update shared-memory read marks; `immutable=1` would ignore committed data
in a nonempty WAL. Such partial inputs are rejected explicitly. The evaluator
never checkpoints, writes or repairs the source. Stop writes to the live source,
checkpoint through its normal operational procedure, then retry. Alternatively,
prepare a checkpointed copy and explicitly reinstall a compatible ready index
to establish **that copy's** binding. Copying a DB and its partial active pointer
alone is not a valid recipe. A checkpoint does not repair a stale coverage proof.

Original main/nonempty-WAL, selected metadata/generation and the installation receipt
are checked around reference reads and at evaluation exit. SHM and zero-byte WAL
are coordination sidecars that external read-only clients can create, update or
remove without changing canonical data. They are not input-identity gates;
immutable reference connections never open them. A WAL becoming nonempty still
rejects the run, even without a known committed frame. Any canonical source change
discards the run, including additions outside semantic coverage: a frozen
evaluation must still compare the same complete lexical corpus. Corrupt or stale
partial bindings retain the explicit lexical fallback and its actual-mode report;
they are never silently promoted or rebound. These rules add no vector copy,
model inference, schema migration or paid API use on the original.

New reports include `evaluation_input`: original SQLite main/WAL SHA-256 values,
active metadata SHA-256, generation identity and reuse strategy. A nonempty WAL
is part of the captured source; the main-file digest alone does not identify its
committed contents. Generation changes are checked by file identities without
an extra full read of large arrays. Tests independently hash all fixture input
files before/after, including vectors, models and SQLite sidecars.

Python callers can use `evaluate(Path("data/snapshots/curated20-v3"), dataset,
mode="hybrid")` or `with evaluation_library(path) as library:`. Passing an
already constructed `Library` still supports existing callers, but cannot undo
schema writes that happened before the evaluator received it. Never create that
object directly over a published frozen input. Evaluation contexts serialize
their model-cache environment routing within the process; use fresh dedicated
processes for independent experiments.

```sh
uv run pytest tests/test_evaluation_isolation.py tests/test_evaluation.py
```

These checks include a pre-migration database, a committed nonempty WAL,
semantic retrieval with read-only memory maps, CLI and calibration execution,
cache metadata isolation, byte-hash preservation and cleanup after failure.
Real tiny installed-partial fixtures additionally check actual hybrid execution,
original-versus-owned identity after an owned-only schema migration, untouched
original SHM/model/vector bytes, pending-WAL rejection and source-change discard.
Historical reports, dataset labels and frozen source files are not rewritten.

Dataset version 1 contains `split`, a list of `queries`, and optional descriptive fields. Each query has `id`, `query`, explicit `answerable`, `language`, `type`, and `relevant_work_ids` or `relevant_book_ids`. `evidence_contains` optionally requires one of the listed source phrases in a relevant passage. A correct work alone is insufficient for a scene or exact-quote hit when this condition is present. Optional `filters` select a book or language. Empty results constitute the non-generative search baseline's abstention signal.

The evaluator records:

- MRR@k and hit@k over answerable queries, including scene, idea, quote, description and cross-language cases.
- Work recall@k over explicitly labeled works, independently of the phrase condition; precision@k uses the conventional denominator `k`, even when fewer results return.
- Negative abstention accuracy, separately from answerable retrieval scores. Semantic search may return irrelevant neighbors; their presence must not be treated as evidence of an answer.
- Per-query type and query-language breakdowns. The language label describes the query, which can differ from the source text.
- Exact quote, bibliographic metadata and reader-anchor rates over every returned passage, including irrelevant ones.
- End-to-end retrieval p50/p95 wall time and process peak resident memory. Memory includes Python and imported modules; this is not incremental model memory. These numbers do not measure index-build cost or paid API cost.

Citation verification independently loads the book and section and checks `section.text[start:end] == result.text`, correct title/author/edition, their presence in the displayed citation, section title, and all three reader query parameters. It never trusts the search response's `verified` boolean. Offsets are zero-based Unicode code points into canonical parsed text, not UTF-8 byte counts, printed page numbers, or PDF coordinates. This proves fidelity to extracted text; it cannot establish OCR accuracy or validate a physical edition.

Keep measured failures in reports. In particular, cross-language lexical failures and out-of-corpus semantic false positives are expected limitations to investigate, not cases to relabel away. A larger catalog changes retrieval difficulty and must have a separate evaluation run. A score from eight excerpts cannot be transferred to thousands of books.

The automated evaluation tests deliberately corrupt quotes, offsets, attribution and reader anchors and assert that verification detects the corruption. They also check rank metrics and avoid reporting perfect citation rates when there are no returned passages.

Reports include a corpus digest and source snapshot (artifact/content hashes and bibliographic metadata), Python-module hashes, Python/platform information, actual returned search modes, and embedding model names when supplied by retrieval. Requested semantic search that falls back to lexical search is visible in `actual_search_modes` and per-query warnings. `compare_reports(baseline, candidate)` rejects different dataset/corpus digests or `k` and returns signed metric deltas. Pin model revisions and retain model-build metadata for full semantic reproducibility.

The initial development fixture contains 23 queries: 18 answerable questions, including 4 cross-language questions, and 5 negatives. Some negatives use normal question wording with common function words; a keyword search must not be credited with correct abstention solely because artificially exotic query tokens never occur. The held-out fixture contains 13 separate questions and is reserved for the final check of a frozen candidate. Evaluation tests ensure labeled evidence phrases actually occur in the checked-in demo source and query text does not overlap across splits.
