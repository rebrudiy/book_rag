# Book Atlas interface

The UI is served by the local FastAPI app at `/`. It uses no CDN, web fonts,
client framework, paid API or build step. All book text and annotations are
inserted as DOM text nodes. External source links accept only HTTP(S).
Archived-original download links accept only same-origin `/api/artifacts/<sha256>`
locators supplied by the API.

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

When an acquired file is archived, the reader's source details offer
**Скачать зафиксированный оригинал**. The provider page and rights links remain
separate. The attachment retains its SHA-256 filename and actual format suffix.
Saved fragments use the saved snapshot's `artifact_url`, so their download stays
with the captured edition even after the same book is imported again. A missing
archive locator produces no download link. The archive fixes acquired file bytes;
it does not establish that the provider's transcription matches a printed edition.

Verify the two-edition lifecycle without changing a live library or its notes:

```sh
uv run python scripts/browser_archive.py
```

This script creates original CC0 HTML fixtures and a real isolated API on an
OS-assigned loopback port. Chromium finds a quotation, opens and saves it, then
the script replaces only its own mutable source in place and reimports it. The
saved attachment must still match the first bytes and SHA-256, while the current
reader downloads the replacement. It also checks attachment headers and format,
provider/rights separation, absent-archive behavior and a 390 px source panel.
It uses no API mocks, removes its own temporary library and server, and writes
three screenshots plus `report.json` to `artifacts/archive-browser/`.

The library distinguishes **Доступность изданий** from **Загрузка и обработка ·
последняя попытка**. Availability uses `searchable_books`/`status_counts`; the
second group uses `ingestion_status_counts`, one latest state per edition rather
than a history total. A failed refresh can therefore count as searchable and
have a failed latest attempt. Counts are global snapshots with a visible capture
time and refresh button; catalog totals follow the selected filters separately.
Availability and latest-attempt filters can be combined, for example indexed
editions whose refresh failed. A failed first acquisition stays unavailable.

Cards, search evidence and the reader use actual `ingestion_*` fields to explain
failed/deferred or ongoing refreshes. They identify the available previous
edition; quotations, metadata and downloads continue to refer to that edition.
The reader exposes the latest attempt's timestamp and a collapsible error reason.
The separate `ingestion_local_path` field is not rendered. A successful retry removes the notice.
Missing optional ingestion fields preserve the existing reader behavior.
See [the backend contract](PIPELINE.md) for transaction and migration details.

When the API supplies curated `related_editions`, the reader offers **Другие
издания и переводы**, showing each edition's language, edition label and optional
translator/partial-coverage label. Selecting a link opens that edition's first
section; chapters and quotations are not assumed to align. The bounded list is
collapsed initially on mobile and reports when additional linked editions exist.
The explicit `confirmed_full_works_lower_bound`, when supplied, is displayed as
a lower bound separately from provisional catalog work groups.

Run the real isolated availability lifecycle and optional GET-only live checks:

```sh
uv run python scripts/browser_pipeline.py \
  --live-base-url http://127.0.0.1:8000 \
  --live-base-url http://127.0.0.1:8001
```

The temporary original CC0 fixture checks failed and deferred refreshes, exact
old quotations and downloads, separate counters/filters, an unavailable first
acquisition, successful retry, mobile layout and curated translation navigation.
It uses no API mocks. The live checks compare each captured stats response with
the visible counters, follow an indexed card into its reader, check mobile width
and assert that all browser requests use GET. No live annotation or corpus writes
are made. Reports/screenshots are saved to `artifacts/pipeline-browser/`.

Saved passages with `historical_reader_url` offer **Читать сохранённое издание**
as a real link, supporting native new tabs and copied addresses. The historical
reader uses `/?saved=ID`; optional `chapter=N` selects the zero-based archived
chapter. Without a chapter parameter it opens the chapter originally saved with
that item. Its visible **Сохранённое издание · сохранено …** label identifies the
mode and save date, interpreted from SQLite UTC and displayed locally.

Historical chapter navigation requests `GET /api/saved/{id}/context?ordinal=N`.
Only the selected chapter text is retained; the remaining entries are a TOC.
Edition, source, rights, contributor, translator and original-download metadata
come from that historical response. The UI does not supplement them from the
current book or claim that the captured context is a complete critical edition.
Unicode codepoint offsets are checked before highlighting the frozen quote.
Other chapters have no quotation highlight; returning to the saved chapter
restores it. Historical passages have no current passage ID, so the reader offers
copying and does not submit them as new passage saves.

Back/forward, reload and direct links preserve the historical item/chapter.
Leaving the reader invalidates pending responses. A missing or corrupt context
clears prior text, highlighting, source links, title, contributor and TOC, then
offers a return to saved fragments. The saved excerpt and note remain available.
Legacy items without a canonical context do not acquire an invented historical
link; a reported archival failure leaves a short explanation alongside the
retained quote/note. An independently verified current-context action is labeled
**Открыть текущее издание**, separately from the saved edition.

```sh
uv run python scripts/browser_historical.py
```

The real isolated CC0 fixture saves a nonzero Unicode-offset quotation, reimports
different current text and checks the old two-chapter reader, frozen attribution,
source download, notes, two saved-chapter defaults, lazy TOC/prev/next, history,
reload, native middle-click new tab and mobile layout. It then removes only its
own current catalog entry, tests a late response with actual network latency,
and corrupts only its own archived chapter to verify the real 409 failure path.
Missing/invalid contexts and a legacy metadata fixture are also checked. No API
responses are mocked; live libraries and annotations are untouched. Screenshots
and `report.json` are written to `artifacts/historical-browser/`.

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


## Russian source journeys and explicit source constraints

`scripts/browser_russian.py` checks the actual Russian pilot through the local
browser without mocks or corpus/annotation writes. It finds each work by title
and Cyrillic author name, selects it through the bounded scope lookup, follows
an exact quotation into the reader, independently checks canonical offsets and
navigates neighboring sections. It also verifies the retained Nekrasov epigraph
in Dostoevsky, Turgenev's dedication, source rights and pinned revision links.
A 65,088-character Gogol section was checked at a 390 px viewport without
horizontal overflow or JavaScript errors.

The reader exposes `coverage_scope=original_work_only` as an explicit scope
label: original work without later editorial apparatus. This does not claim a
complete critical edition. Rights text and the rights link come from the source
metadata. A recorded source-bundle hash and a collapsible list of permanent
chapter/page revision links make the captured Wikisource version inspectable.

Current pilot links (start the full-corpus server on port 8001):

| Work | Local reader | Original source |
| --- | --- | --- |
| Записки из подполья | [wsru-underground-4d592d8e2fd1](http://127.0.0.1:8001/?book=wsru-underground-4d592d8e2fd1) | [Wikisource](https://ru.wikisource.org/wiki/%D0%97%D0%B0%D0%BF%D0%B8%D1%81%D0%BA%D0%B8_%D0%B8%D0%B7_%D0%BF%D0%BE%D0%B4%D0%BF%D0%BE%D0%BB%D1%8C%D1%8F_(%D0%94%D0%BE%D1%81%D1%82%D0%BE%D0%B5%D0%B2%D1%81%D0%BA%D0%B8%D0%B9)) |
| Отцы и дети | [wsru-fathers-and-sons-492401072288](http://127.0.0.1:8001/?book=wsru-fathers-and-sons-492401072288) | [Wikisource](https://ru.wikisource.org/wiki/%D0%9E%D1%82%D1%86%D1%8B_%D0%B8_%D0%B4%D0%B5%D1%82%D0%B8_(%D0%A2%D1%83%D1%80%D0%B3%D0%B5%D0%BD%D0%B5%D0%B2)) |
| Палата № 6 | [wsru-ward-six-740d89bd5ddc](http://127.0.0.1:8001/?book=wsru-ward-six-740d89bd5ddc) | [Wikisource](https://ru.wikisource.org/wiki/%D0%9F%D0%B0%D0%BB%D0%B0%D1%82%D0%B0_%E2%84%96_6_(%D0%A7%D0%B5%D1%85%D0%BE%D0%B2)) |
| Смерть Ивана Ильича | [wsru-ivan-ilyich-f20f7ddf97c0](http://127.0.0.1:8001/?book=wsru-ivan-ilyich-f20f7ddf97c0) | [Wikisource](https://ru.wikisource.org/wiki/%D0%A1%D0%BC%D0%B5%D1%80%D1%82%D1%8C_%D0%98%D0%B2%D0%B0%D0%BD%D0%B0_%D0%98%D0%BB%D1%8C%D0%B8%D1%87%D0%B0_(%D0%A2%D0%BE%D0%BB%D1%81%D1%82%D0%BE%D0%B9)) |
| Шинель | [wsru-overcoat-4efb1fb3e2e1](http://127.0.0.1:8001/?book=wsru-overcoat-4efb1fb3e2e1) | [Wikisource](https://ru.wikisource.org/wiki/%D0%A8%D0%B8%D0%BD%D0%B5%D0%BB%D1%8C_(%D0%93%D0%BE%D0%B3%D0%BE%D0%BB%D1%8C)/%D0%9F%D0%A1%D0%A1_1938_(%D0%A1%D0%9E)) |

```sh
uv run python scripts/browser_russian.py --base-url http://127.0.0.1:8001
```

The script writes a report and bounded viewport/element screenshots in the
ignored `artifacts/russian-browser/` directory. Source IDs include the captured
revision bundle; a later acquisition may create another edition ID.

A source-variant notice appears only when actual metadata explicitly records
`illustrations_included=false` (at the book or its source-variant level). It says:
«В этой копии нет иллюстраций; оригинал доступен по ссылке источника».
No absent-image notice is inferred from a file format, a failed download or a
missing flag. When catalog metadata carries the flag, the card also labels the
copy as **Без иллюстраций**. To check an actually recovered, indexed variant:

```sh
uv run python scripts/browser_russian.py --base-url http://127.0.0.1:8001 \
  --illustration-book pg63371-full
```

The optional check requires the real indexed edition and its explicit flag; it
does not synthesize a source or quietly mark a failed book as verified.

The real `pg63371-full` text-only recovery was subsequently checked with that
option: the indexed source metadata recorded omitted illustrations, the reader
showed the notice, and the source link remained available. The same browser run
confirmed that all five Russian editions without such a flag displayed no
illustration notice. The JSON report records these actual-source checks.
