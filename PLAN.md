# Plan

Goal active: continue iterations until verified weekly Codex remainder <=3%, user stop, or environment limit.

1. ✓ Vertical slice: permitted demo → resumable import → lexical search → exact citations → reader + saved research.
2. ✓ Baseline + separate heldout fixtures, browser desktop/mobile, source provenance, type/lint/unit checks.
3. Now: publish public GitHub; finish first multilingual embeddings experiment; fix natural no-answer false matches while preserving frozen baseline.
4. Parallel: expand full EPUB corpus (20 editions/19 works), rights-aware offline discovery, thematic map/related books.
5. Next: compare retrieval modes on dev, measure indexing/latency/memory, use heldout once for release verification; CI check.
6. Then: broader query judgments, semantic chunking/model context, optional reranking and ANN capacity benchmarks; improve ingestion fidelity and near-duplicate/work matching based on measured errors.

Ownership: lead library/API/CLI/semantic/integration/docs; corpus agent parsers/sources/catalog/demo; UI agent static/explore/browser; evaluation agent metrics/fixtures/retrieval/CI. Avoid overlapping edits.
