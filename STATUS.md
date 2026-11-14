# Status

Updated2026-11-14T22:45UTC. Goal ACTIVE. Last authentic weekly quota49% remaining at22:39:40UTC; reset2026-11-15T12:12:16UTC. Read scripts/check_codex_limit.py original rollout /Users/rebrudiy/.codex/sessions/2026/11/14/rollout-2026-11-14T13-57-03-01a1d9b4-2279-7993-a084-3011527a9369.jsonl. Stop NEW work<=3%; preserve records/agents. User questions do not cancel continuing goal.

## Published / services

Public https://github.com/rebrudiy/book_rag. Seven commits through01e72c5 (verified subset install/epochs/query/CLI/UI/evaluation guards). Both CI jobs PASS https://github.com/rebrudiy/book_rag/actions/runs/43647733667. Next commit starter shelves+real availability+diagnostic/evaluation/browser tools ready; do not accidentally stage unfinished RU connector.

Full http://127.0.0.1:8001 PID13136/exec63854, NOW semantic/hybrid1308of3991. Demo http://127.0.0.1:8000 PID13134/exec83400,8works/excerpts45passages legacyhybrid. Monitor PID1237/exec30751 every60s data/runtime-observations.jsonl. Prior16:11→21:14UTC observation gap causeUNKNOWN; don't invent exact hour baseline. Caffeinate tied to completedPID4056 has exited; other caffeinate processes are not owned by this iteration.

## Corpus / complete index

3991 searchable editions,1401120passages; all source attemptsindexed/no pending/failures. Coverage full3989/partial1/selected_chapters1. LanguagesDE1009/EN1940/FR1031/RU11;60periodicals. Unique-work totalUNKNOWN:24curated full-work lower bound;25curatedgroups includespartialD2L;3987mostlyprovisionalgroups. Receipt data/expand-text-resume2-report.json. All3991rawfiles+canonicalspans independently audited,0errors/68.045s: experiments/full-integrity-3991-v1.json.

Large frozen1308 build FINISHED, PID4056/exec25024 DEAD, never call it running.1308ed/445466passages/1865985windows; resumed1315072+encoded550913 in3518.027s. SourceDB SHA2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0 unchanged. Source data/snapshots/library1308-v1, gen-1812f290f02e09f392e2c880bbcee602de30282ec4ad406300e1c6ff1393555f-a8b86bbaa7d0. Immutable ready active, no staging. Build report data/library1308-embedding-resume-report.json; resources data/library1308-resume-resources.jsonl (peakRSS~2.8GB/MPSdriver1.1GB).

Installer FINISHED PID13728/exec63201 DEAD; data/partial1308-installation-report.json. Main active data/full/vectors/active.json bound to real target. Everywindow/ID/actualspan/header verified.3928duplicate rows allbitwiseequal firstrows: schema2_reconciled_exact, hardlinks all6files, no extra matrix/encoding. Receipt vectors/installations/install-fda51e49bb044a228a1e1d1ab70bb911.json SHA5e80856e5fb35e1f02e743f433398edf8a593fc3a8e776c618f139b602e19599. Original sourceDB/arrays/maps never edited. Epoch guards before/afterquery, outsidebook/language earlylexicalfallback, unrelatednewbooks remain globally lexical.

## Actual usability / measurements

Home starter shelf implemented/frozen: realmetadata RU5/EN6/FR4/DE4 onfull, bounded lazy≤6queries×limit8 perlanguage, reader+scopedsearch clear oldquery withoutmodelcall; missing/wrongauthor unavailable notpromoted. Sourceversion/partial labelsretained. ActualtempAPI/Chromium8workflows+emptycase+liveGET8000/8001 passed,0JSerrors; existing15smoke+205pagination passed onownedtemp. Artifacts starter-shelf-browser and starter-shelf-regression. Root reviewedscreenshots.

Actual full browser PASS: artifacts/large-semantic-browser/report.json,5screenshots. Exactly1global semanticAlice query (8.292s) +1outsideRegexearlylexical(28.05ms); coverage1308/3991, independentcanonicalquote+readerhighlight,390pxnooverflow,0JS/consoleerrors,0nonGET. scripts/browser_large_semantic.py is GET-only and opt-in, notCI demo.

Exploratory realqueries receipt experiments/semantic1308-availability-v1.json: globalRU Alice→ENAlice top1(3.143s warm), scopedRU Kafka→Germanopening top1(.471s), scopedTolstoylife/death→chapterIX top1(.518s). All20hits across4queries independently exacttext/metadata/readerlink checked. KNOWNFAILURE globalRUKafka didNOTfindKafka top5 (otherFR/Tarzan .63 vsKafka .394); do not cherry-pick scoped success asglobalquality. FirstglobalcoldKafka15.03s. These are exploratory inspectedsmokes, notheldout/relevance estimates.

Paired54 EVALUATION RUNNING PID16542/exec51364: scripts/evaluate_semantic_subset.py data/full --model-cache data/models --output experiments/large-subset-dev-v1. Log data/large-subset-dev-v1.log. Baseline phase slow/mostlyI/O; do not claimstuck/dead withoutcheck. Oneowned5.368GBSQLite copy in systemtemp,4GiBreserve, currently~9.6GiBfree (normal~14.5GiB). No writes/checkpointoriginal; partial-reference actualoriginal identity guards. No liveimports/src*.py changes/modeljobs untilrunends. This rerunsDEV54 curatedtargets with3971distractors, negativecases explicitlywrongbookscope; alternativeedition relevance incompletelylabeled. Modes shareprocess/caches/RSSnotisolated; reports onlypublishafterguardsexit.

## Agents / pending code

/root/interface: frozen starter +NEWdiagnose_semantic_candidates.py/5fakeCPUtests ready (noactualmodelrun). Now tiny FTS queryplan/hydration/envelope equivalence experiment, no production/coreedits untilfreezeends. /root/interface/pipeline_review: balancedlanguage topicmap selection inignoredpatch/temp only, no liveAPI/model/core/staticedits. /root/corpus: NEWrussian_short_sources.py +tests/docs/audit script preparing10complete originals/8authors, source-SHA-bound notes review.43source/audit testsPASS,0newacquisitionHTTP until23:06:28.869486UTC sharedprovider cooldown. Newconnector src currentlyFROZEN; afterevaluator correctSaltykovedition fromprimaryRVB (20volumes/Hudozh1974vol16bk1pp30–34; laternotesexcluded). LiveDB untouched.

Lastfull local787PASS13.51s BEFORE subsequentdrafttests; publishedcore CI767PASS. Newdiagnostics5fakeCPU PASS; RU43tests overlaps17existing. Don't add counts asuserfeatures. Ruff/ty lastcore PASS. NewCI steps partialbrowser+starterbrowser prepared. Include readynewdiag/tests; excludeunfinishedshortRU files fromnextcommit. Source *.py frozen during evaluator, standalonescripts/docs canadvance.

## Controls / constraints

Frozen20 curated20-v3 SHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8,20ed/19groups/12108passages/49738MiniLMwindows. Keep20/E5DBs, olddev/heldoutlabels/reports byte-identical. Expandeddev54 oldMiniLM .6826MRR/.7955hit/.2neg; fixedpool .67121MRR regression; E5small notpromoted. No OCR/generatedanswers/automaticstances/imagesrender. RussianRegex25sections55513chars/27literalcodeblocks/4tables remainspartial, notfullwork; dump7531namespace0pages discoverynotbooks. Historicalsavedversions/notes/privatecollections neverreset. Genericdownloadlabel «Скачать зафиксированный файл» because generatedFB2 isnot providerrawHTML.

Span experiment experiments/span-storage-v1.json1308DB1.594GB→.940GB(-41.03%), all445466spans/FTSdigests/8probes exact. ProductionINCOMPATIBLEuntilallSQL consumers+FTSOLDdeletehydration+explicitstorage_mode+epochreinstall adapted; docs/SPAN_MIGRATION_PLAN.md inventories. Conservative4GiB+3×5.368GB preflight currentlyfails; don'tmigrate. Authority200audit50answered/1candidate/49noexactclaim/150unavailable aftertimeout,20frontmatterreviewed,confirmedincrement0; no cooldownbypass. Project.venv only16GiB/10CPU/MPS/no paidresources; diskfloor4GiB. Oldrequestedstatus receipts experiments/status-alltime-2026-11-14T2116Z.json and hourly-status-2026-11-14T1224Z.json.
