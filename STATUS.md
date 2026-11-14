# Status

Updated 2026-11-14T22:22Z. Goal ACTIVE; user questions steer continuing work, not cancellation. Latest authentic weekly quota51% remaining at22:18:39UTC; reset2026-11-15T12:12:16UTC. Read scripts/check_codex_limit.py against original rollout /Users/rebrudiy/.codex/sessions/2026/11/14/rollout-2026-11-14T13-57-03-01a1d9b4-2279-7993-a084-3011527a9369.jsonl. Stop NEW work<=3%; preserve all state/agents.

## Published / running

Public https://github.com/rebrudiy/book_rag. Six commits through8f5703d (editable notes, audited RU reference/dump discovery, integrity/storage monitoring); both CI jobs passed https://github.com/rebrudiy/book_rag/actions/runs/43643976120. Next subset-index integration+independent authority/storage experiments ready to publish after final review.

Full http://127.0.0.1:8001, PID13136/exec63854. Demo http://127.0.0.1:8000 PID13134/exec83400. Both restarted with new tested subset-capable backend; full still lexical until explicit ready installation. Demo8 works/excerpts/45passages has legacy hybrid. Monitor PID1237/exec30751 writes data/runtime-observations.jsonl every60s. Past gap16:11→21:14UTC cause UNKNOWN; do not invent exact hour baseline.

## Corpus / active build

3991 indexed editions /1401120 passages, pipeline all indexed/no pending or failures. Coverage full3989,partial1,selected_chapters1; languagesDE1009/EN1940/FR1031/RU11;60periodicals. Unique-work total UNKNOWN:24curated full-work lower bound,25curatedgroups includespartialD2L,3987mostlyprovisionalgroups. Final ingestion147.696s receipt data/expand-text-resume2-report.json. All3991 raw files + canonical spans checked,0errors/68.045s: experiments/full-integrity-3991-v1.json.

Embedding PID4056/exec25024, data/snapshots/library1308-v1 (1308editions/445466passages), checkpoint1837088/1865985 at22:22UTC. NEVER activate staging. Frozen sourceDB SHA2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0. scripts/embed_frozen.py uses readonly immutable SQL and no Library migration. Batch32/MPS ratios0.65/0.5, telemetry data/library1308-resume-resources.jsonl; report/progress data/library1308-embedding-resume-{report.json,progress.log}. Temporary caffeinate -i -w4056 (exec52380), no persistent setting. Disk14.65GiBfree, floor4GiB. Original ingest+embedding jobs DEAD; failure reported219MBfree after unobserved gap, later16GiBfree, causeUNKNOWN.

When source vectors/active.json appears COMPLETE: BOOKRAG_DATA_DIR=./data/full .venv/bin/bookrag install-semantic-index data/snapshots/library1308-v1 --model-cache data/models > data/partial1308-installation-report.json 2>data/partial1308-installation-progress.log. Installer validates all windows/IDs/actualtext, may derive3GB matrix for unequal schema2duplicate rows, zero new encoding. Never mutate original snapshots/models/evals.

## Current integration / verification

New index_coverage.py uses per-book tombstone epochs/namespace/schema/inode, exact content+headers/span proofs, cheap pre/postguards~8.52ms synthetic1308. New index_installation.py verifies complete ready generations, exact localtokenizer input mapping, IDs/vector/keymap checksums, schema2duplicate reconciliation, atomicreceipts and4GiBreserve. Root Library explicitly migrates writabletarget tracking; frozen source is never constructed as Library. Query keeps global lexical candidates, semantic coveredonly, earlywordsforoutsidebook/language; corrupt/stale/changedwhileencoding => currentverifiedlexical. UI shows honest editioncoverage and clears obsoletebadges.

Evaluation uses owned evidenceDB plus read-only ORIGINAL semantic_reference to respect receipt inode without impersonation. Nonempty original partialWAL rejected; never checkpoint/migrate source. Sourcefile footprint guardsdiscard changedruns. Focused25 partial/eval testsPASS; full767PASS/16.75s in data/checks-partial-final.log (exec69873 finished). Independent final integration review found no remaining blocker after language fix. Ruff/tyPASS. Actual isolated integrated browser8scenariosPASS/0JSerrors: data/checks-partial-browser.log, artifacts/partial-coverage-real-api; fakeencoder only tests protocol, no relevance claim.

Iteration6 historical archive/annotation browser12+11+15+205scenarios passed; nousernotes reset. Canonical saved versions lazysnapshotted onfirstsave; fixedrawbytesdownload available; generic «Скачать зафиксированный файл» because generatedFB2 is not providerHTMLoriginal. RU Regex fixedrevision25sections55513chars,27literalcodeblocks/4tables, explicitpartial with upstreamcredits; references docs/WIKIBOOKS_IMPORT.md and WIKIBOOKS_BROWSER.md. Wikibooks dump7531ns0pages are discovery notsearchablebooks. No OCR/generatedanswers/automaticstances/imagesrender.

## Agents / next

/root/interface: new starter shelf in ignoredtemp/patch ONLY until root commit7 signal. Current partialUI frozen/verified. /root/interface/pipeline_review: independent root integration read-onlyreview; outside-language gap found+fixed with2tests, awaiting final. /root/corpus: span-migration read-only audit/doc; do not edit shared library/semantic/coverage until coordination. Prior authority200audit FROZEN: experiments/authority-200-v1.json,50answered (1candidate49noexactclaim),150unavailable aftertimeoutsecondrequest; no retries before23:06:28.869486Z and no needautomaticretry.20cachedfrontmatter reviewed; confirmed increment0;56testsPASS.

Span owned-copy experiment experiments/span-storage-v1.json:1308DB1.594GB→0.940GB(-41.03%), all445466spans/FTS hashes/8queryIDs identical; production CURRENTLY INCOMPATIBLE until SQL/text/proof paths adapted. Rawsection reconstruction latency worse; require batchedchaptercache. Float16 halfbytes/highproxy recall notproductionpromoted. Do not delete active data or frozen controls to make space; only ownedexperimentcopies after review.

## Evaluation controls / receipts

Frozen20 curated20-v3 DB SHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8;20ed/19groups12108passages49738MiniLMwindows. Preserve original20/E5 DBs, olddev/heldoutlabels/reports. Expandeddev54 originalMiniLM MRR.6826/hit.7955/negativeabstention.2; fixedcandidatepool MRR.67121 regression, E5notpromoted. Agent-authored labels nothumanratings; no newsealedfinalsplit. Oldheldout13 .9333MRR/hit1/neg.6667 onlydemocorpus, nevertransferlargelibrary. Reports of user's requestedall-time/hour audit experiments/status-alltime-2026-11-14T2116Z.json and hourly-status-2026-11-14T1224Z.json. Project dependencies in.venv,16GiB/10CPU/MPS, no paidresources.
