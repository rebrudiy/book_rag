# Integration journeys

`tests/test_integration.py` uses a tiny EPUB authored for the tests with explicit CC0 provenance. It needs no network, embeddings, model download or external account. The tests exercise actual SQLite storage and FastAPI endpoints through `TestClient`; they do not substitute mocked search responses.

Run:

```sh
uv run pytest tests/test_integration.py -q
```

Four scenarios cover the boundaries between features:

1. Import a two-chapter EPUB, search a quoted phrase with a book/language filter, resolve the reader URL, and verify exact chapter-text code point offsets. The chapter contains non-BMP emoji and accented Latin text, and the test explicitly distinguishes UTF-16 length from Unicode code point length. Save evidence and a query into two collections. Export one collection through HTTP and CLI and compare the results, including source/rights URLs, artifact hash, safe note rendering and live reader link. Reconstruct the app against the same database and verify persistent notes and search.
2. Correct edition metadata without changing artifact bytes. Reindexing must replace the citation, author, title and source while the original saved note retains its original exact text, edition and source URL. The old passage ID must become unavailable, and its exported note must omit a stale reader link. Changing the parser fingerprint also forces reindexing without changing the source bytes. A fresh app still sees the original note snapshot.
3. Attempt import of a missing source, reconstruct the app, and verify a visible failed book with no searchable passages. Create the source and retry; it becomes indexed and the error clears. This tests recoverability across separate app instances and SQLite connections.
4. Find a book by title metadata while refusing to treat an unrelated preface as research evidence. Confirm an out-of-corpus natural-language question has no results or research answer. Check book/language filters and Unicode author discovery.

The integration audit found a real Unicode discovery defect: SQLite `LIKE` does not provide Unicode case-insensitive substring matching, so lowercase Cyrillic queries failed to find mixed-case Cyrillic author names. The library now uses deterministic Unicode case folding and literal substring matching. The regression assertion passes. All four integration journeys pass after the fix.

The independent evidence verifier also has adversarial tests where Python's clamped slice equals the alleged quotation despite invalid offsets. Excessive end offsets, negative starts, boolean offsets and empty ranges must all be rejected. Quotation equality alone is insufficient.

These are API/storage/CLI checks. They do not verify visual layout, browser JavaScript selection rendering, OCR against printed pages, process-kill crash recovery, simultaneous clients, or semantic relevance. Browser QA and retrieval evaluations remain separate evidence.

## Read-only metadata performance observation

During corpus ingestion on 2026-11-14, a read-only snapshot of `data/full/library.sqlite3` contained 194 indexed editions, 193 works and 67,171 searchable passages (230.74 MiB main database). Twenty-five warm direct-SQL repetitions of the metadata-filter path measured median 0.070 ms for two author matches, 0.049 ms for two title matches and 0.036 ms for no matches. Ten unfiltered metadata-list repetitions returned 220 catalog records, including lifecycle states, with median 1.590 ms and maximum 4.631 ms. The book-to-passage lookup index was present.

These are small warm local metadata measurements taken while ingestion was active, not end-to-end HTTP latency or a guarantee at 10–50k works. They do not assess full-text retrieval, semantic scoring, page transfer size, browser rendering or memory under concurrent use. A larger catalog should be measured independently with pagination and realistic workloads.
