# Selected-file bulk acquisition

The official Gutenberg [mirroring instructions](https://www.gutenberg.org/help/mirroring.html)
recommend rsync for private copies and document both generated-collection
origins used by the allowlist. The [robot policy](https://www.gutenberg.org/policy/robot_access.html)
separately points bulk acquisition to this route. This importer requests only
explicitly selected no-image EPUB files, not a complete mirror or website crawl.
Rights remain the per-edition RDF statement `Public domain in the USA`; neither
bulk transport nor the project's MIT license grants worldwide text rights.

`bulk_sources.prepare_bulk_plan` combines the existing screened queue with
actual remote selected-file metadata. The current input is
`data/transport-research/pg-generated-metadata-v2.json`, captured using the
allowed `rsync --list-only --files-from` operation. Its retained 688,888-byte raw
listing has SHA `b860d8d6de7d27948bedb1e437bafef4a9e7c889763509f3a5b3afe5c01de9b0`.
All 6,107 path/size/UTC-mtime rows are independently reconstructed from that raw
capture before a plan is sealed. This is not labelled as an HTTPS CSV request.

The initial measured origin was `rsync://gutenberg.pglaf.org/gutenberg-epub/`.
The active faster run uses `rsync://rsync.ibiblio.org/gutenberg-epub/`, separately
captured metadata, immutable plans and origin-specific cache receipts. Generated
paths are exactly `ID/pgID.epub`; only the same Gutenberg ID, approved HTTPS
catalog/download locators, explicit no-image variant and positive bounded file
extent are accepted. Existing IDs of every status are excluded during planning.
The original RDF record/catalog SHA, rights, provisional work ID and text-only
coverage are retained. Bulk transport does not resolve work identities.

## Coordinated operation

```bash
# Planning/probe for the active mirror; no book payload is fetched.
.venv/bin/python scripts/run_bulk_sources.py \
  --metadata data/transport-research/ibiblio-remaining-v1.json \
  --plan data/catalog/bulk-ibiblio-v1/plan.json --probe

# After the ordinary runner has stopped and released the shared lock:
.venv/bin/python -u scripts/run_bulk_sources.py \
  --metadata data/transport-research/ibiblio-fresh20-v1.json \
  --plan data/catalog/bulk-ibiblio-v1/pilot-plan.json \
  --limit 20 --run --max-books 20 --batch-size 20

# Resume/promote the same immutable plan, retaining the pilot and cache:
.venv/bin/python -u scripts/run_bulk_sources.py \
  --metadata data/transport-research/ibiblio-remaining-v1.json \
  --plan data/catalog/bulk-ibiblio-v1/plan.json --run --batch-size 128

# Actual process/lock, staged bytes and database stage counts:
.venv/bin/python scripts/bulk_corpus_status.py \
  --plan data/catalog/bulk-ibiblio-v1/plan.json \
  --output data/catalog/bulk-ibiblio-v1/status-latest.json
.venv/bin/python scripts/summarize_bulk_batches.py
```

The bulk runner takes exactly `data/full/.gutenberg-acquisition.lock`, also held
by `scripts/scale_corpus.py`. It refuses a competing acquirer **before** creating
`Library` or transferring payload. Generic low-level HTTP download helpers do
not provide this lock; other acquisition tools must coordinate explicitly.
Changing the original queue JSON cannot change the already running serial
process's in-memory queue. The handoff therefore stops that process first and
uses a separate bulk-aware consumer; the original queue and receipts remain.

A matching metadata-only reachability receipt is required before payload.
Under the lock, indexed IDs are skipped before any registration/import. Other
already registered IDs are left alone unless they have this plan's own recorded
previous import attempt, allowing recovery without modifying an unrelated
source. Current IDs are checked again before each batch and import. All source
processing uses public `Library.import_book`, including canonical duplicate
rejection, chapter parsing, FTS insertion and citation hashes.

Rsync receives into a separate staging tree. Only distinct validated literal
file paths enter `--files-from`; there is no whole-tree selection, `--delete`,
`--inplace` or `--append`. The inactivity timeout is 30 seconds and the whole
batch deadline is bounded at 1,800 seconds, allowing the actually measured slow
transport to complete a larger batch. Progress goes to `rsync-transfer.log`.
An unsuccessful transfer keeps partial staging, defers the provider for an hour
and ends the run; it never silently switches endpoints or retries early.

Every completed source is checked against exact selected size and remote mtime,
hashed, and atomically renamed into `data/full/downloads/bulk/ORIGIN_HASH/`.
Its separate `.bulk-source.json` records actual rsync origin/path, metadata
capture SHA, original catalog locator, byte SHA/size and acquisition window.
The timestamp is the first successful receiver batch completion; per-file
socket completion timing is not asserted. Cache reuse preserves this original
timestamp and rechecks the receipt and file hash. No `.source.json` is invented
for an HTTP request that never happened. Rsync transfer verification and local
SHA do not constitute an authoritative upstream SHA or authenticated transport.

The derived local manifest preserves all previous entries on resume. Use the
bulk runner to consume it: the generic local-path helper alone does not verify
bulk receipts. Downloads remain inside the controlled cache so immutable archive
publication can hardlink them. Fresh atomic replacement preserves any existing
archived hardlink; it never writes through that shared inode.

## Measured pilot and limits

On 2026-11-15 the pilot actually downloaded, processed and indexed 20 books in
51.158 seconds: 48.892 seconds transfer, 1.951 seconds import. Its exact source
payload was 2,455,988 bytes; failures and canonical duplicates were zero. This
small two-connection sample is not a full-run throughput or ETA benchmark.
The first 78-book full-run batch became searchable in 265.341 seconds: its
11,153,271 source bytes took a 258.716-second transfer wall interval, followed
by 6.625 seconds for source verification/publication and indexing. That is
17.64 books/minute on different books; no acceleration over the earlier serial
sample is established. All 8,614 new passages and 9,962,110 canonical characters
passed the independent source/archive/span audit. See
`data/catalog/bulk-generated-v1/first-full-batch-audit.json`.

The independent read-only audit checked all 20 original/archive/API-download
hashes, all same-inode cache/archive pairs and all 1,587 passage text slices,
book ownership, bounds and hashes. All 4,979 pre-existing book rows and saved
notes were unchanged. Real Chromium readers for new Portuguese, French and
Swedish books displayed exact canonical text, headings and no-image notices,
with no JavaScript errors. Screenshots and detailed receipts remain in ignored
`data/catalog/bulk-generated-v1/`; `pilot.run-state.json` and
`pilot.local-manifest.json` are preserved before the full run.

The original pglaf plan has 6,014 candidate editions, 894,083,711 source bytes and a
7,849,490,486-byte conservative source/index forecast. Free disk at planning was
13,872,238,592 bytes. Planning keeps 4.5 GiB reserve; each batch requires that
reserve plus its forecast and working payload, and each import checks the 4 GiB
hard floor with transaction headroom. This is a forecast; actual disk checks can
stop the run safely. The 38 remaining TXT records stay in the original queue for
later serial fallback, after the bulk process has exited and released its lock.
Do not change formats or raise the artifact cap to make them look acquired.

The status command reports local staged file sizes, including partial files,
separately from downloaded/processed/indexed database stages. Staging sizes are
not called verified acquisitions or network byte counts. It reports the actual
PID and lock and leaves ETA unknown until multiple full batches are measured.
Twenty-five offline tests cover provenance, rights, raw metadata verification, unsafe
paths, shared-lock refusal, failed/partial transfer, disk reserve, exact source
checks and real temporary-library resume with protected books/notes. No frozen
semantic coverage book or original control database is rewritten.
An actual local sleeping child executable additionally verifies that SIGINT
stops and reaps the transport child before the provider lock is released. This
supports a coordinated mirror handoff without leaving an orphan acquirer.

## Ibiblio acquisition and actual completed-batch measurements

The separately retained ibiblio listing has 5,886 remaining paths and SHA
`3a7617c1e3d347fd32a4e9888245ecd6c06f7827891e7ad42f9cebf48ddd59e6` over
663,948 raw bytes. Sixty-one files differ in size or mtime from pglaf; none are
silently treated as the same bytes. The full ibiblio plan records 875,901,264
source bytes and a 7,701,053,411-byte source/index forecast. Its pilot selected
twenty unregistered books outside the then-active pglaf batch. The prior
acquirer and child were stopped and its lock released before this payload began.

This actual pilot downloaded/processed/indexed all twenty books in 4.983 s
(3.092 s transfer, 1.766 s import), with 3,355,438 source bytes and zero failures.
All 2,507 passage slices, source/archive/API-original hashes and same-inode pairs
passed. All 5,077 pre-existing book rows and saved notes were preserved. Real
Chromium readers for Swedish, German and English sources preserved exact DOM
text/headings and showed the no-image notice without JavaScript errors.

The full run began at 01:55:10 UTC on 2026-11-15. At 02:02:12 UTC, twenty-one
completed receiver batches accounted for 2,668 new searchable editions and
467,989,625 source bytes, over 416.134–417.134 seconds end to search: approximately
383.76–384.68 editions/minute. Current transfer/index phases and the earlier
twenty-book pilot are excluded from this denominator. This establishes measured
progress on those books; it does not guarantee the same rate for later books.

`summarize_bulk_batches.py` reconstructs these groups from exact received-source
receipts, preserved input lists and read-only source-pipeline timestamps. SQLite
timestamps have one-second resolution, so reported intervals are bounds, not a
falsely exact commit stopwatch. An input rewritten after receipt cannot support
its original timing and is excluded. Raw timestamps and per-batch counts remain
in `data/catalog/bulk-ibiblio-v1/batch-measurements.json`. Pilot, old pglaf and
new ibiblio receipts all remain separate. Public metadata summaries are
`experiments/bulk-sources-v1.json` and `experiments/bulk-sources-v2.json`.

## Completed expansion and final independent audit

The full ibiblio queue completed at 02:11:46 UTC on 2026-11-15: 5,866 newly
indexed editions plus its twenty-book pilot, with zero failures/duplicates.
The full run took 995.628 seconds (459.039 transfer, 481.003 import). After its
process and rsync child exited, its lock was verified free and the provider had
no pending cooldown. The original serial queue then resumed only its remaining
38 TXT files and one previously registered EPUB. Those 39 were indexed in
108.24 seconds; all 6,961 already indexed IDs were skipped without refreshing
their text. The original queue bytes and earlier process receipts remain.

Final library totals are **11,001 searchable editions and 10,997 provisional
candidate workgroups**, a 7,000-edition increase. Current new-source statuses
are exactly 7,000 indexed, zero failed/deferred/duplicate/unregistered. All
7,000 had passed download and processing; those are cumulative stages, not
four separate current populations. No acquirer remains running for this queue.

A separate read-only audit verified every new source/archive/cache hash and
same-inode pair, all 7,000 canonical book hashes, and all 891,794 exact passage
ownership/bounds/slices/hashes in 181,321 sections. These new texts contain
1,023,911,388 canonical characters; their original payload is 1,020,126,599 bytes.
All metadata still matches the immutable queue and retains no-image flags.
None of the protected 1,308 books occurs in the queue or has a new index event
during expansion; the original frozen database SHA is unchanged. The audit took
46.288 seconds and wrote no database or model state. This establishes mechanical
source/citation integrity, not literary work identity or printed completeness.

Runtime evidence is `data/catalog/scale10000-v2/final-integrity-audit.json`;
the small public final summary is `experiments/corpus-scale-final-v1.json`.
Authority proposals and compound-work review are the next step; additional
book payloads are not queued merely to inflate a confirmed-identity count.
