# Search availability and source refreshes

An edition's searchable state and the latest acquisition attempt have separate
records. `books.status='indexed'` identifies the last successfully indexed
edition. Its metadata, section/passages, source hashes, archived `local_path`
and timestamp remain current while a replacement is downloaded or parsed.

`source_pipeline` stores one latest attempt per book: `stage`, `local_path`,
`error` and `updated_at`. `Library.stage()` updates that record and appends the
existing event history. For an already indexed book it preserves the edition
record, including when the provider is unavailable, a retry is deferred, an
archive fails verification or the parser rejects new input. Calling `stage()`
again without a path retains the attempted path rather than borrowing the
current edition's archive path.

For a book without an indexed edition, legacy `books.status` transitions remain
visible: discovered, downloaded, processed, failed/deferred, indexed or
duplicate. A failed first import does not become searchable. An explicit
administrative invalidation of an indexed edition requires changing its
availability record; an ordinary refresh error is no longer an invalidation.

Successful imports publish the replacement metadata, sections, passages,
source hashes, archived path, index-version fingerprint and indexed pipeline
state in one `BEGIN IMMEDIATE` transaction. Deduplication remains inside that
transaction. A successful duplicate replacement publishes a `duplicate` edition
and pipeline state together; its removed passage IDs are not left searchable.
A rolled-back attribution or metadata error leaves the previous FTS rows and
saved-passage links intact. An unchanged successful retry also clears a previous
failed/deferred pipeline state. Saved quotations continue to use their frozen
artifact hashes after a successful replacement.

The pipeline table has a cascading foreign key to `books`, so snapshot subset
cleanup removes its rows together with excluded editions. Startup migration
backfills only the current legacy status/path/error. It never infers that a
legacy failed row is safe to restore merely because old sections remain.
Migration writes the schema: open a copy of a frozen evaluation database,
preserving the published original and its file hash.

## API contract

`GET /api/books/{id}` and each entry in `GET /api/books` add:

| Field | Meaning |
| --- | --- |
| `status` | Current edition's availability state, unchanged contract |
| `local_path` | Current edition's acquired archive after a successful import |
| `ingestion_status` | Latest source attempt stage |
| `ingestion_error` | Latest failed/deferred attempt error, otherwise null |
| `ingestion_local_path` | Latest attempt's input or archive path; may differ from current edition |
| `ingestion_updated_at` | SQLite timestamp of its latest transition |

`GET /api/books?status=indexed` continues to list all searchable editions,
including those with failed refresh attempts. Optional
`ingestion_status=failed` filters by the latest attempt separately; combine the
two parameters to find searchable editions awaiting a successful refresh.
Pagination totals use the same combined filters as the returned page.

`GET /api/stats` retains `books` and `status_counts` based on availability, adds
`searchable_books` as an explicit indexed-edition count and adds
`ingestion_status_counts` for the latest attempt states. An indexed edition with
a failed refresh contributes to `status_counts.indexed` and
`ingestion_status_counts.failed`. These counters describe different facts.

Verified search passages expose `ingestion_status` and `ingestion_error`.
When a failed or deferred refresh leaves the previous edition available, they
also carry `ingestion_warning`; search responses include its text once in
`warnings`. This applies to lexical/baseline results and semantic/hybrid evidence
without changing vector fingerprints, encoding, ranking or thresholds. Citation
metadata and artifact hashes always refer to the shown successful edition.

## Verification

```sh
uv run pytest tests/test_pipeline_availability.py tests/test_library.py tests/test_snapshots.py
```

The fixtures use temporary owned libraries and original CC0 text. They cover
invalid/empty/missing sources, malformed FB2, corrupt candidate archives, metadata
failures before and inside the transaction, availability during parsing,
unchanged retry, successful replacement, deduplication, archive downloads,
pipeline/availability counters and filters, migration and cascading snapshots.
No live corpus, published evaluation database or ingestion process is changed.
