# Performance measurements

These are observed local measurements, not target-scale capacity claims. Host: macOS ARM64, 16 GiB RAM, 10 CPU, Python 3.12.12. Other development and ingestion processes were active.

## Offline indexing

Reproduce after downloading the curated20 EPUB catalog:

```sh
uv run python scripts/benchmark_index.py --manifest corpus/manifest.json --downloads data/full/downloads --output experiments/local-index.json
```

The command creates a fresh isolated scratch SQLite index and removes only that scratch directory. It does not modify the live library or download books. It independently verifies every returned search quotation against section text.

| Run | Editions / passages | Source bytes | Index incl WAL | Index time | Process peak RSS | Lexical p50 / p95 |
| --- | --- | --- | --- | --- | --- | --- |
| `index20-v1.json` |20 /12,106|35,426,559|44,572,672|2.626s|132.70MiB|52.52 /126.99ms|
| `index20-v2.json` |20 /12,106|35,426,559|44,572,672|2.578s|105.19MiB|37.91 /81.45ms|

The later query verifies an exact SQLite section substring instead of returning the entire chapter for every candidate. These single-process samples are noisy and do not establish statistical speedup. Both checked120 returned evidence spans independently. Neither includes OCR, model inference or network time. Manifest digests differ because classification metadata was added; source artifact hashes and indexed counts match.

## Local embeddings

Initial demo183 token windows /45 passages,384 dimensions:6.549s build after weights cached. Initial model download plus first v1 build104.755s. Evaluation includes cold model load, so read p50/p95 and total time together; process peak approximately841MiB for semantic/hybrid evaluation. The model and all caches remain outside Git.

## Next scaling checks

Measure a frozen1,000-edition snapshot, repeat query loops, record sparse-map cold/warm times and vector build throughput. Current expanded corpus is changing during ingestion; live counts are not a stable benchmark. Exact vector search is linear in the number of windows; compare ANN recall/latency before any 10–50k-work capacity claim. Disk guard reserves4GiB; default hosting is local single-user.
