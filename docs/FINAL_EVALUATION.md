# Sealed final evaluation

`evals/final-large-v1.seal.json` is an **aggregate-only commitment**, not a query fixture
and not a quality report. The real fixture remains in ignored, owner-read-only runtime
files under `data/sealed-evals/final-large-v1`. No real query, anchor, source-book identity,
title, or raw source text is included in the public seal, validator, or synthetic tests.

At sealing, the developer received counts, SHA-256 commitments and limitations only.
No retrieval evaluation or model call was run on this set. The published zero-run count
describes that sealing stage; authorized later reports must not rewrite this commitment.
This is procedural blinding within an agent team, not encrypted storage or an independent
human evaluation organization.

The sealed set contains 40 cases: 34 positives and six explicit wrong-book negatives.
Positive targets span 28 source books outside the prior 20 required targets and excluded
known-probe works. Query languages are English 12, Russian 12, German 9 and French 7.
It includes six exact-quote, six scene, six idea, six explanation, four book-description,
six cross-language and six unanswerable cases. Cross-language cases cover six directions.
Documentary situations are included among the scene cases. The fixed grounding source is
the `library1308-v1` snapshot; these counts do not establish quality over an entire larger
catalog or over books outside semantic coverage.

The public seal commits to the private labels, private source receipts, private seal,
grounding SQLite bytes, old-fixture hashes and aggregate composition. Committing this
file to Git makes the commitment independently inspectable before any final result.
The private seal also retains its original validator, synthetic tests and validation
report. Publishing a reusable validator does not alter those frozen private files.

## Integrity validation before candidate freeze

This command checks grounding and commitments only. It makes no search/model/API calls,
prints no cases, and writes no report or source files:

```sh
.venv/bin/python scripts/validate_final_fixture.py \
  --source data/snapshots/library1308-v1/library.sqlite3 \
  --known evals/dev.json evals/dev-expanded.json evals/heldout.json \
  --public-seal evals/final-large-v1.seal.json
.venv/bin/python -m pytest -q tests/test_final_fixture_seal.py
```

The validator opens the source using SQLite `mode=ro&immutable=1` and `query_only=ON`.
It rejects nonempty WAL, compares main/WAL/SHM hashes before and after, checks exact
Unicode code-point spans against canonical chapters and indexed passages, independently
checks hashes and attribution, and rejects known query/target/anchor overlap. Negative
scope and distinguishing-term absence are checked against every chapter of the selected
wrong book. Source initialization, migrations and checkpointing are never performed.

Source metadata, canonical evidence and private files are read internally for these
integrity checks; their contents are not exposed to the developing agent. The CLI emits
aggregates and hashes only. Invalid or malformed inputs produce an error code without a
private query, book identity, source excerpt or traceback. Synthetic tests use invented
CC0 text and owned tiny SQLite files, never the real labels or source corpus.

Thirty-one tests cover non-NFC/CRLF preservation, wrong coordinates and attribution,
actual-text drift despite a matching claimed hash, wrong negative scope, omitted later
chapters, pending WAL rejection, existing SHM preservation, changes during validation,
old-fixture overlap, modified payloads, redirected paths, altered public commitments,
private resealing attempts, aggregate mismatches, and CLI disclosure prevention.

## Explicit candidate freeze

Before any case inspection or retrieval run, record an explicit freeze in a separate
candidate receipt. Record:

- Git revision and dirty-tree status, retrieval modes and output `k`.
- Dataset, private seal and public seal SHA-256 values.
- Grounding source SHA-256 plus the actual evaluation corpus identity and counts.
- Active-generation metadata, installation/coverage receipts and vector identity.
- Model/profile/revision, prefixes, window strategy and all retrieval parameters.
- Baselines, comparison plan, report destinations, timestamp and unblinding authorization.

Choose parameters using development data before freezing. Keep the old development and
held-out labels and reports unchanged. Establish that the requested semantic mode executes
the intended index/profile; fallback must be reported explicitly rather than counted as
an actual semantic run. The seal proves fixture identity, not relevance or source truth.

If the final evaluation corpus is larger than the 1,308-edition grounding snapshot,
declare that choice before unblinding, prove that all scored anchors remain exact, and
record the full lexical corpus and semantic coverage separately. Compare candidates and
baselines on the same corpus, fixture digest and `k`. Do not transfer results between
different distractor populations or call covered-subset scores whole-library quality.

## One-time final run and release

Only after the recorded freeze, allow the fixture to be unblinded. Use the existing
owned evaluation-copy workflow; never construct `Library` on a frozen source directory.
For example, after authorization and with the required compatible index available:

```sh
BOOKRAG_DATA_DIR=data/snapshots/library1308-v1 \
BOOKRAG_MODEL_CACHE="$PWD/data/models" \
.venv/bin/bookrag evaluate \
  --dataset data/sealed-evals/final-large-v1/labels.json \
  --mode hybrid --output experiments/final-large-v1/hybrid.json
```

The example is a post-freeze procedure, not a command run during set preparation.
Run planned modes in fresh processes. Retain failures and actual-mode/fallback counts.
Reject a run whose source, generation, fixture or frozen candidate changes. A schema
migration, source checkpoint or index rebind belongs to explicit snapshot preparation,
never an implicit repair of final inputs. A new-inode SQLite copy does not inherit a
valid partial-index binding automatically; see `docs/EVALUATION.md`.

Publish the committed fixtures, provenance, validation code and candidate/results after
the one-time final check so others can reproduce it. Verify their bytes against this
prior commitment. Keep acquired archives, large vectors/models, unrelated runtime data
and private notes outside Git. Once a set has been used for tuning, it is development
data; a new final set requires a new version, fresh labels and a new prior commitment.

## Limits of the judgments

Ground truth is agent-authored, **not human-rated**, with no inter-rater validation.
Queries were written after reading the target passage and may be easier than natural
user requests. Anchors are non-exhaustive: strict passage scoring can reject another
valid answer. Book-description cases retain premise evidence but use book-level relevance.
Wrong-book negatives combine authoring-agent reasoning and distinguishing-term absence;
they are not formal non-entailment proofs or claims that no answer exists in the corpus.

Exact canonical text checks do not prove faithful transcription from print, OCR quality,
the present-day accuracy of historical assertions, or that a narrator/contributor's words
express the book author's own position. Sampling favors variety over a representative
distribution. Final metrics must retain these qualifications and the declared corpus and
coverage bounds.
