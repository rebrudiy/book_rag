# Status

Updated 2026-11-14 around 15:52 MSK. Goal ACTIVE; continue autonomous work.
GitHub https://github.com/rebrudiy/book_rag, commits a223654 and 892061a published; their CI passed. Iteration3 ready for publication after local checks.

## Corpus / real product

- Initial 1,000-Gutenberg queue FINISHED: 1,000 complete editions + one partial D2L work = 1,001 indexed editions, 325,201 passages at completion; all downloaded/extracted/indexed. Distinct work IDs are provisional; only 19 complete work groups manually curated (plus partial D2L). 60 periodical editions, not 1,000 confirmed unique books.
- Balanced 305-edition queue NOW INGESTING: data/catalog/balanced-curated.json, logs data/balanced-ingestion-progress.jsonl, final report data/balanced-ingestion-report.json. Preserves existing/curated metadata. Selected EN100/FR100/DE100/RU5; only3 overlap existing index. Source serial2s, diskfloor4GiB. Previous1,000 job exited successfully.
- Live full interface http://127.0.0.1:8001 (lexical search; whole-live semantic index NOT built). Demo8/4languages/45passages at http://127.0.0.1:8000 has working hybrid search. Both HTTP200 at last probe.
- Pinned D2L15sourcechapters acquired/imported as one partial work;148 sections;483 code/math blocks verified exact. Raw source/license/cache/provenance outsideGit.
- Corpus agent has acquired5Russian originals/81chapters, but fixing omitted epigraphs before finalfullcoverage claim. WAIT finalmanifestnotice before import data/russian/wikisource/manifest.json. Do not count them indexed yet.
- 13 safe editorial rules from attribution audit applied ONLY future live curatedmanifest, artifactSHA guards prevent transferring to different editions. Frozen20 benchmarks unchanged. Seven mixed/ambiguous contributor cases remain blocked from automatic rules.

## Search experiments / truthful limitations

- Frozen snapshot data/snapshots/curated20-v3:20 full editions,19works,12,108passages. Offline currentparser reimport done, no saved notes. receipt updated. Shared model cache data/models.
- MiniLM pinned window index:49,738vectors,214.572s. New checkpoint batches flush/fsync + atomic cursor/checksums; interrupted-prefix resume tested; immutable active generations.
- Expanded dev54 grounded cases/44positive/10negative/12crosslanguage: baseline MRR .6144 hit .7045; lexical .5386/.6136 with negativeabstention .9; hybrid .6826/.7955 with negativeabstention .2; reranked .6686/.8182 with .2. All exact quotes/metadata/reader anchors1.0. Do not hide no-answer failure.
- E5 explicit query/passsage prefixes, pinned MIT model, isolated snapshot data/snapshots/curated20-e5 (same source fingerprint/IDs).12,343vectors in285.896s. Expanded E5hybrid MRR .6705/hit .7273/crosshit .4167/negativeabstention0; no defaultpromotion. Model floors not comparable across embedding spaces.
- Calibration dev-only: .90target cosine accepts1/14 answerable internalvalidation; reranker no feasible.90threshold with minimum3 accepted. No defaults or heldout changes.
- Agent UI now fixing outputlimit-dependent hybrid candidate pool; will use fixed100distinct /≤2000windows, Library.passages batch method READY. This intentionally creates next experimental version; keep older reports frozen.
- Original release-v0.2 heldout reports remain frozen, no tuning/reuse. Need new sealed final set before new release claims.

## Verification / uncommitted work

- Latest complete local suite: 320 tests passed; Ruff/format/ty pass. User hour-status response at15:24 separately reported295 then-passing tests. Latest global lint/ty pass. Browser15 workflows plus205-record pagination fixture, actual beyond-first-page book and Jowett attribution passed.
- Full audit snapshot499originals/160,338passages exact, report experiments/full-integrity-v3.json. Frozen20 audit20/12,108 exact. CLI verify now distinguishes incomplete original availability.
- Added source-preserving preformatted code, consistent SQLite snapshots, whole-library integrity command, modern acquisition, balanced planning, bounded server-side catalog/typeahead and pipeline state labels. CI browser job added locally; not yet pushed/run remotely.
- Hourly verified status snapshot experiments/hourly-status-2026-11-14T1224Z.json. Last hour report posted to user; they requested brief status then CONTINUE, not stop.

## Agents / next actions

Lead: track balanced ingestion, wait complete Russian manifest then import; publish iteration3 after targeted checks; prepare frozen larger corpus semantic build. Library.passages batch method implemented/tested, notify UI done.
Corpus: finish RU epigraph/body/rights/revision audit + cached regeneration; mocks pass; no broad crawl.
UI: semantic fixed candidate pool + prefix consistency with batch evidence reads; no defaultmodel/prefix/build changes.
Evaluation: calibration and attribution audit COMPLETE; available for independent actual relevance review or next controlled experiment.

Current interpreter .venv; uv in ignored .bootstrap/bin/uv. No paid APIs.16GiB RAM,10CPU; source/model/indexdata ignored. Weekly quota AVAILABLE from current-session rate_limits10080-minute window: latest62%remaining. Recheck between iterations, stop new work at<=3%; no token proxy. Current rollout /Users/rebrudiy/.codex/sessions/2026/11/14/rollout-2026-11-14T13-57-03-01a1d9b4-2279-7993-a084-3011527a9369.jsonl.


## Subsequent iteration updates

- Five complete Russian originals NOW INDEXED: Ivan Ilyich, Underground, Overcoat, Fathers and Sons, Ward6.81 narrative chapters/87 parsed sections; five verified new complete work identities raise curated full-work lower bound19→24 (partial D2L counted separately).
- Live corpus at last probe1,214 editions/405,337passages, 112DE/138FR/10RU; balanced job still has92discovered+1downloading+1oversizefailure. Source botany pg63371 imagesEPUB exceeds100MiB; corpus agent proposes same-ID no-image/text variants offline, never raises cap.
- Pagination/typeahead complete, actual beyond-first-page and source partial/full labels browser-tested. Russian source acquisition/revision/epigraph fixes complete and cached.
- Fixed candidate-pool100/≤2000windows plus batch evidence method complete, real5/20/50 prefix invariance verified for both models/modes. Paired reports experiments/candidate-pool-v1 show mixedquality, no defaultpromotion.
- Evaluation agent reviewing actual source relevance where single exact-anchor labels may be incomplete; proposals only, no frozen label/score edits.
- New CI browser job pending firstremote run. Next publish iteration3, finish balancedcorpus, resolve oversize botany via bounded officialvariant, then freeze/reindex largerlibrary and build whole-corpus vectors with checkpoints.
