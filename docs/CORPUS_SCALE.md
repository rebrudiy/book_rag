# Resource-aware full-text corpus growth

The current scale run adds metadata-screened, full-text **candidate workgroups**.
They remain provisional identities. Normalized author/title equality and exact
content hashes remove known duplicates; they do not prove that every remaining
record is a different literary work. Translations, retitled editions, hidden
anthologies and homonymous titles still need authority/contents review.

## Frozen input and selection

The input is the already acquired official Gutenberg offline RDF archive at
`data/catalog/rdf-files.tar.bz2`, SHA-256
`86a11d5cf64ac9bf3f8c59cbc6bdbe505185884c66d84a0930be48b02f6d31a3`.
No website enumeration or new catalog crawling is required. See
[SOURCES.md](SOURCES.md) for the primary
[offline catalog](https://www.gutenberg.org/ebooks/offline_catalogs.html),
[robot policy](https://www.gutenberg.org/policy/robot_access.html) and
[permissions](https://www.gutenberg.org/policy/permission.html). Every accepted
record explicitly says `Public domain in the USA`; this is a jurisdictional
statement, not worldwide permission or an MIT license for texts.

`book_rag.corpus_scale.plan_scale_corpus` streams RDF members without extracting
archive paths. A separate planning SQLite database stores candidates and dedup
proofs, keeping the whole catalog out of Python memory. It excludes:

- every source ID already in the library, including the protected 1,308-book
  semantic coverage set;
- non-text records, missing/unverified rights, explicit periodicals, volumes,
  chapters/components, collections, and unresolved collective authors;
- records without an approved same-Gutenberg-ID HTTPS UTF-8 TXT alternative and
  an explicit RDF byte extent between 40 kB and 3 MiB;
- exact normalized author/title duplicates, including aliases whose existing
  source ID appears later in the streamed catalog.

Author normalization strips lifespan dates, normalizes Unicode and punctuation,
and sorts explicitly separated coauthors. Title normalization uses NFKC,
casefolding and word tokens. This is a conservative duplicate-screening rule,
not an authority resolution claim. The selected representative has deterministic
size/ID priority; JSONL evidence records excluded and retained source IDs.

The complete scan found 55,267 eligible provisional groups from 79,811 records.
The original 5,049-entry budgeted prefix used language/subject round-robin
selection; 1,951 further smallest eligible candidates were appended without
changing prefix IDs or order. The final 7,000-entry queue has 6,959 no-image EPUBs
and 41 UTF-8 TXT sources. All entries explicitly carry
`illustrations_included=false` and
`coverage_scope=text_only_without_illustrations`. Images, facsimile layout and
visual equations are not claimed preserved. EPUB cache URL mappings retain the
explicit RDF URL and record SHA in source provenance.

| Language | Queued editions |
| --- | ---: |
| English | 2,557 |
| German, French, Spanish, Italian, Dutch, Finnish | 605 each |
| Portuguese | 500 |
| Swedish | 231 |
| Danish | 82 |
| Russian | 0 |

No additional Russian texts passed this already-used Gutenberg catalog screen;
the separately reviewed Wikisource acquisitions cover that gap. Subject labels
come from explicit catalog subjects: fiction 3,394, history 626, biography 190,
philosophy 131, science 129, economics 104, technical 1, other 2,425. The narrow
technical classifier undercounts implicit topics; `other` is not invented into
an engineering label.

## Disk budget and owned cleanup

The forecast is `6 × RDF UTF-8 TXT bytes + selected artifact bytes + 65,536` per
book, against measured free disk minus a 4.5 GiB planning reserve. The existing
live SQLite baseline occupied 3.171 bytes per canonical character; six bytes per
UTF-8 reference byte deliberately leaves additional room, but remains a forecast.
The 7,000-source queue projects 8,883,817,179 bytes added. Free space when sealing
it was 14,980,038,656 bytes; the planning budget was 10,148,200,448 bytes.
The runtime independently checks actual free disk before every book and stops
above the 4 GiB hard floor, also allowing the next transaction's working room.
Other concurrent work, including the local generation model, shares that disk.
Controlled downloader-cache files and archived originals use verified hardlinks
on this filesystem: all 458 checked new books shared an inode between those two
paths. Thus the artifact term counts their physical payload once here; a
cross-filesystem/copy deployment needs to budget both retained copies.

Only the two parent-authorized, project-owned benchmark database copies were
removed to make room: the span-only copy (940,126,208 B) and the FTS-maintenance
copy (1,599,156,224 B). Source/target receipt, sizes, metadata/text hashes and all
445,466 reconstructed spans were checked first. The frozen original's SHA was
checked before and after and did not change. Reports, query outputs and deletion
markers remain. APFS did not expose all logical deletion as immediately free
space; the cleanup receipt reports the actual immediate delta separately.
See `experiments/corpus-benchmark-cleanup-v1.json` and its runtime receipt for
exact checks. No original snapshot, semantic control/index or model was deleted.

## Active pipeline and safe resume

The run uses public `Library.import_book` for canonical-content duplicate
rejection, parsing, FTS insertion and citation hashes. Existing indexed IDs are
skipped before registration/import, so queue-only metadata does not reindex
covered books. A read-only proof checks all 4,001 baseline IDs and the protected
1,308 IDs have zero intersection with the queue; the first 150 entries each have
exactly one download/processed/indexed event, all before the resumed process.

One exclusive `data/full/.gutenberg-acquisition.lock` serializes this runner.
Provider requests wait two seconds after the preceding network operation;
verified cached artifacts do not make requests. Shared deferred-host state and
`Retry-After` are respected, and 403/429/server or transport failures stop the
host rather than trying an alternate endpoint. Atomic download sidecars verify
requested/final URLs, size and SHA. Progress is written atomically after every
book; interruption resumes with already indexed sources untouched.

```bash
# The already sealed queue is reused after SHA validation.
.venv/bin/python -u scripts/scale_corpus.py \
  --manifest data/catalog/scale10000-v2/queue.json --limit 7000 --run

# Read-only actual stage counts; writes only the optional JSON receipt.
.venv/bin/python scripts/corpus_scale_status.py \
  --manifest data/catalog/scale10000-v2/queue.json \
  --output data/catalog/scale10000-v2/status-latest.json
```

Do not start another runner while its recorded PID is alive. Current launch,
log, run state and queue/report are in `data/catalog/scale10000-v2/`; source
artifacts and library remain under ignored `data/full/`. The queued 7,000 sources
would take the baseline 3,997 groups above 10,000 if enough imports succeed and
remain distinct; this is not a claim that the target is already achieved.

The status command counts distinct downloaded/processed book IDs and current
indexed/duplicate/failed/deferred states from SQLite opened with `mode=ro`.
It confirms the actual PID/command, reports measured recent throughput, and
suppresses ETA when the process is stopped or deferred. A recent-rate ETA is
an extrapolation, not a completion promise. Public metadata measurements are in
`experiments/corpus-scale-v1.json`; current counts always come from the live
read-only status command.

Offline tests cover rights gates, same-ID/provider URLs, metadata dedup including
late existing aliases, no-image provenance, budget stops, immutable queue reuse,
and stopped/deferred status reporting. No whole-live evaluation snapshot is
claimed while this acquisition/index pipeline changes the corpus.

At 01:13 UTC on 2026-11-15, a read-only transaction snapshot independently checked
all 458 then-indexed queue books: 54,500,991 original artifact bytes matched their
stored SHA-256 hashes, and all 40,304 passages (45,866,615 characters including
chunk overlap) matched their section ownership, Unicode bounds, exact text slice
and SHA. No failures were found; the check took 4.211 seconds and wrote no
database. This verifies canonical citation integrity, not completeness against
every printed original. Three live API baseline searches on new DE/FR/EN books
also returned verified quotations with source/citation/reader links.
