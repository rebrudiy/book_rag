# Owned RU21 compressed semantic pilot

This is an experimental end-to-end passage index for **21 Russian editions**, not
semantic coverage of the full library. It adds no production search route, active
pointer, or default ranking change. The three relevance checks come from the
public `rag-dev-v1` fixture; no sealed evaluation cases were read or run.

The pilot uses actual canonical text, production MiniLM token windows and real
offline embeddings. This differs from the earlier incremental mechanics probe,
whose external vectors had deliberately synthetic CC0 passage assignments.

## Source and model identity

`semantic_pilot.ReadOnlyLibrary` never calls `Library.__init__` on the original.
Its SQLite connections use `mode=ro` and `query_only`. A single read transaction
copies only selected books, metadata, index-version records, sections, section
attribution, and passages into a newly owned database. Original passage and
section IDs survive. Private saved items, model weights, original artifacts, and
source vector matrices are not copied. Only the owned database initializes FTS
and epoch tracking. An explicit indexed/Russian selection check rejects missing,
nonindexed, or differently labeled editions.

The source receipt hashes canonical text, attribution, provenance and indexer
metadata. Canonical coverage also verifies each passage against its exact
section slice. Later source changes fail verification; unrelated editions may
continue ingestion. This is a selected-edition logical proof, not a claim that a
growing original database has an unchanged whole-file SHA. The owned snapshot
does have a full database SHA, checked before reuse.

The model is cached
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, normalized float32, 384 dimensions,
empty document/query prefixes, and the unchanged production `token-windows-v2`
recipe. The model's sequence limit is 128 tokens. Receipts include model-file
SHAs, profile, revision, actual production-window function SHA, exact encoder
inputs, original passage IDs, and per-book input counts. This prevents silently
mixing an E5 profile, a changed header/window recipe, or another model space.

The shared IVF-PQ codebook comes from the frozen1308 sample experiment in
[SCALABLE_SEMANTIC.md](SCALABLE_SEMANTIC.md). Its model/profile identity and SHA
are checked before use; original codebook bytes are rechecked after the pilot.
The pilot uses 64-byte PQ codes, 128 coarse lists and `nprobe=32`. It does not
retrain the codebook on the public queries.

## Process isolation and interruption

The coordinator imports neither Torch nor native FAISS. One spawned Torch-only
worker encodes batches of at most32 strings; a separate spawned FAISS-only
worker consumes the vectors. Only NumPy arrays and plain JSON metadata cross
their trusted local pipes. In particular framework string subclasses such as
`torch.torch_version.TorchVersion` are stripped before pickling: unpickling that
class would otherwise import Torch into the FAISS worker. Co-loading the native
libraries has previously aborted a process, so worker isolation is required.

The encoder is offline, uses existing pinned weights and finite IPC deadlines,
and rechecks weight SHAs at shutdown. The coordinator monitors combined worker
RSS, owned output size and free disk. This run used a 2GiB combined worker RSS
limit, 250MiB owned disk limit and 4.5GiB free-disk reserve. No paid API is used.

Window manifests are replayable SQLite files with atomically published,
non-overwriting receipts. Replay checks the full file before and after, dense
window IDs, each exact input SHA and the complete input hash chain. An unsealed
owned manifest can be rebuilt; changed sealed inputs are rejected. No embedding
matrix is persisted. The compressed builder retains durable chunks/checkpoints,
assembles immutable shards, and epoch-binds its complete source coverage before
publishing `ready.json`. It never installs a production pointer.

The measured run deliberately stopped after512 durable windows, then resumed the
remaining5650. Durable windows were not encoded again. A crash before a chunk's
checkpoint may require recomputing that uncommitted chunk; this is not an
exactly-once guarantee across arbitrary process failures. Existing incremental
tests also cover shard publication failures, source changes, corruption and
prefix-stable querying. Additional manifest/IPC hardening was tested after the
measured run without changing its inputs or rewriting the original report.

## Measured result

The compact metadata report is
[`experiments/ru21-semantic-pilot-v1.json`](../experiments/ru21-semantic-pilot-v1.json).
Raw local reports and source/vector data stay outside Git.

| Item | Actual run |
| --- | ---: |
| Canonical Russian editions / sections / passages | 21 / 252 / 1,282 |
| Owned canonical snapshot | 9,498,624 bytes |
| Real canonical windows | 6,162 |
| Additional own CC0 windows / query vectors | 2 / 6 |
| Total encoder rows / requests | 6,170 / 200 |
| MPS encoding time, including query vectors | 23.263 seconds |
| Model load time | 3.244 seconds |
| Encoder peak RSS | 834,060,288 bytes |
| FAISS worker peak RSS | 75,431,936 bytes |
| Owned output at completion, before browser report | 19,706,521 bytes |

These are bounded pilot measurements, not total wall-clock build throughput or
an extrapolation of full-corpus indexing. RSS excludes separate GPU driver
allocations; the runtime checks worker RSS, not total machine memory pressure.
The final whole output also includes the small browser report.

| Public dev case | Compressed ANN anchor rank | Raw lexical | Existing morphology fallback |
| --- | ---: | ---: | ---: |
| `ru-chameleon-owner` | 2 | no result | no result |
| `ru-lefty-warning` | 1 | no result | 1 |
| `ru-land-grave` | 2 | no result | no result |

These queries name their book scope. They are three known public development
anchors, not an unbiased quality estimate. The lexical control disables only
the optional Russian morphology fallback; the separate morphology column uses
the existing fallback unchanged, including its quoted-query eligibility rules.
ANN requests100 candidates; controls request40. All reported positive anchor
ranks are at most2, and the zero-result controls have no hidden later hits.

Two own CC0 positive fixtures retrieve the correct passage at rank1. Each book
has only one passage, so these check real encoder/metadata/source plumbing, not
difficult relevance. A genuine wrong-book CC0 negative still returns the only
available, **irrelevant** ANN passage. Canonical verification proves that those
bytes exist, not that they answer the question. PQ scores are not calibrated
answerability or entailment probabilities. No generator, abstention threshold,
or production promotion is provided by this pilot.

Nine GET-only Chromium reader journeys passed: three actual RU top results,
two rank2 ground-truth anchors, a mobile RU reader, and the three own CC0
scenarios. The script checks actual API text/citation against the owned source,
exact highlighted section offsets, mobile overflow and JavaScript errors. It
writes no saved items or other user state. Desktop/mobile screenshots were
inspected. The reader can display the negative's exact unrelated source, which
does not turn that negative into a successful answer.

## Reproduce

Use an idle accelerator, the existing optional FAISS environment from
`SCALABLE_SEMANTIC.md`, pinned cached MiniLM weights, the frozen sample codebook,
and the same canonical editions. Full raw corpus text and model weights are not
distributed in the repository.

```sh
.venv/bin/python scripts/run_ru21_semantic_pilot.py \
  --source data/full \
  --output data/semantic-pilots/ru21-new \
  --cache data/models \
  --codec-report data/bench/ivfpq-frozen1308-sample-v2/report.json \
  --device mps
```

The default selection deliberately fails if the source no longer has exactly21
indexed Russian editions. For a larger later source, pass `--book-ids-from
ru21-ids.json`, containing a reviewed JSON list of21 edition IDs (the measured
list is in the compact public report). To continue an interrupted owned run,
repeat the command with `--resume`. A completed output is never overwritten;
choose another output for another measurement. A source attribution, canonical
text, model or profile change requires a fresh reviewed generation.

With the matching normal reader API available, the browser proof performs GETs
against actual retained passage IDs, and starts an owned read-only API for the
small CC0 fixtures:

```sh
.venv/bin/python scripts/browser_ru21_semantic.py \
  --pilot data/semantic-pilots/ru21-v1 --base http://127.0.0.1:8001
.venv/bin/pytest -q tests/test_semantic_pilot.py tests/test_encoder_ipc.py \
  tests/test_incremental_ivfpq.py tests/test_experimental_ivfpq.py
```

The next integration decision is an explicitly partial, source-bound worker
adapter with lexical fallback for uncovered books. That requires separate root
review; copying this experimental `ready.json` into a production active pointer
does not create a supported production index.
