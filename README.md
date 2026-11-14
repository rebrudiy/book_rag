# Book Atlas · Атлас книг

An open, local-first library for finding passages, reading books in context and assembling research with verifiable sources. Search works without a generative model, API key or paid service.

**Current scope:** functional prototype, not yet a 10–50k-work library. Includes eight small public-domain demo works/excerpts in English, French, German and Russian. Runtime books, models, indexes and user notes stay outside Git.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12+:

```sh
git clone https://github.com/rebrudiy/book_rag.git
cd book_rag
uv sync --extra dev
uv run bookrag demo
uv run bookrag serve
```

Open **http://127.0.0.1:8000**. The initial demo requires no network after installing dependencies. Commands below assume a source checkout. `BOOKRAG_DATA_DIR` defaults to `./data`; environment variables are documented in [.env.example](.env.example). `.env` files are not auto-loaded: export the variables in your shell.

## What works

- SQLite FTS5 lexical search with language/book filters and independent search results.
- Every result includes author, title, edition, section and Unicode character offsets, a verified exact passage and a reader link.
- Structured TXT, HTML, EPUB, FB2 and text PDF import. Scanned PDFs need OCR outside this prototype.
- Resumable discovery/download/import, artifact and normalized-content hashes, duplicate detection and explicit pipeline counts.
- Reader with chapter navigation and highlighted source location.
- Extractive research board across books: verified evidence, with explicit empty results. No generated prose or inferred author positions.
- Durable fragments, queries, annotations and collections in local SQLite.
- Optional local multilingual semantic search and reciprocal-rank fusion; explicit lexical fallback when vectors are missing/stale.
- Reproducible development and held-out evaluation, independent citation checks and CI.

## Corpus

Read [source provenance and rights](docs/SOURCES.md) before downloading or redistributing texts. Software is MIT; books retain their own rights. Project Gutenberg public-domain determinations are for the USA; check applicable rights elsewhere.

```sh
# Inspect the manifest before downloading. Full books are separate from demo experiments.
BOOKRAG_DATA_DIR=./data/full uv run bookrag ingest corpus/manifest.json
BOOKRAG_DATA_DIR=./data/full uv run bookrag stats
BOOKRAG_DATA_DIR=./data/full uv run bookrag serve

# Import your own permitted text, with explicit metadata/provenance
uv run bookrag import /path/to/book.epub --metadata /path/to/metadata.json
```

Metadata JSON requires a stable `id` and should include `title`, `author`, `language`, `work_id`, `edition`, `source_url`, `rights` and `rights_url` (see the demo manifest). Share a `work_id` across translations/editions. Work identity is currently curated; automated fuzzy matching is not implemented.

Imports register the entire manifest as discovered, then process books independently. Rerun the same command after interruption. Failed entries keep an error and can be retried. Unchanged indexed files are skipped; canonical exact duplicates are recorded separately. Downloaded does not mean searchable: only successful transactional indexes count toward searchable books.

## Optional multilingual retrieval

```sh
uv sync --extra dev --extra semantic
uv run bookrag embed
uv run bookrag search 'девочка следует за белым кроликом' --mode hybrid
```

Uses a local Sentence Transformers multilingual MiniLM model (first run downloads weights, no paid API). Model, cache and vectors live beneath the configured data directory. Embedding construction streams batches into a memory-mapped matrix; generation activation is atomic. A corpus fingerprint prevents stale vector use. The initial exact cosine index is for experiments; 10–50k full books will require an ANN backend and measured capacity planning. Long passages may be truncated by the model; see [architecture](docs/ARCHITECTURE.md).

## Verify

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run bookrag evaluate --dataset evals/dev.json --output experiments/local-dev.json
# Final evaluation is deliberately separate; do not tune against this split.
uv run bookrag evaluate --dataset evals/heldout.json --output experiments/local-heldout.json
```

[Evaluation protocol](docs/EVALUATION.md) explains relevance metrics, cross-language and negative cases, independent quote/link checks, latency and memory. The small curated fixture measures regression behavior, not general book-search quality. [PLAN.md](PLAN.md) and [STATUS.md](STATUS.md) track the next work and measured limitations.

## Local API

Interactive schema: http://127.0.0.1:8000/docs. Key routes: `/api/search`, `/api/books`, `/api/books/{id}`, `/api/passages/{id}`, `/api/research`, `/api/saved`, `/api/stats`. Default binding is loopback. Authentication and multi-user access are not implemented; this is a personal local research application.

Contributions: include a use case, provenance for any text fixtures and relevant tests. Do not commit corpus downloads, models, personal collections, environment files or credentials.
