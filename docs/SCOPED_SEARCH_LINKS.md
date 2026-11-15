# Scoped search links and restoration

Fragment-search links carry the complete requested scope:

```text
/?q=clef&mode=semantic&scope=BOOK_ID&language=fr
```

`q` is the original search text, `mode` is the selected retrieval mode, `scope`
is an edition ID and `language` filters source text. These values survive reload,
opening a result in the reader, the reader's return button and browser history.
An edition ID must be resolved to an actually indexed edition before a scoped
search is submitted. A missing or unavailable edition remains visible as an
unconfirmed restriction; it must not silently turn into a whole-library query.

Changing route, mode, language or selected book cancels the older restoration.
A delayed successful book lookup cannot overwrite newer filters, and a delayed
404 cannot clear a newly selected valid book. Removing a restriction or clearing
filters is an explicit user action; changing a language alone does not authorize
discarding an unconfirmed book restriction.

Answer links retain their separate behavior: `panel=answer` restores the question
and selected scope without sending `/api/ask`. The optional planning checkbox
requires a confirmed book, is not serialized in the URL and defaults to unchecked
after a fresh page load. Restoring a link or toggling that checkbox does not
generate an answer.

After a nonempty fragment search finishes, the interface scrolls to the results
column so the mobile hero and filters do not hide the first result. Returning
from the reader to an unchanged completed fragment search restores that position. Aborted or
stale requests, edited questions and the Answer panel do not trigger that scroll.

## Isolated browser proof

Run:

```sh
.venv/bin/python scripts/browser_scoped_search_links.py \
  --artifacts artifacts/scoped-search-links
```

The script creates a temporary owned French/Russian/English CC0 library and a
small real float-vector index. A fake encoder substitutes model inference; no
Torch, GPU, external model, acquisition or paid API is used. It runs the actual
FastAPI application and Chromium. A fixture middleware delays selected real
`GET /api/books/ID` handlers before they return their actual200/404 responses.
It does not mock successful or failed HTTP response bodies.

Browser requests are GET-only, with an additional transport guard rejecting any
unexpected write. The script never submits an answer, saves a note or changes a
live library. It records actual `/api/search` query parameters and checks:

- direct scoped semantic links, reload and subsequent mode changes;
- exact source highlights and both reader/browser return paths;
- missing and discovered-only editions blocking search, including Enter;
- delayed200 after navigation, followed by a valid history retry;
- delayed200 after a language change preserving the blocked book restriction;
- delayed404 after a different explicit book choice preserving that newer scope;
- Answer scope/checkbox restoration without automatic generation;
- mobile scope restoration, exact reader text, results visible after search/return
  and horizontal overflow.

The report and screenshots are written under the ignored artifact directory.
The report distinguishes fake encoding from real retrieval/source verification;
it is an interface/source-safety check, not a semantic-quality measurement. Live
Kant or other corpus checks are separate integration probes and do not run from
this isolated script.
