# Architecture and retrieval decisions

Python 3.12+, FastAPI, browser-native HTML/CSS/JS, SQLite with WAL and foreign keys. No remote generative service. On the initial host: 16 GiB RAM, 10 CPU.

## Evidence model

A book record represents an edition. A curated `work_id` groups editions and translations. Original downloaded file SHA-256 and normalized section-content SHA-256 have different roles: artifact provenance and exact-content deduplication. The source manifest holds acquisition URL, rights URL, edition and retrieval date. Source provenance is retained with the corpus catalog; the original artifacts stay in the configured data directory.

Each section has canonical Unicode text, an ordinal and a heading. Passages store nonoverlapping character spans, exact copies and SHA-256 digests. Search rechecks the section slice and digest before returning evidence. The independent evaluator checks this again, plus citation metadata and reader URL identifiers. This establishes integrity relative to the imported canonical edition; it does not prove OCR/transcription is error-free or establish real-world print page numbers.

Paragraph-aware chunks target 1,200 characters, capped at 2,000 and split on sentence boundaries when needed. Exact positions are Python Unicode codepoints. Browser reader must translate correctly when slicing JavaScript UTF-16 strings (astral characters need care). Unnumbered ebooks cite chapter and character offsets; no invented print page numbers.

## Baseline

FTS5 `unicode61 remove_diacritics 2`, BM25 weights text/title/author/chapter. Natural-language tokens are escaped, joined with OR and bounded to 40. Lower BM25 values rank earlier. Unicode tokenization does not provide multilingual morphology; common words can yield irrelevant hits. Baseline is intentionally frozen in `experiments/baseline-dev.json` on the demo corpus.

References: [SQLite FTS5](https://sqlite.org/fts5.html) documents contentless deletion and BM25. Deletes supply the original indexed fields before section removal; indexing and FTS mutations share a transaction.

## Semantic experiment

[Multilingual MiniLM](https://www.sbert.net/docs/sentence_transformer/pretrained_models.html) supports 50+ languages according to its authors. This is a model capability, not measured project quality. Local normalized embeddings, dot product cosine similarity, optional RRF with constant 60. A configurable `BOOKRAG_SEMANTIC_THRESHOLD` (initially 0.30) removes weak cosine candidates; it is not a calibrated probability of answerability.

Context prepends title/author/section. The model's token limit can truncate long passages. This is an explicit early experiment limitation; sentence-sized windows or a longer-context retrieval model should be compared on the development split before replacement.

Indexes use immutable generation directories and an atomically replaced `active.json`. Corpus identity hashes passage IDs and text digests. Changed corpus triggers an explicit lexical fallback. No model is loaded for baseline search. Building vectors uses bounded batches and memory-mapped arrays; exact matrix scan is linear and requires benchmarking before large-scale use. Interrupted generations are removed; downloaded model blobs remain cacheable.

## State and scale

Discovery/download/processed/indexed/duplicate/failed states are independently recorded. A failed book does not abort the remaining manifest. Indexing is transactional per book. Whole job reruns skip matching file hashes. Exact duplicates are suppressed; near-duplicate editions and automatic work identity remain future work.

At 10–50k works the number of passages, not just works, determines index size. Expansion should measure disk, build time, query memory, latency and retrieval quality before changing backend. SQLite remains the metadata/evidence authority even if ANN vectors move to another local engine. No claims about target-scale performance have been made.
