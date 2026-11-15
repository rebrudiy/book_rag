# Compact semantic expansion: CPU IVF-PQ experiment

This is an optional storage/candidate-recall experiment. It does not replace
`semantic.py`, activate an index, change embeddings, tune retrieval thresholds,
or evaluate relevance. The existing 1308-edition float32 generations remain
unchanged. No original SQLite database, model, or final-evaluation labels are
opened by the experiment.

## Dependency and process boundary

The probe uses `faiss-cpu==1.16.0`, released 2026-11-10, under the
[upstream MIT license](https://github.com/facebookresearch/faiss/blob/v1.16.0/LICENSE).
[The release](https://github.com/facebookresearch/faiss/releases/tag/v1.16.0)
and [maintainer PyPI metadata](https://pypi.org/project/faiss-cpu/1.16.0/)
identify Meta/Facebook Research. Upstream's supported installation guide
principally documents Conda; this experiment uses the small published PyPI
wheel, not a source build or an additional package manager.

Optional local install, without changing project dependencies/lock:

```sh
.bootstrap/bin/uv pip install --python .venv/bin/python --no-deps --no-cache 'faiss-cpu==1.16.0'
```

The measured macOS arm64 wheel is
`faiss_cpu-1.16.0-cp310-abi3-macosx_15_0_arm64.whl`, 5,251,785 bytes,
SHA-256 `fe430200c5b91eff67ebb9fe3958963d3ae7dfb341e3af7c1102ecf6ade4378e`.
Its exact URL is provided by
`https://pypi.org/pypi/faiss-cpu/1.16.0/json`. Other platforms need their own
compatible wheel and checksum. No transitive dependencies were changed;
the local probe uses the existing NumPy 2.5.4. A normal `uv sync` may remove this
unregistered experimental extra; production and ordinary CI do not require it.

**Run native FAISS in its own CPU subprocess.** Loading this wheel into the
pytest process before Torch caused a native abort consistent with incompatible
OpenMP runtimes. Tests now execute native FAISS only in a child; the parent
remains able to import Torch afterwards. The lazy backend rejects calls unless
the explicit worker opt-in is set, and rejects a process that already imported
Torch. This opt-in is an architectural guard, not proof of process isolation.
Do not import FAISS into the ASGI, embedding, generation, or pytest process;
do not set `KMP_DUPLICATE_LIB_OK` to hide the conflict. A production candidate
would require a dedicated bounded CPU worker with a small structured IPC API.

## Reproduce

```sh
.venv/bin/pytest -q tests/test_experimental_ivfpq.py
.venv/bin/python scripts/benchmark_ivfpq.py --self-test \
  --output data/bench/ivfpq-synthetic-new
.venv/bin/python scripts/benchmark_ivfpq.py \
  --source-vectors data/snapshots/library1308-v1/vectors \
  --output data/bench/ivfpq-frozen1308-new --rows 65536
```

Use a new output directory. If an owned run is
interrupted before `report.json` exists, repeat with `--resume`; sealed shards
and the trained codec are verified and reused. Completed reports are not
overwritten. The `--worker` flag is an internal implementation detail; launch
the normal parent so RSS/time/free-space limits remain monitored.

The parent enforces a 900-second wall deadline, watches child RSS every 0.2 seconds,
and kills its owned child above 1 GiB RSS or below 4.5 GiB free disk. Sampling and
shards are bounded so output remains below 300 MiB. RSS monitoring is sampled,
not an operating-system memory reservation; a transient allocation can outrun
the poll, so the worker also checks its recorded peak before accepting a report.
Do not run concurrently with another large CPU benchmark if interpreting timing.

The source is opened as read-only NumPy metadata and bounded `pread` calls:
no complete matrix is copied. Streamed full SHA-256 and file identities for
active metadata, generation metadata/checkpoint, embeddings, and passage IDs
must match before/after. Mapped row gathering was replaced because resident
file pages can inflate peak process RSS. The SQLite file is never opened or
migrated. Source reads can warm the operating-system cache.

## What is measured

The frozen source has 1,865,985 windows, 384 float32 dimensions and 445,466 passages.
The probe samples 65,536 distinct window rows uniformly without replacement
with seed 20261115, then chooses 64 query rows spread across the sorted sample
with distinct passage IDs. Query vectors are stored document vectors. They are
not independent natural-language query embeddings and do not establish RAG
quality, multilingual quality, or the validity of an answer.

All windows whose passage ID equals the query passage are excluded from both
neighbor results and the 32,768-vector codebook training sample. Other nearby
passages, related editions, and translations may remain. The exact reference
uses float32 inner products of the existing unit-normalized vectors, scans in
4096-row blocks, orders ties by sample window ID, and keeps unique passages.
The reference corpus is this 65,536-window sample, not all 1308 editions.

The candidate uses a shared trained `IndexIVFPQ(IndexFlatIP,384,128,64,8,IP)`:
128 coarse centroids, 64 subquantizers, 8 bits/code, 12 clustering iterations,
seed 20261115, two CPU threads. Coarse clustering is spherical; large optional
precomputed tables are disabled. Eight 8192-row shards use that same codec.
Each receives unique sample window IDs. Search loads one verified shard at a
time, merges up to 4096 window hits per query, then excludes the query passage
and keeps up to 1000 distinct passages. `nprobe` is swept through 1,8,32,128;
the last setting visits every coarse list and isolates remaining PQ/ranking
error from coarse-list misses.

Metrics report exact top-10/top-100 overlap with ANN top-10/top-100, plus whether
the exact top-10 fits inside 100 ANN candidates and top-100 inside 1000 candidates.
Approximate-score absolute error is measured against original float32 scores
for returned windows. None of these scores is a probability. ANN batch timing
includes shard IO, integrity checking, native search and window merge; exact
timing is sequential per query. These workloads do not yield a fair online
speedup ratio. Peak RSS includes imports, sampling, training, integrity checking
and search; it is not production online RAM use.

The earlier v1 run is retained under `data/bench/ivfpq-frozen1308-sample-v1`.
It passed source-byte guards after a bounded-read rerun but selected queries
only from the beginning of a larger fallback sequence. Its query coverage is
biased and its resumed phase timings are not training timings. Use the v2
spread-query run for the final aggregate; no prior report is overwritten.

## Measured v2 result

The accepted v2 report is
`data/bench/ivfpq-frozen1308-sample-v2/report.json`, SHA-256
`65441a1c96cac5305b0e422794b961bc5ccb049d2d1ad1c9c892d63bd490a335`.
It includes source and implementation hashes.

The measured implementation is retained in Git commit `c47a674`. A subsequent
loader-only fix ignores legitimate access-time updates while hashing a cold
index file. It still checks device, inode, size, modification/change timestamps
before/after checksum verification and native loading. Regression tests cover
cold reads and actual file replacement/modification races. The published v2
measurements and hashes are unchanged; use the historical commit for exact
source reproduction of that measurement.

Results are means over 64 stored queries on 65,536 sampled windows representing
61,639 distinct passage IDs:

| nprobe | Exact top-10 in ANN top-10 | Exact top-10 in ANN top-100 | Exact top-100 in ANN top-1000 |
| ---: | ---: | ---: | ---: |
| 1 | 0.4531 | 0.5828 | 0.5006 |
| 8 | 0.6578 | 0.9109 | 0.8455 |
| 32 | 0.6797 | 0.9813 | 0.9741 |
| 128, all lists | 0.6813 | 0.9953 | 0.9989 |

At nprobe 32, returned-window score absolute error has mean 0.0201 and p95
0.0579. Good candidate inclusion does not preserve the exact ordering or an
existing cosine abstention floor. This is a promising candidate generator,
with no production promotion or relevance-quality conclusion.

The worker peaked at 451,952,640 bytes (431.0 MiB). Owned output was 11,092,604
bytes before the final JSON report; the optional installed package occupies
18,672,615 bytes. The eight standalone shards occupy 9,446,816 bytes, including
repeated coarse/PQ codebooks; the source float32 sample would occupy 100,663,296
bytes. No sample float32 matrix is persisted.

Training plus the first shard took 1.234 s. The run stopped after publishing
that shard, then reused the codec and first shard to create the other seven in
1.465 s. This demonstrates sample-level interruption/resumption; it is not an
embedding-throughput measurement or a power-loss test. Parent tests also check
corrupted sealed indexes fail without overwriting them and incomplete owned
unsealed publications can be rebuilt.

All before/after full SHA values and file identities matched. The original
`embeddings.npy` SHA is
`92239c409e85432d6da95a54a2b9da4fc750433d8c45b0f6bceade7d80b3707e`;
`ids.npy` SHA is
`f95cf7c638bc8f6c76ae219b07a866e0fcef005af812727f7702b68d283dda72`.
Free space at completion was 7,577,501,696 bytes (7.06 GiB), with corpus ingestion
continuing independently. Current free space must be checked again before new
embedding work; this number is an observation, not a remaining-space promise.

## Capacity model

[Upstream's index table](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)
gives PQ code bytes plus 8-byte IDs for IVF-PQ. PQ64x8 at 384 dimensions uses
64 code bytes, not 64 float components. This project additionally needs an 8-byte
window-to-passage mapping when FAISS IDs identify individual source windows.
That totals 80 bytes/window before codebooks, manifests, attribution/coverage
proofs, array headers, temporary chunks and allocator overhead. Duplicate
windows still consume rows; no new deduplication claim is made.

At the measured 1308 mean window density, 11,000 editions would have approximately
15,692,534 windows. This is a linear planning assumption, not an actual measured
11,000-edition window count; the expanding books differ in size and language.

| Storage, including passage mapping where applicable | Projected 11k GiB |
| --- | ---: |
| Float32 matrix only | 22.45 |
| Float16 plus 8-byte passage IDs | 11.34 |
| Per-row int8 plus float32 scale and passage IDs | 5.79 |
| PQ64 plus 8-byte window IDs and 8-byte passage mapping | 1.17 |

These are storage calculations, not equal-accuracy alternatives. Scalar int8
still requires a large scan and its native scores need separate validation.
Keeping a full newly encoded float32 matrix before compression would defeat
the disk saving. The current original float32 controls remain on disk.

At the same density, 50,000 editions would require about 5.31 GiB for PQ+IDs/mapping
alone. That does not mean the full product fits the current machine: text,
SQLite/FTS, original artifacts, models, proof metadata, codebooks and temporary
work remain substantial. Recompute actual window counts and free disk before
each increment; preserve 4.5 GiB free. Ongoing corpus ingestion shares that budget.

## Concrete production design to validate next

1. Preserve the current exact 1308 control. Define a new model-space descriptor
   including model revision/tokenizer, query/document prefixes, dimensions,
   normalization, window/header strategy and a codec hash. Never mix E5 and
   MiniLM vectors or independently trained PQ codebooks into comparable scores.
   Train one fixed codec on a versioned, language/book-balanced reservoir.
   The 128-centroid sample setting is not a selected full-corpus parameter.
2. Stream the existing exact window text through the unchanged encoder in
   bounded batches. Immediately persist small immutable standalone-code chunks
   with explicit window IDs, passage IDs, exact input-key hash chain and source
   provenance; checkpoint only after flush/fsync+atomic receipt publication.
   [FAISS's standalone-code API](https://faiss.ai/cpp_api/struct/structfaiss_1_1IndexIVF.html)
   exposes `sa_encode`, `sa_code_size` and `add_sa_codes`, allowing a resumed
   shard to consume checked codes without re-embedding. Include coarse-list
   bytes in temporary-code budgets. Do not retain a complete new float32 matrix.
3. Assemble 250k-window immutable shards from sealed chunks with the same codec.
   Retain the chunks until shard bytes, row counts/order/IDs and all input/source
   proofs are verified and the completion manifest is durable. Then remove only
   the owned redundant chunks. A crash may repeat assembly but not encoding.
   Atomic manifest publication is the only ready/active transition; never serve
   staging. Keep per-shard temporary working space bounded and fsync directories
   for production power-loss durability. Codec copies in each shard add overhead.
4. Reuse the existing strict canonical/source proof and per-book epoch binding
   protocol for each ready shard. A covered book/header/model-input change
   invalidates its shard; a new book outside coverage does not bless or invalidate
   unrelated rows. Verify guards before and after queries, plus canonical evidence
   before returning citations. The experiment's hashes are not a live coverage
   receipt and cannot substitute for this admission protocol.
5. Keep full-library lexical retrieval available. Advertise the actual eligible
   semantic subset, model space, codec and stale/unready state. A scoped book or
   language outside valid coverage explicitly falls back to lexical search.
   Scoped filters must operate on eligible IDs and be tested for candidate
   starvation; global post-filtering of a small ANN pool is insufficient.
6. Merge a fixed bounded candidate pool independent of the requested result
   count, aggregate windows to unique passages, and validate source evidence in
   batches. Quantized similarities cannot silently inherit the old exact cosine
   floor. Evaluate candidate recall, score/abstention calibration and natural
   query relevance on development data before any production admission.
   Exact rescoring would need a bounded exact-vector cache or checked re-encoding,
   not a hidden full float32 replica. No final set is involved in this work.

[Upstream recommends training a shared empty IVF index then adding shards](https://github.com/facebookresearch/faiss/wiki/Indexes-that-do-not-fit-in-RAM).
Its on-disk inverted lists and `IO_FLAG_MMAP` are a future option after
measurement; adding directly to on-disk lists is documented as inefficient.
Immutable shard manifests are simpler for incremental admission and rollback.

The new module implements only sample codec training, immutable shard receipts,
interruption/resumption without retraining sealed shards, corruption rejection,
and bounded sequential shard search. It deliberately has no production installer,
embedding worker, SQLite bindings, arbitrary-input loader or concurrent-writer
support. Its private output directory requires a single writer. SHA verification
is required before reloading self-produced native indexes; it is not authenticity
for an attacker-supplied receipt. [FAISS warns that untrusted index files can be unsafe](https://github.com/facebookresearch/faiss/wiki/Index-IO,-cloning-and-hyper-parameter-tuning).
