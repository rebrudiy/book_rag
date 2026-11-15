# Status

Updated 2026-11-15T05:33:36.186410+00:00. **PAUSED at the explicit user stop condition: authentic weekly Codex remainder3% at2026-11-15T05:31:07.930Z**, reset12:12:16Z. This is a quota stop, not project completion. No new development/experiments/indexing; all agents saved and stopped. Exact quota source and machine state are in ignored data/continuation-current.json.

## Use the product

Main: http://127.0.0.1:8001 . Verified /api/stats at stop. PID85900, exec95140, log data/server-full-20261115T0513Z.log. From the repository root:

```sh
./scripts/run_full.sh
```

The launcher uses existing local data and performs no acquisition. Main generation uses experimental qwen3-4b-instruct-2507; fresh-checkout default remains1.5B. Demo8000 PID83700 remains available. Candidate8002 stopped. Diagnostic monitor1237 explicitly stopped at the quota boundary. No download, encoder, native-index builder or generation experiment remains running.

GitHub: https://github.com/rebrudiy/book_rag . Main contains scoped links/UI, optional query planning, runtime source-proof acceleration and standalone compact storage. Separate **compact-registry-v2** branch checkpoints the not-activated consumer; do not describe it as deployed. Last verified all3CI-green main before this stop checkpoint:64c42c7, https://github.com/rebrudiy/book_rag/actions/runs/43680633140 . Check newer workflow status on GitHub; never infer success from an earlier commit.

## Critical extraction finding / exact next task

**HTML/EPUB extraction drops raw body text between recognized block tags whenever other sections are present.** Confirmed cached source pg16384-full, O Mandarim: canonical text has only456characters while the raw EPUB contains substantial prose outside p tags. Original cached artifact SHA3262e7b16c0e7c08e971b6532be35851a7196067f64fb3ad9f508bdead318735 remains available. Headings surviving does not prove a complete book.

Read-only triage of the32shortest remaining editions flagged25 with canonical/all-visible-HTML ratio below0.1. This deliberately biased sample and visible-HTML comparison include headings/navigation; they are NOT a corpus-wide error rate or a completeness score. See experiments/short-edition-extraction-triage-v1.json. No real canonical book has been reimported or modified. **Parser is still unchanged; no fix/test was begun before the stop.** Agent handoff: data/bench/parser-loose-flow-handoff.json.

NEXT: add original CC0 tests and repair generic loose text/inline/BR flow while preserving headings, order, tables, verse, preformatted code and no duplication; audit cached artifacts before controlled reimports. Reimports invalidate passage IDs/epochs/citations/vector bindings, so plan notes/citation migration and rebuild affected indices. Preserve frozen controls; never silently rebind old vectors.

## Actual corpus / search scope

Live stats: **11,001 indexed editions,2,293,173 canonical passages,11catalogue languages**. All ingestion statuses sayindexed. These are records with searchable extracted text, NOT11k verified full unique works. Full-source labels10,998full/2partial/1selectedchapters do not establish extraction completeness.10,997provisional workgroups/35curated/60periodicals; API confirmed-full-work lower bound33. True count of full unique works UNKNOWN, especially given the extraction defect.

All11,001 records have lexical search/metadata discovery/reader access to the available canonical text. Actual ID union: global dense baseline1,308; RU21 overlaps baseline by10; direct-live batches96+1,000 disjoint. **2,415 distinct editions have book-scoped semantic routes; unscoped global semantic search remains1,308.** Another8,586 lack full-text vectors. Prior erroneous2,420 total was corrected by actual intersections; see experiments/semantic-coverage-union-v1.json. Vector presence also does not prove complete source extraction.

The new short1000-v2 selection (1,000previously unserved editions,21,290,272characters/20,960passages;922en/32pt/28de/13fr/5fi) was explicitly interrupted after the extraction finding. It adds **ZERO** searchable editions. Checkpoint19chunks/4,864windows persisted; parent87843 andchildren87845/87846/87926 verifiedclosed. Native complete/ready/registration never occurred. Encoder aborted cleanly on parent failure; resource tracker warned about one semaphore cleanup. Log data/bench/semantic-short1000-v2.log; complete failed-run record under data/semantic-batches/short1000-v2. **DO NOT RESUME before repairing and auditing extraction.** Selection receipt experiments/semantic-short1000-v2-selection.json.

## Completed storage / quality work

Inactive compact prototype exactly reconstructed all190,601 sealed inputs twice, matching every UTF8SHA and ordered input chain:99,586,048→12,857,344bytes (-87.09%),29.832s,493MiB peak RSS. No model/GPU/download. Both old and new files remain, so no production disk space was freed. Source guards passed. Later review hardening rejects orphan rows, checks final file SHA before/after, cleans only owned output after failed receipt publication and prevents CLI report symlink overwrite. Sixteen focused checks pass; the hardened source was NOT remeasured on the full batch. Main code commit394df7a; experiments/compact-window-manifest-v1.json.

Isolated compact-registry-v2 adds a model/tokenizer-free reader, exact complete stream/hashchain/mapping proof, explicit immutable v2 pins, source guards and250MiB/1MiB pre-hash limits; v1 preserved. Owned CC0 fixture can read/register without its old full-text payload. **88focused tests pass,1real-native test excluded**, Ruff/ty pass. This branch is a saved candidate, not merged, not real-source registered and not activated. Old real manifests remain required by current main.

Previous measured semantic1000 runtime: cold21.373→4.220s,warm13.767→.916s with all10IDs/order/scores identical; strict source/metadata/canonical quote guards retained. Browser Kant/Kafka/Voltaire/Dostoevsky, reader/back/mobile/reload passed with0JS/write/Ask. Deep links preserve book/language/mode and unknown scope never broadens globally.

RAG remains experimental. Open run05:14cases/11answered/3abstained;13/14answerability-status matches are NOT factual accuracy. Exact32sourcecards/13claimcitations do not certify paraphrases. One later4B Smith/Marx comparison fixed the wrong-topic source selection but still lost Smith's neighbor-harm qualifier; AI review is not human validation. Metadata description discovery11k has measured misses (4@1/9@12 across23open positives). No paid APIs/equipment; rejected NLI/partial-claim/E5 experiments stay rejected.

## Checks / agents / immutable state

Latest full local suite before the new standalone module:1,667PASS36.80s, data/bench/full-suite-20261115T0508Z.log. New standalone module separately16PASS0.57s; Ruff --no-cache/format/ty pass. Do not add these counts and call it a new complete-suite run. Branch88focused checks are separate from user improvements and the full suite. Fresh no-Torch Python3.14 environment earlier passed177native/core and182answer/planner checks. Preserve primary.venv Metal llama.cpp wheel; avoid casual uv sync.

All agents frozen: corpus completed compact hardening and saved parser findings only; interface completed scoped UI and checkpoints v2 branch; pipeline_review completed guard/AI review and17independent compact-reader checks. Root saved integration/records/publishing. No active agent work remains.

Control audit data/bench/frozen-controls-20261115-final.json verified originalcurated20 DB ea1feab7..., original1308 DB2d77675b..., original embeddings92239c40..., idsf95cf7c6..., installationreceipt5e80856e... using raw bytes. None were migrated/checkpointed/rebound/reset. Preserve all originals, models, vectors, failed receipts and controls. Models/corpus/indices/notes stay outside Git.

**NEVER READ data/sealed-evals/final-large-v1/labels.json.** Root unblindedfalse/retrievalruns0; safe seal evals/final-large-v1.seal.json, labelsSHA2a2906f80e9a72d31220d2bac661d0788603495a66da81d159aa1f2c52e9d277. New open triage IDs are recorded; freeze a suitable final protocol before any closed run. Project remains unfinished.
