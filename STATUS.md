# Status

Updated 2026-11-15T01:29:39.637120+00:00. Goal ACTIVE. Continue until authentic weekly Codex remainder <=3%, explicit user stop or actual environment limit; never use tokens as a quota proxy. Last verified remainder33% at2026-11-15T01:35:36UTC; reset12:12:16UTC. Use scripts/check_codex_limit.py on original ~/.codex/sessions/2026/11/14 rollout01a1d9b4-2279-7993-a084-3011527a9369.

## Product

Public https://github.com/rebrudiy/book_rag, latest published963ddf2; bothCI jobs PASS https://github.com/rebrudiy/book_rag/actions/runs/43663625010. Corpus acquisition code committed. Native/scoped search and RAG changes deployed locally but next coherent commit pending real-browser check.

Full http://127.0.0.1:8001 PID44840/exec6067; demo8000 PID42826/exec58811. Unique logs data/server-full-20261115T0140Z.log and server-demo equivalent. HFoffline, sharedcachedata/models, context8192. MonitorPID1237/exec30751 records data/runtime-observations.jsonl. Do not reset user notes or demo corpus (currently9 local editions).

At this timestamp: 4808 searchable editions/1483114 passages, 4804 mostly provisional candidate groups; full coverage labels4805. Unique complete works UNKNOWN; curated lowerbound33,35curated groups,60periodicals. Semantic remains1,308 editions/445,466passages/1,865,985windows; lexical spans all indexed books. Partial semantic coverage is explicit.

New Answer tab now retrieves, generates offline with pinned Qwen2.5-1.5B Q4_K_M, selects canonical evidence IDs, inserts exact support quotes+offsets, validates aliases/literalquotes and source versions before/after generation/judgement. Same-model entailment/question check rejects claims; it is not proof. No evidence means no model call. Search works without generation. Real known Alice watch question answered correctly with exact quote in~10s; wrong-topic quantum hallucination was exposed and rejected by new judge. Broader quality remains unmeasured; global descriptions can still fail. Real browser/model checks passed: watch answered with exact reader link8.795s; wrong topic abstained5.467s;0JSerrors. Scoped eligibility SQL now seeks passages_book instead of scanning all passages. See docs/ANSWERS.md; raw exploratory failed/successsmokes data/bench/local-rag-smoke-v*.json retained.

## Active acquisition and priorities

Serial downloader PID37343 STOPPED at01:37:43UTC, resumable; single active BULK runner PID45107 with shared lock. Bulkplan/log/state in data/catalog/bulk-generated-v1/. Pilot20 and first78 passed full audit; current pglaf observed17.6books/min, no acceleration. Peer probes ibiblio metadata for a new sequential pilot. Old serial details: data/catalog/scale10000-v2/queue.json, queue.run-state.json and pipeline.log. Command scripts/scale_corpus.py --manifest ... --limit7000 --run. Check actual with .venv/bin/python scripts/corpus_scale_status.py. Frozen7,000-candidate queue excludes baseline4,001/protected1,308; first150 skipped onresume, no reindex. Explicittext-only/noimages source metadata;6959EPUB/41TXT,10new languages. Eligiblemetadata55,267groups, targetnotachieved. Measured~21books/min; projectedcompletion is not guaranteed. Runtime preserves4.5GiB working reserve/providerbackoff. New458-book independentaudit40,304spans/artifactSHA0errors, hardlinkphysicaldedup verified; experiments/corpus-scale-v1.json.

User explicitly prioritizes10k–50k corpus + actualRAG over more small retrieval experiments. Parallel agents investigating officialPG rsync bulk to remove legitimate per-HTTPrequest overhead; metadataonly sofar, existingrunnercontinues. No competingpayloadacquirer/handoff until exactpath/size/provenance/lock/disk plan validated.

## Verified code/measurements

Full local1122tests PASS21.51s, Ruff/ty/format/diff clean beforedeployment. Browser fake-generator realAPI17workflows PASS,all5statuses,0JSerrors; quotes/reader/mobile/stale/sourcechange/judgefalse verified. These test counts are separate from user features and do not measure real-model quality.

Native lexical integrated: dev54/54 exact IDs/BM25hex/fullrecords/citations,0mismatch, pairedtotal86.979s→5.772s; p9511979→422ms. Legacybaseline38/54 exact,3controltimeouts/13skipped; entire108comparison INCOMPLETE/validatedfalse. No all108equivalenceclaim. scripts/benchmark_lexical_large.py tracer updated for newnativeSQL; testsPASS.

Scoped scoring verified on actual frozen1308matrix:3scopes bit-exactfullscorearrays/maxerror0, samecandidateIDs100/83/100; originalDB/generationfullhashes unchanged. Follow-upsinglepairs Meditations408.735→1.266ms, Darwin544.612→2.118ms,de385.955→38.247ms. CPUfreshworkers/oneBLASthread/cachehostcaveats, notendtoend/relevance. experiments/scoped-vector-v1.json.

Valid3991dev54 report experiments/large-subset-dev-v3 remains baseline; all597source/reader checks exact. Noanswerweakness recorded. E5-base512 lost paired20 experiment, NOTPROMOTED. No more E5tuning now. OpenRAGdevelopment fixture evals/rag-dev-v1.json has14cases/10editions/16auditedanchors,23runnertests; actual14-casecollection DONE:13 scored answerability cases/4matches,1invalid_output;26sources and4claimcitationchecks valid; semantic/factualreview remains unreviewed. experiments/rag-baseline-v1.json. Do not claim semantic correctness from sourcequotechecks.

## Team / next actions

/root/interface finished/frozen native/scopedproof/UI+17browserflows. /root/corpus prepared/froze14caseRAGdev and now owns NEW bulk_sources helper; /root/interface/pipeline_review finished/froze75runtimeguardtests and researches officialPG rsync metadata. Root owns answers.py/API/backendtests/livebrowser/modelchecks. Coordinate non-overlappingfiles. Current serverrealmodelworker lazy; no embedding build underway.

1. Finish real browser/model positive+wrong-topic checks, preservefailures, publish native/scoped/localRAG+UI+devfixture commit and verifyCI.
2. Validate officialrsync selectedfilelist, boundedbulkhelperofflinechecks and safe serialhandoff to increase realsearchablecorpus faster. Existingpipelinecontinues meanwhile.
3. Run openRAGdev14 on deployedAPI, reviewfacts separately, improvequality fromdevelopmentonly. Expand semanticcoverage efficiently under actualdisk/RAM.

## Immutable controls and CLOSED final

NEVER READ data/sealed-evals/final-large-v1/labels.json. Rootnotunblinded/retrievalruns0. LabelsSHA2a2906f80e9a72d31220d2bac661d0788603495a66da81d159aa1f2c52e9d277; safepublicseal evals/final-large-v1.seal.json. Freeze candidate beforefinalrun; no tuning fromlabels.

Preserve original curated20-v3 DB SHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8 and library1308-v1 DB SHA2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0, all model/vector controls and oldlabels/reports. Never Library(originalDB), checkpoint, epochreset or bindingimpersonation. Installedpartialreceipt5e80856e5fb35e1f02e743f433398edf8a593fc3a8e776c618f139b602e19599. Spanprototype remains productionINCOMPATIBLE; APFScloneownedtest passed but nofullsourceclone. Only2authorizedownedbenchmarkcopies removed2,539,282,432B withproof; sourcecontrols intact.

16GiB/10CPU/MPS, project.venv. No paid APIs/equipment. Models/corpus/indexes/notes/privatecases outsideGit. Detailed older machinehistory data/continuation-state.md; preserve failures, do not rerun completed work merely aftercompaction.
