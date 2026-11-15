# Open RAG development fixture

`evals/rag-dev-v1.json` is an **open development set**, never a closed or final
benchmark. It contains 14 questions over ten editions selected from the curated
20-edition pool and three newly audited Russian originals. Sixteen short support
anchors were copied from exact canonical sections and their surrounding
paragraphs were manually read. No answer was generated to construct the labels.

The source snapshots retain title, author, edition, source/rights URLs, original
artifact SHA and canonical book SHA. Each anchor additionally pins section
ordinal/title/SHA, exact Unicode offsets, quote SHA and section contributor
metadata. Quotations retain their source rights: Gutenberg's public-domain
statement is US-specific; Wikisource transcription credit and CC BY-SA 4.0
remain in the Russian source records. The evaluator's MIT license does not
relicense book text.

| Probe | Expected behavior |
| --- | --- |
| Rabbit's watch; Queen/Alice in court; Austen's opening | Answer concrete book facts with exact support |
| Smith's pin factory; Darwin's broader struggle | Explain the fact supported by the selected passage |
| Republic introduction | Identify the Laws and credit Benjamin Jowett's editorial introduction, not Plato |
| Russian question about Smith's butcher/brewer/baker | Answer in Russian while quoting the English source literally |
| Smith vs Marx/Engels on property | Cite both books and preserve the stated distinction |
| Origin 1859 vs sixth-edition conclusion | Cite both editions of one work; compare the quoted wording |
| Mass–energy formula scoped to Alice; Python SHA-256 scoped to Smith | Abstain on the selected-book evidence |
| Chekhov's dog owner; Lefty's warning; Tolstoy's grave | Answer three specific Russian-original facts |

The reference facts explicitly avoid several overclaims: Austen's opening
proposition is not asserted as a universal real-world truth; Darwin's concluding
phrase is not proof of absence of a word throughout a book; Lefty's warning is a
character's statement, not independently verified technical advice. The public
fixture records these cautions for manual review.

## Source validation without any model

```bash
.venv/bin/python scripts/evaluate_rag_answers.py --validate-only \
  --output data/evaluations/rag-dev-v1/validation.json
```

The default is also validation only. It opens SQLite with `mode=ro`; no
`Library` constructor, import, saved-item write or model is invoked. It checks
all ten indexed source snapshots and all sixteen exact anchors. A missing book,
changed artifact/canonical text, shifted section or contributor mismatch stops
scoring and prevents API collection. Do not refresh a source simply to make a
mismatch disappear: inspect it and explicitly re-audit/version the development
fixture. The live catalog may grow while these selected source versions stay
unchanged. No read transaction is held during generation, so collection does
not pin the live WAL for the entire run.

The runtime archive is ignored by Git. Gutenberg current download URLs can
change; a future download is not assumed to reproduce the pinned SHA. Obtain
the curated source pool through [SOURCES.md](SOURCES.md) and the Russian source
recipe through [RUSSIAN_SHORT_WORKS.md](RUSSIAN_SHORT_WORKS.md), then verify the
actual snapshot. Parser/version changes can require a newly reviewed fixture.

## Collect later, explicitly and locally

Run this only when the local generator is available for the evaluation; it was
**not run during preparation of this fixture**:

```bash
.venv/bin/python scripts/evaluate_rag_answers.py \
  --collect-url http://127.0.0.1:8001 --allow-generation \
  --mode hybrid --limit 8 \
  --transcript data/evaluations/rag-dev-v1/run-01.responses.json \
  --output data/evaluations/rag-dev-v1/run-01.report.json
```

The collector sends each exact question and explicit book scope to public
`POST /api/ask`, sequentially. It uses the public claims/source/supporting-quotes
response only, without depending on the generator's internal schema. Question
language is not incorrectly used as a source-language filter for RU→EN probes.
Only an explicitly chosen local origin is allowed; redirects are not followed.
Each response is limited to 2 MiB. There are no automatic retries. HTTP, HTML,
malformed or oversized responses are recorded as operational failures, not
correct abstentions.

Responses and capture timestamps/hashes are saved atomically after every case.
An existing transcript requires `--resume` and a matching fixture SHA/request
configuration. Resume only the same server/model/prompt setup. Use a new run
path after a model, prompt or retrieval change. Generation metadata is retained
from each response; the runner does not claim it attests which code the server
has loaded. A partial transcript is reported as partial, with unrun cases.

Replay without generation or API calls:

```bash
.venv/bin/python scripts/evaluate_rag_answers.py \
  --responses data/evaluations/rag-dev-v1/run-01.responses.json \
  --output data/evaluations/rag-dev-v1/run-01.replay.json
```

## What the report establishes

Automatic checks independently look up every returned passage in SQLite and
verify section ownership, bounds, text slice and SHA; returned `verified=true`
is insufficient. Bibliographic/contributor/translator fields, reader identity,
claim source IDs, exact supporting quotations and explicit literal claim quotes
are checked. Both required books must be cited for comparison probes. Reference
anchor presence in retrieved/cited passages is a retrieval diagnostic.

**These checks do not prove semantic entailment or factual correctness.** A
paraphrase can be false while its attached quotation is exact. Every case keeps
`factual_correctness=unreviewed`, `semantic_entailment=unreviewed` and
`answer_language_appropriateness=unreviewed`, with the expected facts, cautions
and observed claims ready for a human. The runner never turns keyword overlap
or anchor presence into factual accuracy.

The observed answerability metric scores only usable `answered`/`abstained`
responses with valid response structure. `unavailable`, `invalid_output`,
`sources_changed`, transport failures and unrun cases are displayed separately;
they do not earn abstention credit. Source/claim integrity counts have their own
denominators. There is no combined score that can conceal these differences.

Preparation validation: all 10 source snapshots and 16 anchors passed; all 14
model cases remain `not_run`, with zero scored answers. Twenty-three offline
tests and Ruff/ty passed. Tests use actual temporary CC0 canonical books plus
mocked HTTP transport where needed; they never run the real model or claim its
accuracy. They include false paraphrases with valid quotes to preserve the
explicit human-review boundary.
