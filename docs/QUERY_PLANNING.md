# Selected-book query planning prototype

Status: experimental, separate from `answers.py`, API and normal Search. Nothing
here promotes a model or turns a search hypothesis into a source fact.

`book_rag.query_planning.plan_queries` accepts the original question and
server-resolved metadata for the already selected scope: `id`, `title`, `author`,
`language`. It sends only those four fields per book. Text, notes, expected facts
and evaluation anchors are excluded. A supplied generator proposes one or two
short source-language queries per book, at most ten in total. Comparison queries
must target each book's requested axis. There are no question/title/fact-specific
routes or stored translations.

The result includes:

```python
{
    "kind": "retrieval_hypotheses",
    "original_question": original_question,  # retained verbatim
    "used": True,
    "book_queries": [
        {"book_id": selected_id, "language": source_language, "queries": [query]},
    ],
    "response_language_hint": "ru",  # question language, separate from scope
    "fallback": None,
    "generation": {"model": {...}, "usage": {...}, "elapsed_ms": ...},
    "reason": "validated_plan",
    "elapsed_ms": ...,
}
```

The caller owns scope and limits. Output must cover every selected ID exactly
once, and cannot add IDs, change language metadata, set retrieval parameters,
return answer claims or increase the query budget. Each query is at most 160
characters and 24 words, without quotes, FTS operators, control tokens or newlines.
Duplicate JSON keys and duplicate per-book queries are rejected. Language tags
are syntactically checked; correct translation and topic preservation still need
quality assessment. The model's reply-language hint is advisory. An explicit
caller hint must be preserved and is independent of source-language filtering.

Bad/truncated/late/foreign-scope output returns `used=False`, no additional
queries and `fallback="original_retrieval"`. No model is acquired implicitly.
Empty global scope and unavailable generators also retain the existing search.
Invalid caller input raises an explicit validation error. Catch it at an optional
integration boundary, rather than changing the selected scope.

`PlanningLimits()` requests 8 seconds and at most 384 output tokens, temperature
zero. `max_queries_per_book=1` restricts the schema and post-validation to one
query per selected book, without changing the original question or scope. The existing `LocalGenerator` enforces its finite deadline, including queue,
file verification and cold loading; termination cleanup can add wall-clock time.
A returned late callback result is also discarded. Arbitrary generator adapters
must themselves enforce their runtime deadline; this helper does not preempt an
uncooperative Python callback. At an Ask integration boundary, debit planning
from the existing overall request deadline, skip it when less than a valid
minimum budget remains, and retain the original retrieval on queue timeout.
Do not start planning implicitly from filters, navigation or ordinary Search.

## Read-only probe

Run from the repository root as a module. The three cases below are previously
inspected **open development** failures. No final/sealed cases are used.

```sh
BOOKRAG_GENERATION_PROFILE=qwen3-4b-instruct-2507 HF_HUB_OFFLINE=1 \
  .venv/bin/python -m scripts.probe_query_planning \
  --real-planner --source data/full \
  --case ru-query-smith-dinner --case compare-property --case ru-land-grave \
  --report data/bench/query-planning-new-run.json
```

The profile must already have its pinned GGUF locally. The probe performs at most
three generation calls; it downloads nothing and performs no embedding, answer
or entailment generation. It uses the existing read-only adapter, never
`Library(original)` construction/migration. Selected-book guards bind metadata,
raw/canonical hashes, section text hashes, passage identities and contributor
roles before/after, and match the public fixture's editions. Global ingestion can
continue. Reports contain source IDs, citations and text hashes rather than full
corpus text. Existing report paths cannot be overwritten.

Control and planned searches both use current **lexical** retrieval, 40 results
per scoped query. The control removes natural-question quote delimiters as Ask
does. This is a lexical ablation, not a paired full-hybrid RAG run. Per-query,
planned-query round robin and planned-plus-original round robin are reported;
merge policy is diagnostic and has not been integrated. An anchor missing from
an excerpt is not proof of its absence from a work.

## First actual result: budget and morphology blockers

[query-planning-v1.json](../experiments/query-planning-v1.json) records exactly
three calls to the reviewed Unsloth Qwen3-4B-Instruct-2507 Q4_K_M conversion,
revision `a06e946bb6b655725eafa393f4a9745d460374c9`. All selected-source guards
passed. This is not an official Qwen GGUF, and its conversion base revision remains
unknown; see the existing generation-profile provenance.

| Open case | Planner wall time | Result |
| --- | ---: | --- |
| Russian question to English Smith | 8.004 s | Deadline; original retrieval retained |
| Smith/Marx property comparison | 8.587 s | Deadline including worker cleanup; original retained |
| Russian grave question | 5.339 s | Valid two-query Russian plan; normal lexical queries miss anchor |

The successful plan used 270 prompt/69 completion tokens. The two timeouts kill
their workers, so the next attempt must cold-load again. These three calls do not
establish warm latency. At two passages per book, original and planned lexical
channels both hit **0/4** reference anchors. Original lexical retrieval contains
one anchor at rank 14 within 40; planned normal lexical retrieval contains none.
There is no demonstrated overall retrieval gain from this first planner run.

A separate CPU-only replay tests the existing bounded Snowball stem candidate
and body-coverage reranking helpers **as an explicit experimental planned-query
channel**, including when normal lexical results are nonempty. It does not change
the production empty-only Russian fallback or strict quoted Search behaviour.

```sh
.venv/bin/python -m scripts.probe_query_planning \
  --replay-report experiments/query-planning-v1.json --source data/full \
  --case ru-land-grave --report data/bench/query-planning-stems-new-run.json
```

[query-planning-ru-stems-v1.json](../experiments/query-planning-ru-stems-v1.json)
revalidates the saved plan and the identical selected edition, makes **zero model
calls**, and records the two generated Russian queries. The stem channel finds
the ending anchor at ranks **1** and **2**, respectively, in 24.85/5.40 ms in this
single sequential CPU observation. Normal planned lexical retrieval still misses
it; the report's main summary refers to that normal channel, while stem results
are explicitly nested under `experimental_russian_stems`. This known-example
success is not general relevance evidence. Short Russian stems can remain
ambiguous, and source-language morphological quality needs independent tests.

## Integration decision and next experiment

The structurally guarded planner is usable as an optional retrieval-only
component, with an unchanged original-query fallback. Its 8-second cold budget
has not succeeded for the two English/decomposition cases; do not claim those
gaps fixed. Next, measure a shorter one-query-per-book plan with a shared warm
runtime or a separately declared 12–15 second budget, retaining the first
negative receipt. Any extra actual model calls require coordination with other
GPU workers.

Before integrating into Ask, separately evaluate the source-language queries,
explicit planned-Russian morphology, per-book comparison coverage, original
query preservation and final bounded source merge. Exact verified excerpts must
be rechecked as in the existing answer path. Planning text must never enter the
answer's evidence list or supply missing facts. A successful hypothesis does not
weaken the independent answer/directness and canonical quote checks.

Validation: `tests/test_query_planning.py` covers scope injection, malformed and
duplicate JSON, caller controls, schema grammar bounds, timeout/truncation,
question/source language separation, unchanged original retrieval, read-only CC0
probe guards, reference exclusion, replay validation and corrupt quote suppression.

## Second actual run: one query per book

[query-planning-v2.json](../experiments/query-planning-v2.json) preserves the next
three authorised calls with a 20-second budget and `max_queries_per_book=1`.
The same owned worker is reused until the runner closes it; it was coordinated
between other model jobs. No expected facts or reference quotations entered the
prompt. All selected-source guards passed.

| Open case | Planner wall time | Actual retrieval result |
| --- | ---: | --- |
| Russian question to English Smith | 4.555 s | Finished, but plan failed validation; original retained |
| Smith/Marx comparison | 3.806 s | Valid English per-book queries; reference anchors rank 8/rank 2 |
| Russian grave question | 3.492 s | Valid Russian query; ending anchor rank 1, plain and stem channels |

For these four reference anchors, normal planned lexical retrieval changes hits
at 2/book from **0/4 to 2/4**, and candidate-pool coverage within 40/book from **1/4
to 3/4**. This is an actual retrieval gain on inspected development failures, not
an answer-quality or generalisation claim. The Smith own-labour passage remains
at rank 8 and therefore outside the current two-passages-per-book Ask selection.
The cross-language dinner case remains unresolved: the invalid output was not
released or retained as raw text, so its exact rejected field cannot be diagnosed
from this receipt. It must not be credited as successful query translation.

The successful Russian plan infers reply-language hint `ru`, independently of
its source-language field; the English comparison infers `en`. A successful real
Russian-question/English-source query plus Russian reply hint has **not** yet
been demonstrated. Fake/CC0 tests exercise that contract but are not model
quality evidence.

Reproduce only with a newly authorised model slot and new output path:

```sh
BOOKRAG_GENERATION_PROFILE=qwen3-4b-instruct-2507 HF_HUB_OFFLINE=1 \
  .venv/bin/python -m scripts.probe_query_planning \
  --real-planner --timeout 20 --queries-per-book 1 --source data/full \
  --case ru-query-smith-dinner --case compare-property --case ru-land-grave \
  --report data/bench/query-planning-one-query-new-run.json
```

## Optional canonical candidate helper

`retrieve_plan_candidates(library, question, books, plan, original_searches,
language=None, limit=40)` is ready for separate root integration. `original_searches`
is a mapping from the exact selected IDs to existing search responses. The helper:

- Revalidates question, IDs, source languages, plain queries and total bounds.
- Preserves the original response mapping and does not mutate its results.
- Runs scoped lexical queries and, for validated Russian hypotheses, a separate
  bounded Snowball stem/body-overlap channel even when normal Search is nonempty.
- Applies the caller's source-language filter to both channels.
- Retains strict source ownership, hashes, original text and attribution through
  existing canonical result validation. Corrupt candidates are suppressed.
- Returns individual `planned_searches` and bounded `candidates_by_book`, with
  planned channels and original results interleaved by rank; scores are not added.
- Retains only original candidates on invalid plans or optional-search failure,
  including failure after another book's planned search already succeeded.

The full original response mapping remains available even if a bounded merged
candidate list cannot hold every original result. The helper does not generate
claims, select final answer sources, create public source aliases or modify
normal Search. The caller must apply its existing editorial filtering, recheck
sources before/after generation, enforce the remaining request deadline, and
measure its final source-selection policy independently.

[query-planning-v2-replay.json](../experiments/query-planning-v2-replay.json)
replays the saved plans with **zero model calls**, checks the identical selected
editions, and exercises this helper. Original results are unchanged for all
three cases. Helper anchors are Smith rank 8, Marx rank 2 and grave rank 1, with
**2/4 at 2/book and 3/4 within 40/book**; the rejected dinner plan keeps original
retrieval. Observed helper times are 101.37 ms for the comparison and 16.01 ms for
the grave case, with warm-cache/sequential-query confounds. No production API,
answer path, corpus or model default was changed.

```sh
.venv/bin/python -m scripts.probe_query_planning \
  --replay-report experiments/query-planning-v2.json --timeout 20 \
  --queries-per-book 1 --source data/full \
  --case ru-query-smith-dinner --case compare-property --case ru-land-grave \
  --report data/bench/query-planning-helper-new-run.json
```

## Third development iteration: observed rejection and opt-in body reranking

`experiments/query-planning-v3.json` records the next bounded experiment. Normal
`Library.search`, Ask/API code and the original-query control are unchanged.
The candidate helper still defaults to its previous round-robin order. Root can
explicitly request the new policy:

```python
retrieve_plan_candidates(
    library,
    question,
    books,
    plan,
    original_searches,
    language=language,
    limit=40,
    rerank_candidates=True,
)
```

The return adds `ranking_strategy` and per-book `rerank_by_book` diagnostics.
The pure `rerank_planned_candidates(question, queries, candidates, language=...,
limit=...)` accepts already canonical/scoped candidates. It reads only passage
bodies, original question and validated hypotheses. There are no book-ID,
author-title, reference-quote, expected-answer or anchor rules.

The heuristic combines source-language Snowball term coverage, candidate-pool
IDF, original-question coverage, query bigram overlap and the shortest window
covering matched query terms. Bigram evidence requires another topic term nearby
in the same sentence; adjacency is never manufactured across a paragraph or
sentence ending. This separates a locally connected topic from words scattered
through an unrelated passage. Scores and source text remain unchanged; added
`planned_lexical_rerank` fields explain the ordering. Candidate-observed coverage
is explicitly distinguished from full planned-term coverage, with absent query
topics listed separately. Neither coverage metric proves semantic support.

An unconditional first replay improved the property comparison but regressed
the new Russian-to-English dinner query from rank1 to4. That failed report is
retained. The conservative policy therefore applies original-question lexical
reranking only when the validated reply-language hint matches the source language.
Cross-language or unknown hints keep the existing planned round-robin order.
The hint is advisory language metadata, not a translation proof; an independent
caller hint should be supplied where available. This language guard is a
measured development choice, not a generalisation claim. It introduces no
book-specific routing and never changes source-language filters.

### What was actually wrong with the dinner plan

The historical v2 rejected raw output was not saved, so its exact bad field remains
unknown. A fresh run with the same earlier prompt/schema reproduced an invalid
plan in4.652s. Its response-language value was valid `ru`; the generated query
contained a closing typographic double quote U+201D and a comma **inside** the
JSON query string. The plain-query validator correctly rejected it and retained
original retrieval. It was not a demonstrated translation success.

The generic query-string JSON-schema pattern now excludes straight/typographic
quotes, controls and the already forbidden query syntax. It passes the existing
native grammar budget; the160-character/24-word limits remain independently
post-validated. The prompt also asks for distinctive actors/objects/numbers from
the question instead of vague abstractions. No rejected text is silently repaired.
A new actual20-second-budget call produced a valid English dinner hypothesis
with reply hint `ru` in5.693s and retrieved the reference passage at rank1.
Because both grammar and the generic prompt changed, their separate effects on
query wording were not isolated.

`python -m scripts.diagnose_query_planning --report data/bench/<new>.json` is an
explicit research-only, one-call runner. It receives only the original public
question plus four metadata fields, retains bounded rejected output in a new
ignored `.raw.json` file and closes the worker. Raw output and validation detail
are not added to `plan_queries` results, API responses or UX. It refuses an
existing report path or a path inside the original library. One initial `<stdin>`
startup attempt failed before model inference because spawn requires a real main
module; that receipt is also retained. There were exactly three subsequent actual
4B completions, no answer or entailment calls and no model downloads.

### Retrieval measurements and remaining negative

Saved v2 comparison/grave plans were replayed without model calls to isolate
candidate ordering. The fresh valid dinner plan was replayed separately. All
selected-source guards passed and every original response mapping was unchanged.

| Open reference | Prior helper rank | Guarded new helper rank |
| --- | ---: | ---: |
| Smith, own labour | 8 | 1 |
| Marx/Engels, bourgeois property distinction | 2 | 1 |
| Tolstoy, grave ending | 1 | 2 |
| Smith, dinner | No valid historical plan; fresh planned query rank1 | 1, cross-language ordering retained |

The fixed old-v2 replay improves two-per-book reference coverage from2/4 to3/4;
its rejected dinner plan still retains original retrieval. Combining that replay
with the separately generated new dinner plan yields4/4 reference anchors within
two/book on these three already inspected open cases. This is a mixed replay
retrieval observation, not a fresh paired end-to-end answer-quality test or a
heldout result. The grave rank worsening and the earlier unconditional dinner
regression are explicitly retained rather than omitted.

The third actual4B call,4.164s, used the wrong-topic Alice question. It produced a
valid query that echoed book-title words and still retrieved40 irrelevant lexical
candidates. The body reranker does not establish answerability or whole-book
absence. All results remain hypotheses with `semantic_support_verified=false`.
Callers must retain their source/editorial filters, canonical quote verification,
question-axis checks and ability to abstain. No production integration is approved
by these measurements alone.

Reproduce the rerank ablation with zero model calls and a new report path:

```sh
.venv/bin/python -m scripts.probe_query_planning \
  --source data/full --replay-report experiments/query-planning-v2.json \
  --timeout 20 --queries-per-book 1 --rerank-planned \
  --case ru-query-smith-dinner --case compare-property --case ru-land-grave \
  --report data/bench/query-planning-new-replay.json
```

Tests now include private rejection capture, the one-call diagnostic budget,
plain-string grammar constraints, same-sentence proximity, metadata-only false
hits, unchanged source scores/text, opt-in/language gating, full-term missing-topic
diagnostics and fail-closed optional rerank failures. The four tests importing
repository scripts use a scoped repository-root fixture so the standalone
`pytest` executable works without relying on `python -m pytest` path behaviour.
No global path alteration or production import fix was introduced.

### Final helper contract for explicit Ask integration

Use server-resolved selected-book metadata. Keep the exact original question and
the original scoped response mapping; do not replace them with a generated query.
A successful helper returns:

```python
{
    "used": True,
    "reason": "validated_plan_candidates",
    "original_searches": original_searches,  # same mapping, unchanged
    "planned_searches": [
        {
            "book_id": selected_id,
            "query": hypothesis,
            "method": "planned-lexical",  # or planned-russian-stems
            "results": canonical_rows,
            "warnings": [...],
        },
    ],
    "candidates_by_book": {selected_id: ordered_canonical_rows},
    "ranking_strategy": "body-coverage-proximity-v1",  # if explicitly requested
    "rerank_by_book": {
        selected_id: {"applied": True, "reason": "same_language_original_question"},
    },
    "warnings": [...],
    "elapsed_ms": ...,
}
```

Every selected ID is represented in `candidates_by_book`. Initial per-book
interleaving is Russian-stem, planned-lexical and original channels by rank,
deduplicated by passage ID and capped at `limit`. With two planned hypotheses,
each contributes its own channels. The source row retains its original score;
**consume the returned list order before the final two-per-book cap** instead of
resorting by those heterogeneous channel scores. The complete original response
mapping remains available even when the bounded candidate pool cannot include
every original row.

An explicitly enabled rerank runs only for matching question-hint/source language.
Skipped books retain that initial channel order and explain
`cross_language_or_unknown_hint_original_order`. `rerank_candidates=False` remains
the default and records `original-round-robin`. No selected ID, source-language
filter, response-language hint or search limit is inferred from the model beyond
the already validated plan. An explicit source-language filter also suppresses
unfiltered originals supplied by a caller; `original_searches` itself stays
unchanged for auditing.

If the plan is unused, foreign, malformed or optional searching/reranking fails,
`used=False`, `planned_searches=[]` and `candidates_by_book` retains only the
eligible original candidates. Reasons are `original_retrieval`,
`invalid_plan_original_retained` or `planned_search_failed_original_retained`.
Optional failure after one book already succeeded discards all planned additions.
Success-only diagnostics may be absent on fallback, so callers should use `.get`.
Private exception messages and rejected model output are not exposed.

Before generation, apply the existing editorial-packaging exclusion and canonical
source rechecks. Generate answers from those canonical excerpts and the unchanged
original question only. Do not add planner hypotheses or rerank explanations to
the evidence list. Debit planner/helper time from the caller's overall deadline;
keep the existing source revalidation, final quote checks and abstention path.
The known wrong-topic result still demonstrates why retrieval hypotheses cannot
serve as an answerability decision.
