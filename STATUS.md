# Status

Updated 2026-11-14T21:36:49.577563+00:00. Goal ACTIVE. User asked for a verified all-time/hour status and said CONTINUE; report delivered at2026-11-15 00:16MSK. Receipt experiments/status-alltime-2026-11-14T2116Z.json. Earlier hour receipt experiments/hourly-status-2026-11-14T1224Z.json.

## Published / services

Public repo https://github.com/rebrudiy/book_rag. Five published commits: a223654,892061a,1a1a7e1,75acbd0,7e86928. Both CI jobs passed for7e86928: https://github.com/rebrudiy/book_rag/actions/runs/43608473643. Iteration6 ready, publish now; root preparing exact staged scope before partial-UI agent edits.

Full interface http://127.0.0.1:8001 (exec94109/PID98259), demo http://127.0.0.1:8000 (exec65399/PID98257). Full library lexical; large semantic index NOT YET active. Demo8 works/excerpts/45passages has optional legacy hybrid. Monitoring exec30751/PID1237 writes data/runtime-observations.jsonl every60s; observed gap16:11→21:14UTC, do not invent an exact hour baseline or cause.

## Corpus / recovered jobs

CURRENT 3991 indexed editions / 1401120 passages; coverage {"full": 3989, "partial": 1, "selected_chapters": 1}; languages [{"language": "de", "count": 1009}, {"language": "en", "count": 1940}, {"language": "fr", "count": 1031}, {"language": "ru", "count": 11}]. All3956 files/extraction hashes physically checked atuser-report time; then resumed35pending/failed and finished ALL3991 successfully in147.696s. No pending/failures remain. Final queue receipt data/expand-text-resume2-report.json. Previous stop receipt data/expand-text-with-translations-report.json retains diskfloor stop +DNSfailure.

Full unique-work total UNKNOWN. Lower bound24 curated full works; partial D2L separate,60periodical editions. Work IDs/provisional3987groups are not verified unique books.

Source/embedding stopped after a5hour observation gap: embedding log showed only219,611,136Bfree duringfailure, then16GiBfree whenrootreturned; cause notestablished. Original ingest/embedding processes are DEAD, do not callthemworking.

Embedding RESUMED with executable scripts/embed_frozen.py, exec25024/PID4056, source data/snapshots/library1308-v1 (1308editions/445466passages). Current checkpoint 1408224/1865985 vectors (was1315072atfailure). readonly immutable SQLite source prevents schema migration. Command: BOOKRAG_MODEL_CACHE=./data/models PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.65 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.5 .venv/bin/python scripts/embed_frozen.py data/snapshots/library1308-v1 --batch-size32 --resources data/library1308-resume-resources.jsonl. Reports data/library1308-embedding-resume-report.json / -resume-progress.log; memory sampler sameprocess every30s. LastGPUdriver~1.1GB/recommended12.7GB, CPUpeak~0.9GB before suffix encoding. Oldsource command/session40120 no longer running.

## Ready iteration6 / verification

- Saved title/note/collection PATCH+UI; query/quote/citation/provenance/historicalversion immutable.12isolated Chromium workflows +historical11/smoke15/catalog205 passed; zeroJSerrors. Saved single-item reads avoid loading allnotes.
- Licensed RU Regex reference indexed: ruwikibooks-regex-266423,25sections/55513chars,27literalexamples/333inlinecode/4tables44rows172cells, upstreamWikiBooks+Wikipedia credits. Explicitpartial/provisional/unverified completion. Actual GET-only browser anddownloadSHA passed; docs/WIKIBOOKS_BROWSER.md. SourceFB2sha83d7579ab5fc2a6120d14569243c5109e0d09371224138b567072214eed27737. HTMLoldid rendering may use currenttemplates; savedresponse/hash/time explicit. retrieved_at fromoriginalHTMLreceipt, packaged_atseparate;27source tests, zeroHTTPcachedrepeat.
- Streaming RUWikiBooks pinned25,354,533B dump→168,842,618BXML/21430pages,7531ns0;8.90s/RSS79.8MB;39tests. CLI catalog-wikibooks creates REVIEW catalog only, no book-count promotion. CLI acquire-wikibooks-reference --index is bounded onepage.
- Full integrity audit2528books/841599passages/all2528rawfiles0errors: experiments/full-integrity-iteration5.json. NEW all3991 audit PASSED:3991rawfiles/1401120passages/0errors,68.045s; experiments/full-integrity-3991-v1.json includes exact input fingerprints+UTC timestamp.
- FTS maintenance: owned20copy252.6→44.5MB with identical8probeIDs/scores; larger1308alreadycompact1.594GB→samebytes, NOsavings. Originalcontrols unchanged. Reports experiments/fts-maintenance-v1.json and fts-maintenance1308-v1.json; scripts benchmark_fts_maintenance.py. Fixed its finaltyunion typing; global Ruff/ty pass; one docformat fixed.
- Root global pytest728passed10.98s includes ready futureindexmodules too. Publishediteration6 expected646tests ifindexhelpersexcluded. LastpublishedCI linksabove. Browserarchive regression passed after genericdownloadlabel «Скачать зафиксированный файл» (derivedFB2 is not rawproviderHTML).

## Next index integration / agents

Coverage helper FROZEN: index_coverage.py /46tests /benchmark_index_coverage.py /docsINDEX_COVERAGE /experiments/index-epochs-v1.json. Explicit ensure_epoch_schema(conn) still NOT wired to live Library. Strict source bytes/headers/section ownership/bounds/slice checks; LRU4; cheapqueryguard~8.52ms for1308syntheticmetadata books vs5sfullsubsethash. Per-booktombstoneepochs+namespace+DDLcookie+inode; drift failclosed, never automatic acceptance byclaimedSHA. Writecost1.60xinsert/1.71xrefresh accepted. Root reviewed; enable in nextcommit afterpub6.

Installer FROZEN: index_installation.py /36tests /docsINDEX_INSTALLATION. install_partial_index(source,target,model_cache=...,hardlink=True) and read_installation_receipt(target,active). Complete ready ONLY; no staging/legacyunproved generations. Everyinput/window/id checked withlocalpinnedtokenizer. Schema2keychecksums cover unique keys, notallrows; currentbigbuild has~0.2%duplicates. Ifall duplicatesbitwiseequalverifiedfirstrow: hardlinks+honest reconciledproof. Otherwise deriveownmatrixfromverifiedfirstrows (100%reuse/0encode), newchecksums/receipts/windowstream proof; originalsource unchanged. Diskpreflight4GiB+scratch/materialization. repeatedsourceWAL gates, sealedmetadata fieldcompare, metadata-symlink-before-read fixes; actualtinyready190→ownedtmp sourceSHAunchanged.

/root/interface now prepares partialcoverage UI in NEWpatch/tempcopies ONLY untilrootpub6push: optional semantic_coverage {indexed_books,searchable_books,active,requested_book_covered}; do not leave stalebadgeafterfallback. Root will wire semantic/Library/API/CLI; agentmustnot edit these.
/root/corpus now independent span-only STORAGE EXPERIMENT onownedcopy, no production changes/downloads/MPS/newchildren. Goal measure deletingduplicatep.text and reconstructsection[start:end], preserveallIDs/hashes/FTS/results. ProductioncurrentlyINCOMPATIBLEuntilroot SQL changes; no accidental adoption.
/root/interface/pipeline_review completed coverage/installer independentreview; available afterroot wiring for integrationreview.

## Controls / limits

Never mutate original curated20-v3/E5 DBs, old dev/heldout labels/reports. Owned evaluationcopies now preserve sourcebytes; realisolatedbaseline sourceSHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8. Expandeddev54 originalMiniLM MRR.6826/hit.7955/negativeabstention.2; fixedpoolMRR.67121 regression recorded, E5notpromoted. No OCR/generativeprose/automaticstance, no newsealedfinalsplit, workidentity mostlyprovisional. Imagesnotrendered; sourcescopeexplicit. Runtime/model/index/notes ignoredGit, project .venv only,16GiBRAM/10CPU/MPS, diskfloor4GiB, no paidAPI. Lastauthenticweeklyquota55%remaining at21:15:23UTC, reset2026-11-15T12:12:16UTC. Recheck scripts/check_codex_limit.py currentrollout /Users/rebrudiy/.codex/sessions/2026/11/14/rollout-2026-11-14T13-57-03-01a1d9b4-2279-7993-a084-3011527a9369.jsonl; stopnewwork<=3%.
