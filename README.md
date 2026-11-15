# Book Atlas · Атлас книг

An open, local-first library for finding passages, reading books in context and assembling research with verifiable sources. Search works without a generative model, API key or paid service.

**Current scope:** functional prototype, not yet a 10–50k-work library. Includes eight small public-domain demo works/excerpts in English, French, German and Russian. Runtime books, models, indexes and user notes stay outside Git.

[Measured large-library MVP](docs/LARGE_LIBRARY.md): the 4,001-edition checkpoint has a verified semantic subset of 1,308. A [resumable acquisition queue](docs/CORPUS_SCALE.md) is expanding the local corpus toward10,000 candidate works; live counts are in `/api/stats`, and provisional identities do not establish unique-work totals. Runtime books are acquired separately from the demo. [Development measurements](docs/LARGE_RETRIEVAL.md) keep gains, regressions and a known global-description failure visible.

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
- Durable fragments, queries, editable notes/titles and thematic collections in local SQLite. Edits preserve the exact quotation and its provenance.
- Optional local multilingual semantic search and reciprocal-rank fusion; explicit lexical fallback when vectors are missing/stale.
- Optional [local generated answers](docs/ANSWERS.md) from retrieved evidence, with mandatory exact supporting quotations, source links, abstention and source-version checks. Generated paraphrases can still be wrong; only source quotations are verified.
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

Imports register the entire manifest as discovered, then process books independently. Rerun the same command after interruption. Failed entries keep an error and can be retried. Unchanged indexed files are skipped; canonical exact duplicates are recorded separately. Downloaded does not mean searchable: only successful transactional indexes count toward searchable books. A failed refresh preserves the last successfully indexed edition; [attempt state and current availability](docs/PIPELINE.md) are recorded separately.

Imports preserve the exact acquired bytes in a [content-addressed original archive](docs/ARTIFACTS.md). Reader and saved-fragment source links can download the verified original; older saved fragments retain their original artifact after an edition is replaced. Chapters, code blocks, verse and table rows are retained during extraction. This does not establish the correctness of the provider's transcription.

Newly saved quotations also preserve the exact canonical book version. **Читать сохранённое издание** opens its original chapter, highlight, contributor attribution and source after the live book is replaced or removed. Historical chapters load individually and are verified before display. This archive is created when saving, rather than for every downloaded book; earlier notes without an archived version keep their quotation snapshot. See [historical versions](docs/CANONICAL_VERSIONS.md).

## Optional multilingual retrieval

```sh
uv sync --extra dev --extra semantic
uv run bookrag embed
uv run bookrag search 'девочка следует за белым кроликом' --mode hybrid
```

Uses a local Sentence Transformers multilingual MiniLM model (first run downloads weights, no paid API). Model, cache and vectors live beneath the configured data directory. Embedding construction streams batches into a memory-mapped matrix; generation activation is atomic. A corpus fingerprint prevents stale vector use. The initial exact cosine index is for experiments; 10–50k full books will require an ANN backend and measured capacity planning. Long passages are covered by overlapping tokenizer windows; optional reranking can still truncate long query/passage pairs. See [architecture](docs/ARCHITECTURE.md).

Interrupted builds resume from durable batch checkpoints. Later builds can [reuse exact unchanged embedding inputs](docs/VECTOR_REUSE.md) from a validated generation with the same pinned model and profile. Share model weights across separate live and experimental libraries with `BOOKRAG_MODEL_CACHE=./data/models`.

A completed frozen index can also be [installed over its verified edition subset](docs/INDEX_INSTALLATION.md) in a larger live library. The interface shows semantic coverage; hybrid search still searches words across every edition. Adding an unrelated book preserves the subset index. Changes to covered text invalidate it and retain lexical search. An unfinished checkpoint cannot be installed.

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

## Larger offline discovery and research export

Download the [official RDF catalog](https://www.gutenberg.org/ebooks/offline_catalogs.html) once, following [provider harvesting guidance](https://www.gutenberg.org/policy/robot_access.html), then discover without crawling the website:

```sh
uv run bookrag discover data/catalog/rdf-files.tar.bz2 --output data/catalog/queue.json --limit 200 --language en --language ru
BOOKRAG_DATA_DIR=./data/full uv run bookrag ingest data/catalog/queue.json
uv run bookrag export --collection 'Research' --output data/exports/research.md
```

Discovery requires explicit per-record public-domain rights; ordinary CSV metadata without rights is skipped. Reruns extend the queue. Downloads use a two-second interval, bounded retries, validated cache sidecars and atomic completion. Imports stop before free disk falls below 4 GiB; queued records remain discoverable. Parser/dependency/chunker fingerprints ensure upgraded extraction rules trigger reindexing even when files are unchanged.

The theme map shows shared vocabulary and lexical similarity within a language. It does not infer author agreement or opposition. Catalog work groups remain provisional unless curated; discovered periodical issues are labeled separately. See the [corpus audit](docs/CORPUS_AUDIT.md) for observed source and attribution defects.

[Retrieval experiments](docs/RETRIEVAL.md) record both gains and regressions, including a semantic false positive on the frozen held-out split. The default UI requests hybrid search and clearly shows lexical fallback when the optional index is unavailable. The original lexical baseline remains available with `--mode baseline`.

For an optional, slower local cross-encoder ordering pass, use `uv run bookrag search 'your research query' --mode reranked`. The model is revision-pinned and downloaded locally on first use. [Paired development measurements](docs/RERANKING.md) show a small ranking gain with higher memory/latency and an observed regression; this is an explicit option, not the default.

Contributor attribution can be supplied through audited `section_attributions` metadata rules. The curated Republic marks Jowett's introduction as editorial rather than attributing its prose to Plato. Unclassified sections retain catalog authorship as context, without claiming verified section authorship.

An explicit section rule can also identify that section's translator. For example, a poem quoted as an epigraph can have a different translator from the surrounding novel; citations, the reader and saved provenance retain that distinction.

## Modern texts, frozen experiments and integrity

```sh
# Pinned CC BY-SA D2L selection; originals, licenses, code/math and source hashes retained
BOOKRAG_DATA_DIR=./data/full uv run bookrag acquire-modern --index

# Plan languages and subject areas from the same offline rights-checked catalog
uv run bookrag plan-balanced data/catalog/rdf-files.tar.bz2 --output data/catalog/balanced.json
# Prefer bounded text-only editions when illustrations are not needed
uv run bookrag plan-balanced data/catalog/rdf-files.tar.bz2 --text-only --per-language 1000 --output data/catalog/text3000.json

# A consistent SQLite online backup, optionally a manifest-filtered experimental subset
BOOKRAG_DATA_DIR=./data/full uv run bookrag snapshot data/snapshots/curated20 --manifest corpus/manifest.json --no-include-saved

# Verify every indexed source slice and rehash the original artifacts
BOOKRAG_DATA_DIR=./data/full uv run bookrag verify --artifacts --output data/integrity.json
```

Snapshots never overwrite an existing destination; originals/model weights remain outside them. See [modern-source provenance](docs/MODERN_SOURCES.md), [resumable vector builds](docs/SEMANTIC_INDEX.md), [balanced corpus planning](docs/CORPUS_BALANCE.md) and [expanded evaluation](docs/EXPANDED_EVALUATION.md). The evaluated 20-edition snapshot is frozen separately from the live expanding corpus.

The catalog fetches bounded pages; title/author filters query the server rather than a preloaded first page. Search scope and research selections remain selected when their matching suggestions change. Partial source selections are explicitly labeled.

Work groups inferred from catalog records remain provisional. [Authority-link proposals](docs/WORK_IDENTITY.md) retain evidence and ambiguity without changing counts automatically; only explicitly curated identities support links between editions and translations. A validated [alternative source format](docs/SOURCE_VARIANTS.md) can recover an oversized illustrated edition while retaining the same provider identity and declaring omitted illustrations.


Five complete Russian originals can also be acquired with captured page revisions and explicit rights records:

```sh
BOOKRAG_DATA_DIR=./data/full uv run bookrag acquire-russian --index
```

[Russian source documentation](docs/RUSSIAN_SOURCES.md) records original-body coverage, retained author notes, excluded unverified editorial annotations and the provider's access policy. This bounded connector handles five audited works; it does not crawl arbitrary Wikisource pages.

For a separate Russian Wikibooks review catalog, `uv run bookrag catalog-wikibooks` downloads a pinned 25.35 MB official export, checks its SHA and streams its pages into an offline chapter graph. This is discovery and source review: it does not add books to search or assert that drafts are complete. See [dump provenance and reproducible measurements](docs/WIKIMEDIA_DUMPS.md).

One audited modern Russian reference can be acquired with `BOOKRAG_DATA_DIR=./data/full uv run bookrag acquire-wikibooks-reference --index`. Its fixed revision, rendered HTML, upstream credits, literal examples and table cells are retained. It is explicitly labeled partial: the captured page is complete, while overall book completion is unverified. See [reference import and fidelity checks](docs/WIKIBOOKS_IMPORT.md).
