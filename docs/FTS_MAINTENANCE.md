# FTS maintenance experiment

An owned copy of the frozen 20-edition control shrank from **252,600,320 bytes
to 44,527,616 bytes** after FTS5 `optimize` and SQLite `VACUUM` (82.37%).
The control itself remained byte-identical. The 12,108 passage IDs and hashes,
eight probe rankings and their BM25 scores remained identical. These are storage
and structural checks, not a new relevance benchmark.

The [report](../experiments/fts-maintenance-v1.json) separates ordinary
maintenance from an explicit contentless-index reconstruction. Reconstruction
plus vacuum produced 44,978,176 bytes with the same probe results; it did not
outperform ordinary maintenance on space. Passage and section text were retained.
The old index had 12,108 document-size records, matching its 12,108 live passages:
the large file alone was not evidence of extra searchable books or broken quotes.

This is a small control that had been reindexed. It does not establish equivalent
savings for the expanding live library, and no production database was changed.
The experiment avoids rewrites of frozen controls and rejects a nonempty source
WAL. It checks the source digest and file identity before and after copying.

The [larger check](../experiments/fts-maintenance1308-v1.json) used the frozen
1,308-edition / 445,466-passage snapshot. Its already maintained database stayed
at **1,594,347,520 bytes** after optimize and vacuum: **no space saving**. Explicit
reconstruction produced a slightly larger file. Probe rankings, scores and
passage fingerprints again matched. Maintenance is useful when there is space
to reclaim; the small control's 82.37% reduction must not be projected onto a
fresh snapshot or the whole live library.

```sh
uv run python scripts/benchmark_fts_maintenance.py \
  --source data/snapshots/curated20-v3/library.sqlite3 \
  --output-dir data/benchmarks/fts-maintenance-local
```

Use a new destination outside the source library. The experiment requires free
space for its owned copy and vacuum working files above a 4 GiB reserve. Large
copies stay in ignored `data/`; only the compact report is suitable for Git.

Before adding live maintenance, measure a larger coherent copy, verify all
passage IDs/hashes and source slices, and coordinate with ingestion so ordinary
new books are not mistaken for maintenance-induced changes. Keep source texts,
saved notes, canonical historical versions and active vector generations intact.

SQLite documents the two separate operations: [FTS5 optimize](https://www.sqlite.org/fts5.html#the_optimize_command)
merges its index, while [VACUUM](https://www.sqlite.org/lang_vacuum.html) repacks the
database file and can require substantial temporary disk space. The experiment
measures their combined effect rather than attributing all savings to one step.
