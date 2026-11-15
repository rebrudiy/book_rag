# Local answers with verifiable evidence

The **Ответ** tab explicitly retrieves book passages and asks an optional local
model to answer from those passages. Search and the extractive research board
continue to work without generation. Model acquisition is a separate explicit
step: follow [local runtime installation](LOCAL_GENERATION.md), then start with
`BOOKRAG_GENERATION_CONTEXT=8192` for a larger evidence context. No paid API or
remote inference is used.

```sh
BOOKRAG_DATA_DIR=./data/full BOOKRAG_MODEL_CACHE=./data/models \
  HF_HUB_OFFLINE=1 BOOKRAG_GENERATION_CONTEXT=8192 .venv/bin/bookrag serve
```

`POST /api/ask` accepts `query`, up to ten explicit `book_ids`, optional
`language`, `mode` (`lexical`, `semantic`, `hybrid`) and a source `limit` of1–12.
Empty book IDs are rejected rather than broadening a selected scope. Scoped
search results are merged by rank, not incomparable score magnitudes. Generated
BookRAG editorial credits are excluded; genuine introductions remain attributed
to their actual contributor.

Each claim includes `text`, `source_ids` and `supporting_quotes` from its
cited canonical sources. The model selects evidence IDs; the server inserts
their original text and exact section offsets. Ordered spans retain all
non-whitespace passage text, prefer natural boundaries and are bounded to420
characters. The full passage is also retained in the source card and reader.
This avoids a small model changing punctuation while copying a quotation.
The server validates JSON types, all citation aliases,
nonempty claims, exact supporting substrings and literal quotations in claim
text. It verifies source text, offsets, hashes, edition and attribution before
and after generation, including on timeout/failure paths. A replacement during
generation discards the answer and stale sources. Background additions alone do
not invalidate existing evidence.

Response status is one of `answered`, `abstained`, `unavailable`,
`invalid_output` or `sources_changed`. Every source contains the standard
verified passage, bibliographic citation and `reader_url`. Only canonical
quotations are marked verified. **A verified quotation does not prove that a
generated paraphrase follows from it.** Model claims can still misinterpret a
source; the original excerpt and reader are available to assess them. A second
call to the same local model checks whether claims follow from their cited
evidence and answer the question. It also sees the full cited passages for
pronoun/name context. Rejected claims suppress the answer. This is an additional
model judgement, with possible false positives/negatives; it is not independent
ground-truth validation. Source versions are checked again after this stage.

No retrieved evidence means no model call. A model abstention means insufficient
evidence in the retrieved excerpts, not proof that no answer exists anywhere in
the library. The system never fills missing evidence with an uncited answer.
Invalid citations/quotes or incomplete JSON suppress the complete generated
answer while preserving verified search results.

Retrieval spans the indexed library lexically. Semantic coverage is reported
separately; installing generation does not embed additional books. A question
over the whole library reasons over retrieved excerpts, not every page at once.
The current exact vector backend and small model do not establish reliable
exhaustive author comparison or argument/counterargument coverage.

Answer generation is bounded to512 output tokens; its grounding check has64.
Both share a45-second combined queue/load/inference budget. Retrieval precedes
that budget and may take additional time.
Oversized contexts are retried with fewer complete excerpts; source text is
never silently truncated. UI generation occurs only after explicit submission;
navigation, language changes and reload do not silently run the model. Browser
cancel discards late output; it does not cancel a native inference already
running on the server, whose deadline still applies.

## Reproduce checks

```sh
.venv/bin/pytest -q tests/test_answers.py tests/test_local_generation.py
.venv/bin/python scripts/browser_answer.py
HF_HUB_OFFLINE=1 BOOKRAG_MODEL_CACHE=./data/models \
  .venv/bin/python scripts/smoke_local_rag.py
```

Unit/browser checks use authored CC0 fixtures and an explicitly substituted
generator, exercising the real API, retrieval, citation validator and reader.
They do not measure model quality. The exploratory real-model smoke uses known
development books, keeps raw model output outside Git and never reads the
closed final fixture. Preserve failed smokes alongside later improvements.
