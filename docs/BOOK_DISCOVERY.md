# Semantic discovery of books through catalog metadata

This index covers the metadata of all11,001 searchable editions. It returns book
cards with titles, authors, catalog subjects and reader links. Its explicit kind
is `metadata_semantic`. It does not return quotations, passage citations, full-text
semantic coverage, a generated answer, or newly confirmed unique-work identities.
The existing passage indexes and RAG source checks are independent.

```sh
# Uses the already cached pinned MiniLM; no new weights or model API.
.venv/bin/python scripts/build_book_discovery.py --data-dir data/full --device mps
.venv/bin/python scripts/probe_book_discovery.py --data-dir data/full
```

The builder uses an existing database through read-only SQLite without invoking
`Library` migrations. It reads `books` joined with `book_metadata.metadata` and
checks that each indexed source is a regular available file and that a reader
section exists. It never reads passage bodies, user notes or sealed evaluations.
Subject evidence is copied from the source catalog, not inferred from text.
Sources without subjects use their title and author. In the actual corpus,
10,933 of11,001 indexed editions have subjects.

## Input and vector contract

The cached model is
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, with the existing `minilm-v2`
profile. Its pooling configuration is checked for384-dimensional token-mean
pooling. Cached model files are hashed into the index contract; missing weights
cause an explicit failure instead of a download. Remote code and hub downloads
are disabled. The discovery encoder is its own instance/cache and never changes
the device of the existing passage-search encoder.

Input is labeled title, author and subjects. Reproducible caps are400 title
characters,300 author characters,32 subject strings and200 characters per
subject. Every field truncation is recorded. Tokenizer offsets produce at most
three exact metadata windows, each checked again to fit128 tokens including
special tokens. Remaining metadata, if any, is explicitly marked token-truncated.
These are metadata limits, never cuts to the original book. Normalized window
vectors are averaged and normalized again to produce one float32 vector per
edition. Query windows use the same model and normalization; an overlong query
is rejected rather than silently shortened.

The actual build encoded11,059 metadata windows for11,001 editions in31.724s on
MPS. Eleven editions had character/count field caps; none reached the three-window
limit. The matrix is11,001×384float32,16,897,664bytes including its NumPy header.
No FAISS index is needed: discovery uses exact NumPy cosine ranking. The build
process exited and released MPS. Queries use the separate lazy CPU encoder.
Build peak model memory was not recorded in this iteration; matrix bytes are not
a claim about total process memory.

## Checkpoints, activation and invalidation

Output stays under `data/full/book-discovery/`, outside Git. The builder owns a
kernel lock. Each batch flushes the matrix and atomically saves a checkpoint
binding the model/metadata contract, completed row prefix SHA256 and token-window
records. Resume verifies that prefix before continuing, so uncheckpointed rows
are overwritten and completed rows are not silently re-encoded. A changed input
gets a separate staging generation; previous results are retained.

Before activation, the complete current metadata/source guard is checked again.
Ready files include vector and record hashes and a versioned contract. A staging
generation is renamed into an immutable ready directory, then `current.json` is
replaced atomically. No book rows, content epochs, semantic receipts or existing
passage vectors are updated. Readers see a complete old or new generation.

Every search checks covered edition IDs, all book metadata/provenance, status,
reader availability and original-source file identity/size/mtime before and after
ranking. The saved artifact/canonical hashes remain part of each guard; the search
does not rehash every source book's bytes on every request. Ready vector/record
hashes and file identities are also checked. A covered update, removal, missing
source, model mismatch or corrupt generation fails closed with empty results.
A source import requires rebuilding this metadata index before that covered
edition can be searched again. Additions outside the covered set may leave the
old generation usable, with explicitly partial coverage and its current total.
Original artifact downloads still perform their own byte verification.

## Backend/API integration contract

In the running application, choose **Найти книгу по описанию** on the home page,
or open **Библиотека → По описанию и темам**. The UI displays the nearest twelve
editions, their original catalogue subjects, source-language filters and reader
buttons. It labels the evidence as metadata; it never adds a verified-quotation
badge to a book suggestion. Availability filters and catalogue pagination are
disabled in this mode. The description, language and mode survive a reload and
the return from the reader through a shareable local URL.

`GET /api/discover/books?q=...&language=en&limit=12` exposes the same service.
An empty description returns HTTP400. Missing or stale indexes return an
explicit unavailable state with ordinary title/author catalogue search still
available. User-facing warnings are translated separately from machine codes.

```python
discover_books(library, query, language=None, limit=12)
```

The result contains `kind`, `status`, `books`, `coverage`, `warnings` and a
metadata-only notice. `coverage.indexed_editions` is the usable vector coverage;
`coverage.searchable_editions` is the current indexed-edition count. A stale
index reports zero usable coverage and an explanatory warning. A language filter
restricts results without pretending to be a separate full-text language index.

Book cards keep ordinary catalog fields, reader section/passage counts, source
rights, coverage/partial labels and explicit no-illustration flags. Each adds a
cosine `score`, `reader_url`, `matched_fields` and `metadata_evidence`. The field
list identifies nonempty fields actually indexed. It is **not a causal per-field
explanation**: the score comes from combined metadata. Evidence contains original
title/author/subjects, indexed capped values and truncation/window records.
Scores are not relevance probabilities. The service returns nearest suggestions,
including for meaningless queries; no calibrated rejection threshold is claimed.
Do not pass these cards to RAG as if they were verified passage quotations.

## Open development result and limitations

`evals/book-discovery-dev-v1.json` contains25 authored open queries:20 descriptions
for the known curated editions, three cross-language descriptions of the same
known sources, and two negative controls. There are19 existing curated workgroups
because the two Darwin editions already share one manual group. No sealed cases
or new final target books were accessed. The negative controls assert no relevant
curated20 target, not absence across all11,001 complete texts.

Across23 positive queries, fixed curated-target recall is4/23 at1 and9/23 at5
and12; MRR@12 is0.270. Cold latency was5.682s; warm median was1.404s, including
both complete source/metadata guards and exact ranking. These are target-edition
retrieval measurements, not full-text relevance accuracy or human judgments.
There is no posthoc credit for another translation or semantically similar work.
For example, the German Marcus Aurelius edition, French/German Darwin editions
and German Einstein edition sometimes outrank the fixed English control.

Some misses are substantive: descriptions of a scientist creating a human,
a captain hunting a white whale and a girl entering a strange talking-animal world
did not retrieve the intended curated editions in the first12. Those scenes may
not be in the title/subjects, and metadata embeddings cannot recover missing
story evidence. Strong topic queries such as evolution, Stoic ethics and
relativity work better. Nonsense still returned nearby catalog vectors (top
cosine0.392); a modern Bluetooth-phone question returned unrelated book metadata
(top0.254). These examples prohibit interpreting any nearest neighbor as an
answer or an established identification.

A separate exact-input check recomputed all20 curated metadata vectors on CPU
and compared them to their MPS-indexed rows. Minimum cosine was1.0 within float32
rounding, with maximum component differences below1.6e-7. Thus the observed
quality limit is not explained by row alignment or a CPU/MPS model-space mixup.
This check is not a relevance benchmark. Runtime results live at
`data/evaluations/book-discovery-v1/`; the public metadata receipt is
`experiments/book-discovery-v1.json`.

Twelve isolated tests cover resumed prefixes, source/metadata/status changes
before and during search, partial coverage from additions, missing artifacts,
reader links, truthful no-image flags, unsafe/corrupt generation rejection and
explicit token/field caps. They do not substitute for the real model measurement.
Next quality work should compare metadata baselines/field weighting and audited
edition links on this open fixture, while retaining full-text search for scenes
and arguments that catalog metadata cannot describe.

Ten API integration tests use real temporary Library imports and metadata
generations with a deterministic encoder substitute. Seven additional live
browser flows used the actual 11,001-edition index: home entry, English Alice
description, Russian Darwin description, reading and returning, source-language
filter plus reload, and mobile widths390/320. Both target editions appeared at
rank3; no JavaScript errors or non-GET requests occurred. These selected examples
are usability checks, not a replacement for the open23-positive-query results.
Run `.venv/bin/python scripts/browser_book_discovery.py` against the local server;
screenshots and raw observations go to a fresh ignored artifacts directory.
