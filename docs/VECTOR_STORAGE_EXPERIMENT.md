# Vector storage approximation v1

The measured candidate-vector component can be substantially smaller: float16
halved its file bytes and row-wise int8 plus explicit scales reduced them by
74.35%. This NumPy implementation also reduced process peak RSS, while decoding
increased query latency. Production encoding, indexes, retrieval defaults and
thresholds remain unchanged.

The checked-in [result](../experiments/vector-storage-v1.json) records the frozen
20-edition MiniLM generation: 49,738 windows, 12,108 distinct passages and 384
dimensions. The source arrays and `active.json` were read directly with read-only
memory maps; their SHA-256 hashes matched before and after. No Library, SQLite,
model encoder or MPS operation was used.

## Results

256 deterministic corpus-vector queries, seed `20261114`; file/RSS sizes below
are decimal MB (1,000,000 bytes). Each query's entire source passage, including
all overlapping windows, was excluded. Neighbors are unique passage IDs scored
by their maximum window cosine.

| Encoding | Index files MB | Peak query-process RSS MB | Warm p50 / p95 ms | Recall@10 | Recall@100 | Max absolute cosine error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| float32 reference | 76.80 | 122.21 | 3.76 / 4.97 | 100% | 100% | 0 |
| float16 | 38.60 | 90.62 | 6.94 / 11.59 | 99.9219% | 99.9961% | 0.00005782 |
| symmetric int8 + row scales | 19.70 | 72.58 | 25.20 / 30.12 | 99.2578% | 99.5156% | 0.00257763 |

The file totals include the unchanged int64 passage-ID array and NPY headers;
int8 also includes one float32 scale per row. The experimental directory retains
additional encodings and reference scores, so creating the experiment itself
adds disk usage. The table compares the logical files needed by one encoding.

Peak RSS includes interpreter/NumPy startup, resident mapped pages, row norms,
scores and bounded decode buffers. The measured import baseline was about 35 MB.
Against the float32 process, total peak RSS fell by about 25.8% for float16 and
40.6% for int8. Accuracy workers additionally map reference scores; their RSS is
reported separately and is excluded from the query-memory comparison.

This run used NumPy 2.5.4 on macOS 26.6.2 arm64, one BLAS thread, a shared host
and warmed filesystem mappings. Latency includes decoding, float32 cosine,
maximum-score passage aggregation and deterministic top100 sorting. It excludes
model encoding, text retrieval, source verification, filtering, hybrid fusion
and HTTP. No cold-cache I/O measurement or ANN traversal was performed. Shared
host load makes these timings descriptive rather than a backend guarantee.

## Math and query construction

The query sample is uniform over distinct passage IDs. The first source window
of each sampled passage supplies its unquantized float32 query; the same unit
queries are used for all encodings. Query order is sorted by source row after
sampling. Every candidate window with that passage ID is removed before ranking.
Other nearby passages, translations and duplicate editions can remain.

Float32 reference rows are divided by their measured norms rather than assuming
perfect unit length. The maximum original norm deviation was 1.19e-7. Independent
float64 cosine sanity pairs differed by at most 1.02e-7. Float16 rows are decoded
to float32 and normalized using their decoded norms.

Int8 uses `scale=max(abs(row))/127`, `rint(row/scale)`, clipping to `[-127,127]`,
and explicit float32 scales. An all-zero row gets scale 1 and is excluded from
cosine candidates. Decoding and dot products use float32, avoiding int8 overflow;
decoded norms are used for cosine. Positive row scales mathematically cancel in
cosine, but the implementation reconstructs the stored vectors explicitly.
No zero source rows occurred in the real generation.

Recall@K is the fraction of float32 top-K unique passage IDs retained in the
compressed top-K, with denominator capped by available distinct passages.
Scores sort descending, then passage ID ascending for deterministic ties.
The report also includes recall allowing 1e-7 boundary ties; no reference ties
occurred in this sample. Cosine errors cover 12,731,880 eligible query/window
pairs, rather than just returned neighbors. Self-tests cover quantization bounds,
zero vectors, read-only source arrays, tie ordering and same-passage exclusion.

These corpus-derived queries measure retrieval approximation. They do not measure
natural-query relevance, cross-language usefulness, answer abstention or citation
correctness. The existing development/held-out relevance sets were not read.

## Reproduction

Use the existing project environment; no additional dependencies are required.
The output directory must be new and outside the source library. Each encoding's
memory/latency worker runs sequentially in a fresh subprocess; a separate worker
performs accuracy comparisons. Blocks of 4,096 rows bound float32 decoding memory.

```sh
uv run python scripts/benchmark_vector_storage.py --self-test
uv run python scripts/benchmark_vector_storage.py \
  --source-vectors data/snapshots/curated20-v3/vectors \
  --output-dir data/benchmarks/vector-storage-local \
  --queries 256 --seed 20261114 --block-rows 4096
```

The repeat report defaults to the new output directory's `report.json`; existing
directories and reports are rejected. The published run additionally supplied
`--report experiments/vector-storage-v1.json`. Large arrays, query rows, reference
scores and job details stay under ignored `data/benchmarks/`.

## Capacity implication and next experiment

Compression helps the vector-storage component. The measured int8 representation
uses 25.65% of the float32 file bytes, including IDs and scales. It does not reduce
the number of windows, full-scan complexity, source/SQLite storage or the cost of
encoding books. The slower int8 decode path would need another implementation
before adoption. These 20 editions establish no capacity or relevance guarantee
for 10,000–50,000 unique works.

A separate candidate benchmark could evaluate Faiss scalar-quantized HNSW or IVF
against its exact Flat baseline. Faiss documents scalar quantization, HNSW graph
memory/search controls and IVF probes; its quantizer is a separate implementation
from this row-symmetric experiment. Measure its actual recall, RAM and latency
before choosing parameters. [Official Faiss index documentation](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)

Cosine search through inner product requires normalized database and query
vectors. Preserve that contract and recheck any score thresholds when testing a
compressed backend. [Official Faiss metric documentation](https://github.com/facebookresearch/faiss/wiki/MetricType-and-distances)
