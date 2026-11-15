# Status

Updated 2026-11-15T05:20:08.174286+00:00. Goal ACTIVE; stop new work at authentic weekly Codex remainder3%, explicit user stop, or an actual environment limit. Last verified remainder6% at2026-11-15T05:11:07.522Z, reset12:12:16Z. Never substitute tokens for quota. Exact local quota-log path and continuation details are in ignored data/continuation-current.json.

## Use the product

Main: http://127.0.0.1:8001 . Already running through scripts/run_full.sh, PID85900 / exec95140, log data/server-full-20261115T0513Z.log. Restart from the repository root with:

```sh
./scripts/run_full.sh
```

The launcher uses existing data/models/indexes and performs no acquisition. Main generation profile is experimental qwen3-4b-instruct-2507; fresh-checkout registry default remains1.5B. Optional flags and overrides are documented in docs/TRY_IT.md and .env.example. Demo8000 now runs current API atPID83700/exec87767, data/library.sqlite3 (9local editions/90passages; no reset). Candidate8002 is stopped; runtime monitorPID1237 remains intentional.

GitHub: https://github.com/rebrudiy/book_rag . Latest published58b4fc3, all3CI jobs green: https://github.com/rebrudiy/book_rag/actions/runs/43680368937 . Includes scoped URL restoration, visible results/reader return, optional query planning, bounded compact indexing and source-proof acceleration. Root is preparing state notes; check git status for a standalone compact-manifest prototype only.

## Actual corpus and semantic availability

Live /api/stats:11,001 indexed/searchable editions,2,293,173 passages,11catalogue languages. Latest ingestion statuses allindexed. Coverage labels10,998full/2partial/1selectedchapters. These labels do not prove complete unique works:10,997 provisional groups,35curated groups,60periodicals, confirmed-full-work lower bound33. True unique complete-work count UNKNOWN.

All7,000 planned additions downloaded/extracted/indexed; final source audit0errors. No downloader or embedding builder is running. Sources/artifact hashes/rights/chapters are preserved; models/corpus/indexes/notes stay outside Git. All editions have lexical search, metadata discovery and a reader.

Actual ID-set union was audited (do not guess overlaps): global baseline1,308; Russian layer21 with10already in baseline; direct-live batches96+1,000 disjoint from baseline/RU. Therefore **2,415 distinct editions are available through book-scoped semantic routes**. Unscoped global dense search remains1,308. Russian language-only route covers21. Another8,586 indexed editions lack full-text vectors. Prior derived2,420/16additionalRU counts were corrected to2,415/11 after the actual ID audit; old unrelated rank2420 results are preserved. See experiments/semantic-coverage-union-v1.json.

Metadata description index covers all11,001 editions; title/author/subjects only. Open23positive fixed-edition probes:4@1/9@12,MRR.270; real misses/nonsense neighbors remain. Never label nearest suggestions as identified books or body evidence. Real browser description→reader/back/reload/language/mobile passes,0JSerrors.

## Recent measured improvements

Completed direct-live1000 batch:1000editions/10languages/53,889passages/49,491,880characters/190,601windows. Explicitly interrupted at116,480durable windows for model QA, resumed74,121new rows with455chunks reused and no durable re-encoding. Uncommitted repeated work is unmeasured. Final source/model guards pass; resumed-only encoding347.787s. Owned130,878,924B; resumed encoder/native peak847,216,640/71,811,072B. Allworkersclosed. Data under data/semantic-batches/multilingual1000-v1; combined registry data/semantic-overlays/multilingual1096-registry-v1/registry.json is ENABLED.

Runtime1000same-book/query probe: cold21.373→4.220s,warm13.767→.916s in separate read-only observations; all10IDs/order/scores identical(maxdelta0). Registration still scans full canonical data; runtime reuses strict schema/namespace/inode/epoch proof, rehashes4untracked metadata tables and verifies returned canonical spans. Bounded256digestcache checksdev/ino/size/mtime/ctime, neveratime.39mutation/cache regressions. See experiments/semantic-batch-runtime-v2.json. Actual mainbrowser4open probes (Kant/Kafka/Voltaire/Dostoevsky), exact reader/back/mobile/refresh,0JS/write/Ask. Direct links preserve mode/book/language, unknown books never broaden globally;11isolatedflows +17Answerregressions pass.

Actual RAG run05:14opencases,11answered/3abstained,13/14answerability-status matches (NOT factualaccuracy);32sourcecards/13claimcitations exact,15/16anchors retrieved. All17candidate source/receipt hashes unchanged; measured commitffac8ab and sourcezip preserved. Independent AI review (not human) retains wrong Smith-property axis, qualifications and Origin refusal despite evidence. Source hashes/citations do not certify generated meaning. Partial-claim retention and multilingual NLI gates previously failed; neither is deployed.

Later opt-in source reranker v2-local32 corrected weak function-phrase bias. Frozen11pool replay preserved earlier anchors and moved Smithown-labour8→1. One actual4B comparison answered32.3s with BOTHdirectanchors. AI review confirms the earlier wrong axis is removed, but still flags Smithneighbor-harm qualification loss and a thin Marx explanation. No general factual-accuracy score. See experiments/query-planning-v4.json and rag-property-v4-agent-review.json. Default nonplanning retrieval/prompt remains unchanged. Inline ASCII quoted speech and final source-scope metadata now validate correctly; source-language races reject output.

## Checks and current work

Latest complete local suite:1,667PASS36.80s (data/bench/full-suite-20261115T0508Z.log). Ruff --no-cache /format/ty pass. CI3jobs green on58b4fc3. Fresh no-Torch Python3.14 environment data/ci-env-20261115 also passed native/core177 and answer/planner182 targeted checks. Keep primary.venv nativeMetalwheel; do not uv sync it casually.

Control bytes freshly rehashed: originalcurated20 SQLite ea1feab7...; original1308 SQLite2d77675b...; original embeddings92239c40...; idsf95cf7c6...; installationreceipt5e80856e... all expected. Raw-byte-only audit data/bench/frozen-controls-20261115-final.json, no SQLite constructor used on controls.

Root owns integration/state/publishing. Interface finished/froze app.js/deeplink tests; pipeline_review finished/froze proof acceleration and AIreview; no remainingjobs. Corpusagent now owns NEW standalone compact_window_manifest.py +repackCLI/tests: repack existing sealed1000inputs to offset/prefix/hash records WITHOUTtextduplication; compare every input/hashchain with old manifest, no model/GPU/download, no productionactivation or oldfile deletion. It is a prototype pending tests/measured receipt; do not count new semantic books from it. Preserve its state/stop if quota reaches3%.

## Exact next task / limits

Finish the compact-manifest prototype and measure actual1000repack; oldregistry still requires oldmanifest presence, so NEVERdeleteoldfiles as cleanup. Then design a VERSIONED registration/reader contract permitting compact inputs or retiring duplicates without losing original source proof. This is needed before streaming the remaining8,586 editions under4.5GiB disk reserve. Current registry caps16batches/8192books/200k windows perbatch are first-stage limits; larger coverage needs measured capacity and explicit global routing/calibration, not unreviewed merging with the frozen float32 baseline.

RAG paraphrases, author comparisons, numerical/mood/recipient qualifications remain fallible. Catalogue language/authorship/work identity is not authority-verified for most corpus. No OCR for scanned PDFs. Closed40-case final set has NOT been run/unblinded; keep new open probe IDs recorded and freeze a suitable candidate/protocol before final testing.

## Immutable controls and closed final

NEVER READ data/sealed-evals/final-large-v1/labels.json. Root retrievalruns0/unblindedfalse. Safe seal evals/final-large-v1.seal.json; labels SHA2a2906f80e9a72d31220d2bac661d0788603495a66da81d159aa1f2c52e9d277. No tuning from hidden cases.

Preserve original20/E5/1308DBs, models/vectors/receipts, old labels and failed reports. Never initialize/checkpoint/migrate frozen databases, resetepochs, impersonatebindings or overwrite immutable generations. Span-storage prototype remains production-incompatible. Earlier history data/continuation-state.md; latest actual machine state data/continuation-current.json. No paid APIs/equipment, no secret/corpus/model/index publication.
