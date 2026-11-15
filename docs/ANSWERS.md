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
`language`, `mode` (`lexical`, `semantic`, `hybrid`), a source `limit` of1–12,
and strict boolean `plan_retrieval` (default `false`). Strings such as `"true"`
and numeric flags are rejected.
Empty book IDs are rejected rather than broadening a selected scope. Scoped
search results are merged by rank, not incomparable score magnitudes. Generated
BookRAG editorial credits are excluded; genuine introductions remain attributed
to their actual contributor.

A question is interpreted as natural language: quotation marks around a title
or paraphrase do not impose an exact-phrase retrieval constraint. Its original
wording still reaches generation and the evidence check. The separate search
API retains exact quoted-phrase semantics. A compact limit of two passages per
book reduces distraction for the baseline local model. Empty scoped Russian
lexical results can use the bounded [word-form fallback](RUSSIAN_RETRIEVAL.md).

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

An open development experiment that retained individually accepted claims
produced more answers but also released wrong actors, off-topic comparisons and
misleading numerical qualifications. That policy was rejected: a mixed grounding
verdict suppresses the complete answer. The failed experiment and reproduction
patch are retained in `experiments/rag-partial-v2.json` and `.patch`; independent
AI-agent review is recorded separately from human evaluation.

In the default mode, no retrieved evidence means no model call. An explicitly
requested eligible planner may run before evidence is found, as described below;
answer generation still requires canonical sources. A model abstention means insufficient
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
Both share a45-second combined queue/load/inference budget. With retrieval
planning enabled, the planner and its additional retrieval also consume that
same budget, beginning before planning. Original retrieval precedes the budget
and may take additional time.
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

## Optional selected-book retrieval planning

`plan_retrieval: true` explicitly enables a separate retrieval-hypothesis stage
for existing selected books. The server resolves their IDs, titles, authors and
languages itself. A global request, an unknown ID, an unavailable selected book
or an invalid mixed scope retains original retrieval without invoking a planner.
No book is silently dropped or added to make the scope valid.

```json
{
  "query": "What does this author say about the requested topic?",
  "book_ids": ["an-existing-selected-book-id"],
  "mode": "hybrid",
  "plan_retrieval": true
}
```

The planner receives only the unchanged question and those four catalog fields,
never book passages, notes, reference answers or expected facts. It uses the same
configured local generator as the writer and judge, permits one short query per
book, at most384 output tokens and20 seconds. It does not acquire weights, invoke
a paid service or switch models. All stages share the existing45-second generation
deadline; the answer stage does not start a new45-second budget after planning.
A valid cross-language plan keeps source-language filtering independent from its
validated reply-language hint. Only that hint is added to the writer payload.

Original searches and their response objects are retained. Validated hypotheses
add bounded scoped lexical/Russian-word-form candidates through
[the optional helper](QUERY_PLANNING.md). Its ordered per-book pools feed the
existing final round robin **before** the two-passages-per-book cap. Same-language
body coverage/proximity reranking is enabled; cross-language hypotheses keep the
conservative channel order. Old heterogeneous search scores are not added or
resorted. Invalid, late, foreign-scope or failed optional planning/search/reranking
retains original candidates. A selected source-version change during planning
also falls back; stale original rows still fail ordinary canonical hydration.
The opt-in path enforces the requested book/language scope during hydration and
again after generation.

A planner may therefore run on an explicitly selected indexed book even when
original retrieval is empty. If it still finds no verified sources, Ask abstains
without writer or grounding-check calls. The default empty-evidence path still
makes zero model calls. This distinction is intentional and opt-in.

The response exposes only bounded retrieval diagnostics:

```python
{
    "retrieval": {
        "planning": {
            "requested": True,
            "used": True,
            "reason": "validated_plan_candidates",
            "elapsed_ms": 1200.0,
            "ranking_strategy": "body-coverage-proximity-v1",
            "book_queries": [{"book_id": selected_id, "language": "en", "queries": [hypothesis]}],
            "response_language_hint": "ru",
        }
    }
}
```

`used` describes retrieval hypotheses, not an answerability or correctness
certificate. Rejected raw model output, native exceptions and private diagnostics
are excluded. No hypotheses or rerank explanations are inserted into the
writer's source evidence. Invalid/unused planning has an empty query list and
keeps a bounded reason code.

Only the opt-in writer and grounding-check prompts add explicit reminders to
preserve actors, recipients, negation, units, quantified bounds, and observation
versus instruction or possibility versus fact. They also reject conclusions of
whole-book absence from missing retrieved evidence. Default prompts and writer
payloads are retained for controlled comparisons. These instructions remain
model guidance, with the previously documented false-positive/false-negative
limitations; exact quote and source-version checks remain mandatory.

Twenty-eight new temporary-CC0 integration tests cover strict API booleans,
metadata-only planner inputs, reply/source language separation, source recovery,
ordered candidates before the cap, untouched originals, opt-in failures, unknown
or global scopes, no default planner calls, missing evidence, shared deadlines
and source/language changes. The166-test targeted suite passed; real-model quality evaluation is separate.
No live model, corpus or server was changed by these tests.
