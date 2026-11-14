# Status

Updated 2026-11-14, iteration 2. Goal ACTIVE; continue autonomous development.
Public GitHub: https://github.com/rebrudiy/book_rag. Initial commit a223654 and its CI passed.

## Verified behavior

- Local FastAPI/SQLite app: structured import, search, exact citations, reader, saved collections, Markdown exports with frozen provenance, lexical thematic map.
- Demo: 8 curated works/excerpts, 4 languages, 45 passages. Full corpus separate in data/full.
- Initial full expansion: 220 indexed editions, 219 provisional catalog work groups, 80,556 passages before parser refresh. Periodical issues and unresolved editions mean this is NOT 219 confirmed unique books.
- Parser/dependency/chunker/contributor fingerprints now force correct reindexing. Explicit contributor rules distinguish Jowett's editorial introduction from Plato's dialogue; API/citations/reader carry section author and role.
- 215 automated tests pass; Ruff/format/ty pass. Latest browser runs cover 15 workflows, desktop/mobile, Russian hybrid search, exact reader mark, map and source-preserving download; zero JS errors. Full actual Jowett browser check underway.
- Dev 23 queries: baseline MRR/hit@5 .7778; hybrid window v2 MRR .8935, hit 1.0, cross-language 4/4, negatives 5/5, citation integrity 1.0.
- Frozen heldout 13 queries, release-v0.2: hybrid MRR .9333, hit 1.0, cross-language 2/2. Negative abstention .6667: one scoped semantic false positive. No tuning on heldout.
- Optional pinned local reranker: paired dev candidate-50 control MRR .9213 → .9352; cross-language .6458 → .8333, but precision regression, one explanation regression, p50 31.9 → 108.7 ms, peak RSS 833 → 1327 MiB. Optional only, default remains hybrid. No new heldout run.
- Offline index benchmark: 20 EPUBs / 12,106 passages in 2.578 s, peak process RSS 105.19 MiB; lexical p50/p95 37.91/81.45 ms. Small noisy sample, no target-scale claim.
- Map cache on 220-edition snapshot: 8581.63 ms cold → 6.87 ms cached. Bounded four snapshots, mutation-safe, no caching if corpus changes during construction.

## Active work / exact next actions

1. Lead: publish iteration 2 after final checks, track ingestion to 1,000 editions; full refresh includes parser/contributor upgrades. Current job logs data/expansion-1000-progress.jsonl, report data/expansion-1000-report.json.
2. Corpus agent: acquire selected D2L modern science chapters from pinned CC BY-SA source with hashes/provenance and structured derived FB2; new module/tests/docs only.
3. UI agent: actual full-corpus Jowett attribution browser check and optional reranker selector; final smoke.
4. Evaluation agent: reranker experiment complete; available for next independent review.
5. Then freeze expanded corpus, build multilingual windows and benchmark scale. Broaden development judgments and create a new sealed final split before further relevance tuning. Work/translation identity and near-duplicates remain research tasks.

## Runtime and quota

Run: `uv sync --extra dev --extra semantic`; `uv run bookrag demo`; `uv run bookrag embed`; `uv run bookrag serve` → http://127.0.0.1:8000.
Full corpus: `BOOKRAG_DATA_DIR=./data/full uv run bookrag serve --port 8001`.
All dependencies in .venv (uv bootstrap .bootstrap ignored); downloaded corpus, models, notes and indexes ignored. No paid services. Hardware 16 GiB RAM / 10 CPU; ~30 GiB free disk at last check.
Weekly quota AVAILABLE in current session JSONL, rate_limits 10,080-minute window: latest 65% remaining. Check via `.venv/bin/python scripts/check_codex_limit.py <current rollout>` between iterations. Stop new work at <=3%; never substitute token counts.
Downloads default to 2-second intervals, validated cache, bounded retries; long Retry-After persists a host deferral across restarts. Ingest reserves 4 GiB disk and leaves pending sources visible.

## Limits

10–50k unique-work target not reached. Demo mostly excerpts; catalog identities provisional. No OCR engine, generative prose or automatic stance claims. Exact quotations verify imported canonical text, not source transcription or print page numbers. The map is same-language lexical similarity, capped at 500 editions. Exact vectors are linear scan; full-corpus semantic index not built yet. Single-user local app, no authentication. Known heldout semantic false positive retained in reports.
