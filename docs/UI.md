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
  items are persisted by the API, can be filtered by collection, exported as
  JSON, revisited and deleted. Browser refresh preserves notes.
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
overflow. It creates and removes only its own named annotation fixtures. Six
screenshots are saved in the ignored `artifacts/browser/` directory.
