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
