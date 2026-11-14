# Status

Updated 2026-11-14, iteration 1. Goal active; continue autonomous development.

## Working and verified

- Python/FastAPI/SQLite app, lexical search, structured import, exact section citations, reader, local saved queries/fragments/collections, extractive research.
- 8 demo works/excerpts, 4 languages, 45 passages. `data/` is the reproducible demo library; full-book expansion runs separately in `data/full/`.
- 53 automated tests pass; core type/lint checks pass. Chromium desktop/mobile: 9 real workflows passed, zero JS errors, no horizontal overflow at 390px. Screenshots ignored under `artifacts/browser/`.
- Initial dev baseline: 23 queries, 18 positive, 5 negative. MRR/hit@5 .7778; cross-language 0/4; negative abstention .6; exact citation/metadata/reader anchors 1.0. Small manually grounded regression set, not general quality evidence. Held-out never executed yet.
- Fixed review finding: edition/source/rights/work metadata now update atomically with reimport (including unchanged file metadata changes); regression tests added. Complete acquisition metadata retained in `book_metadata`.

## Active parallel work / next tasks

1. Lead: finish local multilingual embedding build; measure semantic/hybrid vs immutable baseline and then improve retrieval. Model weights currently downloading into ignored data/models.
2. Corpus agent: 20 legally checked full-book EPUB catalog (19 works) ready; ingestion active in data/full; scalable rights-aware offline Gutenberg catalog discovery underway.
3. Evaluation agent: general lexical preprocessing/reranking module (baseline preserved), independent tests; integrate once contracts arrive.
4. Interface agent: lexical thematic map and book similarity with transparent labels, then browser verification.
5. Publish meaningful Git commits and GitHub repository; add CI and document remaining limitations.

## Runtime / limits

Hardware macOS 16 GiB RAM / 10 CPU. All dependencies in project .venv (uv bootstrap in ignored .bootstrap). No paid API.
Run: `.venv/bin/bookrag serve` → http://127.0.0.1:8000. Bootstrap: `uv sync --extra dev`, `uv run bookrag demo`.
Weekly Codex quota AVAILABLE from current session token_count rate_limits window_minutes=10080: latest observed 67% remaining. Check between iterations using `.venv/bin/python scripts/check_codex_limit.py <current rollout JSONL>`. Stop new work at <=3%; never substitute token counts.

## Known limits

Small corpus; 10–50k work target not reached. Demo mostly excerpts. No OCR engine or generative answering. Work/translation linking curated. Exact duplicates only. Baseline morphology/common-word handling weak. Optional embedding model truncates long chunks; exact cosine scan not yet scale-tested. Single-user loopback service; no auth. Evidence integrity verifies canonical imported text, not source transcription or print page numbering.
