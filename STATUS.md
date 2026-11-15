# Status

Updated 2026-11-15T03:50:00Z. Goal ACTIVE. Last authentic weekly remainder: **19%**, reported at 2026-11-15T03:42:39.833Z; reset 2026-11-15T12:12:16Z. Check scripts/check_codex_limit.py against the original 2026/11/14 rollout ending 01a1d9b4-2279-7993-a084-3011527a9369.jsonl. Stop new work at 3%; never use token counts as quota.

## Try the product

- Main: http://127.0.0.1:8001 — PID62310, log data/server-full-20261115T0332Z.log. Opened in the user's browser. Source code changes to Python require restart; static assets are current on reload.
- Home → «Найти книгу по описанию» → describe a book → «Читать». Metadata discovery covers all **11,001 editions**. Russian example: «происхождение видов и естественный отбор»; English example: «a girl follows a rabbit into a strange underground world». Both selected target editions rank3 in these open checks.
- Main passage search supports words/hybrid/meaning; **full-text vectors remain 1,308 editions**. The completed Russian21 pilot is not yet live. All editions have lexical text search and reader access.
- Generated answers are experimental and can contain unsupported meaning despite exact source quotations. Default local model remains Qwen2.5-1.5B; the stronger 4B candidate is not promoted.
- Demo8000 PID52814; experimental 4B server8002 PID52810. Runtime monitor PID1237 writes data/runtime-observations.jsonl. These PIDs were verified alive. No corpus downloader or embedding batch is running.

GitHub: https://github.com/rebrudiy/book_rag . Latest published 2fb2e19; CI pending for this commit. Earlier a5f62de had both jobs green: https://github.com/rebrudiy/book_rag/actions/runs/43667628667 . Published c47a674 records the first compact-vector experiment. Russian lexical fallback, model profiles, strict RAG follow-up and metadata discovery are now committed and pushed. Remaining experimental/overlay/planner changes are separate.

Restart command (the main service is already running):

```sh
BOOKRAG_DATA_DIR=./data/full BOOKRAG_MODEL_CACHE=./data/models HF_HUB_OFFLINE=1 BOOKRAG_GENERATION_CONTEXT=8192 .venv/bin/bookrag serve --port 8001
```

## Actual corpus

Live /api/stats: **11,001 searchable editions, 2,293,173 passages, 11 languages**, all latest ingestion attempts indexed. Coverage labels:10,998 full /2 partial /1 selected chapters. Languages:en4497,fr1636,de1614,es605,fi605,it605,nl605,pt500,sv231,da82,ru21.

Unique complete works remain UNKNOWN. There are10,997 provisional groups,35 curated groups,60 periodical editions and a confirmed-full-work lower bound33. Edition counts do not prove the requested10k unique-work target.

All7,000 additions in data/catalog/scale10000-v2/queue.json are indexed; no remaining failures/deferred/downloads. Final audit:7,000 artifact/canonical hashes and archive links;891,794 passages;181,321 sections;0 errors. No protected1,308 editions were reindexed. Official ibiblio batch47175 finished02:11:46Z; residual50292 finished02:15:32Z. All former serial/pglaf/ibiblio runners are closed. See experiments/corpus-scale-final-v1.json and bulk-sources-v2.json.

## Verified product and experiments

Metadata book discovery:11,001×384 float32,16,897,664 bytes;11,059 metadata windows built31.724s. This is title/author/subject retrieval, not full-text search. Open fixed-target fixture:4/23 at1,9/23 at12,MRR0.270; real misses and irrelevant negative suggestions retained. Never call nearest-neighbor scores confidence. Twelve backend +10 real temporary-Library API tests pass. Live browser:7 flows, actual index, reader/return/reload/source-language filters,390/320 widths,0JS errors and0non-GET requests. Latest artifacts/book-discovery-20261115T034840Z/report.json. UI now shows12 suggestions and readable single-column mobile cards; technical availability panels are collapsed.

Russian lexical fallback: scoped, unquoted, only after ordinary lexical returns empty. Snowball prefixes are verified against actual passage stems; normal baseline and strict quotations stay unchanged. Open3-case source retrieval improves1/3→2/3; Land's nonempty but poor retrieval is still a limitation. See docs/RUSSIAN_RETRIEVAL.md.

RAG dev14: original baseline1.5B3 answered/10 abstained/1 invalid,4/13 answerability-status matches. Soft question quotes and RU stemming keep strict policy; same3/10/1 outcome in run03. Rejected partial-claim policy yielded8 answers but independent AI review found serious factual/question errors; its patch/results are preserved and policy is reverted. Strict 4B run04:11 answered/3 abstained,13/14 status matches and exact citations, **not factual accuracy**. Review exposes Smith/Leskov/question-axis/whole-book absence errors. No human final score. See experiments/rag-partial-v2.json and rag-run02/04-agent-review.json. Independent NLI experiment also confidently accepted wrong recipients/imperatives/numbers; no NLI threshold is deployed.

Compact-vector work: optional FAISS stays in isolated CPU subprocesses because importing FAISS with Torch aborts this platform. First65,536-window ANN experiment at nprobe32:exact-top10-in100 recall.98125 (candidate approximation, not relevance). Incremental shards resume after six crash points, keep canonical source bindings and avoid retaining full float32 matrices. Real RU21 pilot:21 books/1,282 passages/6,162 encoded windows,512 durable then resume without re-encoding;23.263s MPS,19.7MB owned output,92 focused tests,9 exact reader browser flows. Public anchor ranks2/1/2. It is not installed yet. See docs/RU21_SEMANTIC_PILOT.md.

Encoder capacity same1024 inputs: MPS284.82 vectors/s,CPU136.99. Do not extrapolate old long-book density to the new shorter corpus as an exact completion time. Streamed encoding and model/native workers must coordinate GPU/disk. Current free space approximately6.4GiB; preserve4.5GiB reserve.

Authority/overlap proposals did not mutate live work identities. Wikidata official P2034 map matched555 of10,984 PG editions, all uncertain scope; unresolved10,429. Full paragraph overlap audit11,001 editions found unsafe high-overlap catalogues/selections and useful edition/volume containment; no automatic merge. Preserve negative results.

## Checks, ownership and next action

Latest complete local suite: **1,416 passed in34.66s**, log data/bench/full-suite-20261115T0350Z.log. Ruff/ty pass; browser script formatting fixed. Earlier suite exposed repository-only scripts imports in query-planning tests; corrected with a scoped path fixture. Counts are engineering checks, separate from user improvements and factual accuracy.

Root owns API/answers/static UI/model profiles and integration/publication. /root/interface owns minimal semantic dispatch; child pipeline_review owns semantic_overlay module, live rebinding wrapper, isolated worker and tests. They are implementing an opt-in scoped RU21 overlay; no global merging/promotion. /root/corpus owns query_planning/helper/tests and up to3 explicit offline4B planner probes, investigating an invalid Russian→English plan and a general proximity rerank. No overlapping edits.

Next: complete source-bound RU21 overlay, validate real scoped meaning search in the main browser, publish completed features and verify CI; extend compact full-text coverage to additional books. Keep all open failures. Do not stop for an individual milestone.

## Immutable controls and closed final

NEVER READ data/sealed-evals/final-large-v1/labels.json. Root remains unblinded; retrieval runs0. Labels SHA2a2906f80e9a72d31220d2bac661d0788603495a66da81d159aa1f2c52e9d277. Safe public seal:evals/final-large-v1.seal.json. Freeze a concrete candidate before final testing; no tuning from closed labels.

Preserve original curated20-v3 SQLite SHAea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8 and library1308-v1 SHA2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0, all original vectors/models/receipts and old reports. Never invoke Library on frozen originals, checkpoint them, reset epochs or impersonate inode bindings. Span-storage prototype remains incompatible with production; no full live migration. Detailed earlier history:data/continuation-state.md. Models, corpus, indexes, user notes and raw private cases stay outside Git.
