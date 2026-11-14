# Book Atlas interface

The UI is served by the local FastAPI app at `/`. It uses no CDN, web fonts,
client framework, paid API or build step. All book text and annotations are
inserted as DOM text nodes. External source links accept only HTTP(S).

Primary workflows:

- Search exact words, hybrid retrieval or semantic retrieval. Backend warnings
  are displayed, including when an optional model is unavailable. Language and
  book filters scope the request.
- Open a hit in its chapter. Character offsets use Unicode code points to match
  Python indexing; the reader checks substring equality before highlighting.
  A mismatch produces a visible warning. Editions, rights and original source
  links are available in the reader sidebar.
- Save a verified fragment or query with a collection and personal note. Saved
  items are persisted by the API, can be filtered by collection, exported as Markdown with source provenance or JSON, revisited and deleted.
  Both exports honor the selected collection. Browser refresh preserves notes.
- Research a question using a selectable set of books. The interface describes
  its extractive evidence workflow and does not imply external generation or
  verified agreement/disagreement between authors.
- Inspect the library pipeline and filter its catalog. Demo excerpts are marked
  on book cards to distinguish an extract from a complete text.

`/` focuses search with the slash key. All navigation and actions work with a
keyboard, dialogs use native focus handling, status changes are announced, and
390 px mobile layouts support the complete search/save/read workflow.

Run browser verification against a running demo server:

```sh
uv run playwright install chromium
uv run python tests/browser_smoke.py --base-url http://127.0.0.1:8000
```

The smoke script checks exact reader text, saved collections/notes after reload,
query saving, extractive research, empty results, language filtering and mobile
overflow. It creates and removes only its own named annotation fixtures. Eight screenshots (nine with `--semantic`) are saved in the ignored `artifacts/browser/` directory.

The default retrieval mode is hybrid. Missing semantic dependencies or a missing
index are reported through the backend warning panel; the word-search baseline
remains available. To require the model-dependent Russian situation query and
verify its exact English reader citation, run the browser script with
`--semantic`.

The **Карта тем** view uses `GET /api/explore`: a language-separated TF–IDF
vocabulary map over indexed text. Select a word to find verified passages, or a
book to inspect related works and the words contributing to the relation. The
numeric score is cosine similarity, not confidence or author agreement. No
argument opposition is inferred. The view discloses limits and sampling, filters
by language, supports keyboard selection, and works on mobile.

The implementation streams passages into bounded per-book counters, retains up
to 256 terms per book, and proposes neighbors through inverted term postings.
It samples at most 2,000 passages per book and displays at most 500 books; these
limits are disclosed in the returned metadata and UI. Boilerplate patterns and
stopwords for English, Russian, French, German and Spanish are removed. Other
languages use word tokenization without a dedicated stopword list. Word forms
are not merged, and cross-language lexical links are not generated.

The homepage and library counts distinguish **indexed editions** from **work
groups**. `work_count_is_provisional` triggers an explicit explanation that a
metadata-derived group count does not establish the number of unique works.
The interface also shows curated work groups and periodical editions when those
fields are available. Results and reader chapters show a contributor separately
from the book author when `section_author` differs; editorial sections are
identified as such. The backend's final citation remains unchanged.

The server map cache holds at most four snapshots and returns independent copies.
Its key includes the resolved library database, sorted indexed book metadata,
content/index-version hashes and passage identity. A lock prevents duplicate
concurrent builds. The corpus identity is checked again after a coherent read
snapshot; ingestion during the build produces a visible limitation and prevents
caching. Saved annotations do not invalidate the map. A local measurement on
220 indexed editions took 8,581.63 ms initially and 6.87 ms with a matching cache
entry; timings exclude HTTP serialization and vary with the corpus and machine.

An optional **С переоценкой · медленнее** mode is available in the search's
additional-mode selector. It requests `mode=reranked`: a local multilingual
cross-encoder reorders a bounded set of retrieval candidates. The default stays
hybrid. Missing model dependencies, weights or inference support leave the
backend's labeled retrieval fallback and warning visible. Model scores are not
probabilities, and reranking does not rewrite quotations or attribution.

For a reproducible check of **real source attribution**, import the curated
20-edition manifest (review [source rights](SOURCES.md) first) and run a separate
full-corpus server:

```sh
BOOKRAG_DATA_DIR=./data/full uv run bookrag ingest corpus/manifest.json
BOOKRAG_DATA_DIR=./data/full uv run bookrag serve --port 8001
# In a second terminal:
uv run python tests/browser_attribution.py --base-url http://127.0.0.1:8001
```

The script searches `Republic of Plato longest` within `pg1497-full`. It checks
Benjamin Jowett's explicitly attributed editorial introduction, confirms that the
citation retains Jowett and Plato, independently checks canonical source offsets,
and follows the hit into the reader to verify the exact highlighted quotation
and contributor label. It uses real API data without mocks or corpus mutation.
Two viewport screenshots are written to `artifacts/attribution/` to avoid an
unbounded full-page capture of the complete introduction.

## Large catalogs and partial editions

The landing page requests only six indexed metadata records. It does not preload
the catalog. The library requests pages of 48 editions from `/api/books`, with
server-side Unicode title/author search, language and pipeline-status filters.
Previous/next controls and matching totals describe the actual filtered page.
The default status is indexed; selecting all statuses or a pipeline stage exposes
discovered/downloaded/failed editions without treating them as searchable.

The search scope includes a native, keyboard-accessible select and a title/author
search input. Metadata queries are debounced by 250 ms and request at most 50
indexed matches. The selected edition remains visible and pinned even when a
new query returns a different set of matches; an explicit scope notice and reset
button prevent an invisible restriction. Language choices come from corpus stats,
not the first catalog page. Map-to-book searches also pin their chosen metadata
without fetching the full text until the reader is opened.

Research-book selection has its own bounded title/author lookup. It describes
the number of displayed matches, retains selected editions separately from the
current lookup results, and permits up to ten books per research request. It
does not present the first metadata page as the complete library.

Cards and readers honor explicit `coverage` metadata: `selected_chapters` is
labeled **Выбранные главы**, while `excerpt` is labeled as a partial excerpt.
For example, the selected D2L chapters form one partially covered work; the UI
does not imply that the complete book is available.

The regular browser smoke script includes a deterministic 205-record mock
catalog to check pagination, status filters, Unicode lookup, retained scope,
research selection and partial-text labels. It does not insert these fixtures
into the real library. To additionally reach a real indexed title beyond the
first catalog page, run:

```sh
uv run python tests/browser_smoke.py --semantic \
  --full-catalog-base-url http://127.0.0.1:8001
```

The full-catalog check needs more than 48 indexed editions. It follows an actual
metadata result into the reader and locates it through the scope typeahead.
