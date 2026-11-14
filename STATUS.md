# Status

Updated 2026-11-14 17:43 MSK. Goal ACTIVE; user requested a verified hour report and explicitly said to continue. Report delivered; receipt experiments/hourly-status-2026-11-14T1224Z.json.

## Published / running

Public repository: https://github.com/rebrudiy/book_rag. Published commits a223654, 892061a, 1a1a7e1. Both test/browser CI passed for 1a1a7e1: https://github.com/rebrudiy/book_rag/actions/runs/43596986973. Iteration 4 currently uncommitted; integrate, verify and publish next.

Live full library: http://127.0.0.1:8001 (lexical; whole-live semantic index unavailable). Demo: http://127.0.0.1:8000 (eight works/excerpts, four languages, 45 passages, multilingual hybrid enabled). Servers restarted with iteration4 availability/edition-link changes; bothHTTP200.

Latest verified live probe: 2,338 indexed editions / 772,097 searchable passages; still increasing. Total distinct full works unknown. Confirmed lower bound: 24 curated complete-work groups; partial D2L separately. 60 periodical editions are not counted as confirmed unique books.

## Background jobs / corpus

- Initial 1,000 Gutenberg queue completed. Balanced305 queue completed, plus pinned partial D2L and five Russian originals. Oversize PG63371 botany recovered through same-ID official no-image EPUB (1,187,675 bytes,123sections,1623passages); text-only/illustration omission explicit.
- New serial text-only queue: data/catalog/expand-text-with-translations.json (2,682 entries:2,679expansion+3translations), current exec session43016; data/expand-text-with-translations-progress.jsonl and eventual data/expand-text-with-translations-report.json. Logs append after deliberate code-reload restart. Source2s pacing; no parallel Gutenberg downloader.
- Original-source archival and table-parser refresh of original1,308 completed. Snapshot data/snapshots/library1308-v1: 1,308 indexed editions,445,466passages; source subset data/catalog/refresh-archive-tables.json. Snapshot receipt data/snapshot-library1308-v1-report.json. Do not mutate its content during embedding.
- Whole frozen1,308 MiniLM build STARTED17:24MSK, PID84916 / exec session40120: BOOKRAG_DATA_DIR=./data/snapshots/library1308-v1 BOOKRAG_MODEL_CACHE=./data/models .venv/bin/bookrag embed --batch-size128. Report data/library1308-embedding-report.json; stderr data/library1308-embedding-progress.log. Initial window-counting phase can take minutes before first checkpoint. Vector arrays/mapping preflight includes4GiB reserve.
- Bounded warmed256-window MPS throughput sample experiments/batch-throughput-v1.json: batch128~291.8windows/s, all testedbatchoutputs identical; not a whole-library throughput claim.
- Five Russian originals fully indexed (Ivan Ilyich, Underground, Overcoat, Fathers and Sons, Ward6):81 narrativechapters,87parsedsections,866434canonicalchars. Provider revision/hash/rights bundles preserved; author epigraph/dedication restored, unverifiededitorialnotes explicitly omitted.

## Implemented iteration4 (not all yet published)

- artifacts.py immutable content-addressed originals; parse archive before indexing, preserve source filename fallback. GET /api/artifacts/{sha} verified attachment; saved quotes point to historical raw artifact even after reindex. No downloaded corpus/model/vector files in Git.
- PRE/code XML-space and tables preserve line/row/cell fidelity. PG10912 audit50sections/all57headings/zero missingvisiblewordtokens; D2L483code/mathblocks exact. Attribution rules guarded by artifactSHA, optionalsectionordinal and actualtext phrase.
- source_pipeline separates latest ingestion attempt from the last searchable edition. Failed/deferred/processed refresh retains exact old text, metadata, FTS, savedlinks and archive; atomic successful replacement. New ingestion_* API fields/counters, separate availabilityfilter; 13targetedchecks.
- semantic.py/vector_reuse.py schema2 exactinput+frozenmodelspace keys reference immutable mmap rows. No speculative old-index reuse; durablekeymap sync precedes vectorcheckpoint. Real isolated183+7 experiment:183reused/7encoded, fresh190outputs identical and querytop5same. Activegeneration stays old until successfulnewgeneration.
- corpus_planning.py --text-only selects explicitRDF no-imageEPUB/TXT/HTML, preserves original305default byte-for-byte. Plan3005EN1000/FR1000/DE1000/RU5;995RUshortage explicit.
- work_identity.py read-only CC0 Wikidata proposals:50sample16linked/3ambiguous/31unresolved; no automatic count/DB promotion. Russian translation links verified: PG600→ru-underground, PG47935/30723→ru-fathers-and-sons. Corpusagent preparingprioritymanifest; all3downloaded/audited/indexed:77novelchapters,915exactpassages; experiments/translation-integrity-v1.json.
- Library.get_book related_editions bounded20 only samecuratedworkid/statusindexed on BOTH sides; no provisionalidentity navigation. Added confirmed_full_works_lower_bound stats separately from cataloggroups.

## Experiments / truthful limitations

Original frozen20 curated20-v3 and E5 snapshot plus old dev/heldout reports/labels remain controls. Do not open oldfrozenSQLite through normal Library constructor: schema migration changesfileSHA. Read-only directsqlite is safe; evaluation-agent implementingownedcopyworkflow.

Expandeddev54:44positive/10scopednegative/12crosslanguage/20editions, sourceanchorschecked but agentauthored labels. Originalexpanded baselineMRR.6144/hit.7045/negativeabstention.4; lexical.5386/.6136/.9; MiniLMhybrid.6826/.7955/.2; reranked.6686/.8182/.2; E5hybrid.6705/.7273/0. Quotes/metadata/anchors1.0. E5/reranking notdefaultpromoted.

Fixedcandidatepool100distinct/<=2000windows now prefixinvariant5/20/50. Pairedcandidate-pool-v1 MiniLMMRR.67121(regression), E5.67803(smallgain); reportold/new honestly. Calibration.90target rejectsmostpositives; no universalconfidence thresholdapplied. Relevance review20cases/58passages foundincompleteanchors; proposals only, oldlabelsunchanged.

Historical fullintegrity audit499originals/160338passages passed; does NOT cover allnewbooks. Frozen20audit20/12108passed. Need freshlargeaudit afteractivejobs stabilize.

Knownlimits: noOCR, no generativeanswer prose, noautomaticauthorstance/argumentlabels, no full-livevectorsyet, no newsealedfinalsplit. Text-only sources can omitillustrations; explicitmetadata/UI notice. Existing savedquotes survive reindex with frozenrawsource but historicalcanonicalreader notyetimplemented.

## Agents / checks / next action

Root: supervise ingestion/build, integrate/publish iteration4, then stablelargehybridserver/querymeasurements.
/root/interface: UIavailabilitywarnings/counters and readerrelatededitionnavigation; static/docs only.
/root/corpus: threeprioritytranslationmanifests from verifiedRDF; then offlinepermittedRU sourceplan. No secondGutenbergdownloader.
/root/interface/pipeline_review: owned isolatedevaluationcopies + SHAimmutabilitychecks; no coreLibrary/semantic/UI edits.

Latest complete suite521passed; globalRuff/format/ty pass. ActualRussian5works, archivehistorydownloads and4realrelatededitionpairs passed Chromium with0JSerrors. Newglobalpytest finalrecheck session31274. Recheck allafteragentsfreeze; browser workflows including actualRussianbooks/archive download available scripts/browser_russian.py and browser_archive.py.

.venv projectonly, uv .bootstrap/bin/uv, sharedweights data/models,16GiBRAM/10CPU/MPS. No paidAPI. Authentic weeklyquota59%remaining at14:24:05UTC, reset2026-11-15T12:12:16UTC. Recheck betweeniterations with scripts/check_codex_limit.py current rollout /Users/rebrudiy/.codex/sessions/2026/11/14/rollout-2026-11-14T13-57-03-01a1d9b4-2279-7993-a084-3011527a9369.jsonl. Stop newwork<=3%, saveallstate/agents.

## Current continuation

- Frozen1308 vectorbuild progress atthisprobe: 231552/1865985 windows; recentdurablecheckpoint, jobrunning.
- Livequeue restarted onceagainwithnewparser: nowcachedprevious2,682queueentriesreparse to restorebareDIVbylines and fix EPUBcontinuationchapterlabels. Existingeditionremainssearchableduringrefresh.
- Owned evaluationcopy workflow complete; actual baseline run experiments/isolated-baseline-v1.json preserved frozen20DB SHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8.
- Interface agent now independent CPUonly vectorcompressionbenchmark; childpipeline_review preparing canonical historicalversionshelper forlazyfirst-save archival (notyetintegrated). Corpusagent finalauditdocs/thenboundedRUsourceresearch. Do notstageunfinishednextfeaturefiles.
