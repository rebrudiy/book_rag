# Status

Updated 2026-11-15T00:13:15.650859+00:00. Goal ACTIVE: continue until authentic weekly Codex remainder <=3%, explicit user stop, or actual environment limit. Last verified quota42% at2026-11-14T23:50:28UTC, reset2026-11-15T12:12:16UTC. Check scripts/check_codex_limit.py against the original rollout under ~/.codex/sessions/2026/11/14 (thread01a1d9b4-2279-7993-a084-3011527a9369); never use tokens as a quota proxy.

## Product and corpus

Public https://github.com/rebrudiy/book_rag. Latest verified published CI is ca23c5a, bothjobsPASS: https://github.com/rebrudiy/book_rag/actions/runs/43649799313. Iteration9 ready to publish now; check actualgitlog onresume. Local full http://127.0.0.1:8001 PID22105/exec12918; demo8000 PID22103/exec62319. Monitor PID1237/exec30751 records data/runtime-observations.jsonl every60s. Unique logfiles should be used nextrestart (old reusedlogs contain sparseNULpadding).

4001 indexed editions/1401379passages; allattemptsindexed/no failures. DE1009/EN1940/FR1031/RU21. Coveragefull3998/partial2/selected_chapters1;60periodicals. Unique-work total UNKNOWN:33curated full-work lower bound/35curatedgroups/3997mostlyprovisionalgroups. New10 RUeditions=9full+1partialunknownChekhovnote;259newpassages. Actualsource/projection/ingest receipts experiments/russian-short-v1.json and russian-short-live-v1.json. Full4001rawfiles+allspans audit0errors/66.65s: experiments/full-integrity-4001-v1.json.

Semantic/hybrid1308of4001,445466passages/1865985windows, complete immutable MiniLMsource+installedreceipt; newoutsidebooks/languages fall back explicitly to words. Lexical spansall4001. Data/models/indexes/notes outsideGit. HomeRU6 includesPushkin; bookopening skipsgeneratedBookRAGcredits butTOC/explicitlinks retain them. ActualGET-onlybrowser/deepreload passed artifacts/starter-shelf-original-opening. No usernotesreset. No OCR/generatedanswers/automaticstances/imagesrender.

## Verified changes and experiments

Current deployed CTE selects eligible candidates before hydration and applies same-snapshot scopedrowidbounds. Ownership must match passage/section book beforeLIMIT, single/batch/save; historicalnotes preserved. Fourlarge pairedqueries retained bitexactIDs/BM25/metadata/citations; scopedMeditations12347→88ms/Darwin3125→69ms, globalmanstill14997ms. Nativeprototype separately4001pair17368→625ms exact, notyetdeployed; boundedPythonheap doesnotboundallSQLiteinternalmemory.

Valid3991 dev54 in experiments/large-subset-dev-v3: actual54 baseline/lexical/hybrid each, allsource/label/codeguardsPASS. PositiveMRR .5803/.5076/.5928; hits30/44,24/44,32/44; cross4/12,0/12,7/12; scopednegativeabstention4/10,9/10,2/10. All597returnedquote/metadata/readerchecks100%; noanswerstillweak. Hybridp50~1.90s/p95~13.47s, processpeak3.46GiB, sharedcachetimingsnotisolated. Failuresv1(SHM/emptyWAL falseguard) andv2(allhybridfallback fromdroppedHFrelativecachelinks) retained, bothfixed+realofflineencodeverified. Docs/LARGE_RETRIEVAL.md.

Kafka knownfailure: targetopeningrank7826global/2420DE excludedfrom2000windows/100passages; freshstoredcos1/maxdiff1.23e-7; reranking100can'trecoverit. E5-base512 completedowned20index12343x768,680.204s, batch8/MPS2.13GiB, allsource/activeguardsPASS. Paireddev54 SAME DB/corpus/code: MiniLM .6712MRR/35hits/cross8of12/neg2of10 vsE5base .6515/32/cross5/neg0. NOTPROMOTED. Reports experiments/e5base-dev20-v1, build20receipt; modelpaidcost0. No embedding/model/evaljobs running now, onlyservers/monitor.

## Next actions / team

1. Commit/push iteration9 and verifyCI. Lastlocalfull917PASS16.69s/Ruff/ty/format/diff clean. Publicmetadata only; never stageignoredruntime rawtexts/models/labels.
2. Then tell /root/interface to APPLY artifacts/native-integration-v1/integration.patch (currentlygitapplycheckPASS; sourceunchanged,57focused+4runnerchecksPASS). Runprepared4001dev54 exactresponsecomparison beforebrowser/deploy/CI; controls~240sbudget, no finalfixture use. Current package isstillCTE, do not claimnative deployed.
3. Nextqualityexperiment isolates E5 context budget or localquerytranslation; no E5base512 promotion. Filtered semantic scoring can avoid fullmatrix scans if equivalence verified. Grow corpus under4GiBreserve andrights.

/root/interface waitingpushsignal/nativepatch; /root/corpus finished61sourcechecks/capture/indexhandoff; /root/interface/pipeline_review finishedsealed/storagetasks. Allfilesfrozen, no backgroundjobs. Detailed machine receipts/commands/history in ignored data/continuation-state.md; originalfailed gapcauseUNKNOWN.

## Frozen controls and CLOSED final

Keep original curated20-v3 and E5small/1308DBs/vectors/olddev/heldoutlabels/reports byte-identical.20DBSHA ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8;1308DBSHA2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0. Build4056/install13728/E5build25613 allDEAD/completed.

DO NOT READ data/sealed-evals/final-large-v1/labels.json. Rootnotunblinded, retrievalruns0. LabelsSHA2a2906f80e9a72d31220d2bac661d0788603495a66da81d159aa1f2c52e9d277. Safe public evals/final-large-v1.seal.json commits40cases/34positive6scopednegative/28unseentargetbooks,42canonicalchecks, fourlanguages; agentauthored/not humanrated. Genericvalidator+31inventedtests/protocol in docs/FINAL_EVALUATION.md. Explicitcandidatefreeze before anyfinalrun/inspection/release.

APFSclone20MiB ownedexperimentpassed(newinode/SHA/COW isolation), nofullsourcecloneperformed. Spanstorage-41% remainsproductionINCOMPATIBLE; no migration/epochreset/bindingimpersonation. Hardware16GiB/10CPU/MPS/free~13GiB, project.venv, no paidAPIs/equipment. Continue immediately afterpublishing, never completegoalatMVPmilestone.
