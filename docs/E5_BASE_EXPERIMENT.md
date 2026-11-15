# Isolated E5-base build experiment

`scripts/build_e5_base_experiment.py` prepares and resumes a separate candidate
index. It does not change package profile registration, production defaults,
live active pointers, original frozen20/E5-small databases or final-test data.
The builder measures build behavior only. The separate development comparison below uses the completed index.

The source is `data/snapshots/curated20-v3/library.sqlite3`, pinned to SHA-256
`ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8`.
The runner copies its 252,600,320 bytes to the owned directory
`data/snapshots/curated20-e5base-v1`, verifies identical SHA and a distinct inode,
then builds through immutable/query-only source connections. No source
`Library` constructor or database migration is used. An ownership marker refuses
an unrelated existing destination or a changed copied database.

The candidate is `intfloat/multilingual-e5-base`, pinned to
`d128750597153bb5987e10b1c3493a34e5a4502a`. Its acquisition receipt is
`data/acquisitions/e5-base-d128750.json`; shared cached files include only the
necessary safetensors/config/tokenizer/pooling/card files. The pinned
[model card](https://huggingface.co/intfloat/multilingual-e5-base/blob/d128750597153bb5987e10b1c3493a34e5a4502a/README.md)
declares MIT and the trained retrieval input contract. The experiment uses
`query: ` and `passage: ` prefixes, 512 tokens, mean pooling and 768 dimensions.
The existing E5 window builder retains its bounded title/author/section header,
exact source substrings and 20-token overlap.

## Prepare, gated build and resume

```sh
# Preparation only: no model/runtime import or inference.
uv run python scripts/build_e5_base_experiment.py

# This starts MPS only after the validated development gate below.
uv run python scripts/build_e5_base_experiment.py --build
```

The second command refuses to load Torch or a model until
`experiments/large-subset-dev-v3/state.json` has `status: complete` and
`validated: true`, its dataset digest matches the summary, and
`summary.json` has `semantic_candidate_exercised: true`. Gate file digests are
retained in the build receipt. Seven no-model tests cover independent copying,
no clobbering, gate refusal, scoped registration/environment restoration and
profile-aware progress inspection:

```sh
uv run pytest -q tests/test_e5_base_experiment.py
```

Only this process registers `experiment-multilingual-e5-base-v1` in the profile
registry. The registration and environment restore on exit. The completed
index freezes its profile, prefixes, revision and window strategy in metadata;
later query code reads that stored contract independently of registry defaults.
The model remains unregistered for general package builds: use the identical
runner command to resume this experiment, rather than an unprefixed custom-model
`bookrag embed` command.

The build uses batch 8, MPS, high/low watermarks 0.65/0.5, the shared
`data/models` cache and `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`. It refuses an
implicit CPU substitution or missing pinned safetensors. The core builder keeps
its four-GiB free-disk reserve and durable generation checkpoints. Interrupted
work is retained; rerunning the same command validates and resumes its prefix.
An active pointer is written only inside the owned experiment directory after
the complete generation is sealed.

`resources.jsonl` samples committed/planned windows, interval throughput, peak
RSS, MPS tensor/driver/recommended memory and disk space every 30 seconds plus
initial/final samples. `build-receipt.json` records status, device/dtype, profile,
counts, wall time, gate and source hashes. Original frozen20, old E5-small and
live/demo active-pointer digests are checked after the build. The copied
database itself remains byte-identical to the source.

Keep these experiment sources frozen after completion. Future paired evaluation
must use an owned migrated evaluation copy, as the evaluation isolation helper
does, so it does not alter the source database's published bytes. Compare
E5-base and MiniLM on the same corpus and development labels in a separate
iteration. This build alone justifies no relevance, abstention or promotion
claim; the inherited cosine floor is not calibrated across model spaces.

## Completed build observation

The first owned build completed on `mps:0`, float32, with batch 8. It encoded
12,343 windows of 768 dimensions for all 12,108 source passages, with zero reused
or resumed vectors. Indexing took 680.204 seconds; runner wall time was 686.086
seconds. The vector/ID NPY files occupy 37,917,824 and 98,872 bytes respectively,
excluding the key map, receipts, model cache and copied SQLite corpus.

Twenty-four telemetry samples recorded peak process RSS of 948,240,384 bytes,
maximum observed MPS driver allocation of 2,290,860,032 bytes and maximum observed
tensor allocation of 1,132,847,104 bytes. GPU figures are sampled observations,
not a continuous peak-memory measurement, and should not be added to RSS as an
independent physical-memory total. Minimum observed free disk was
14,166,020,096 bytes, above the four-GiB reserve.

The original source and owned copy still had the pinned `ea1feab7…bb4b8` SHA
after completion. Protected original/E5-small/live/demo active-pointer guards
passed. The cached safetensors SHA was rechecked against the acquisition receipt
after the process exited and remained
`a18a44fad1d0b46ded15928144138cff1135d5cc8233bdd90be5f18822de09a7`.
The completed generation is
`gen-382f1f4a505947f91717995b6e9a919712a5925db0f2319e959339876c1a1c7e-19e22fb4b7b4`.

Receipts in the owned directory are `build-receipt.json`, `build-summary.json`
and `resources.jsonl`. The summary includes implementation hashes and array
sizes. No query, relevance evaluation, sealed final run or model promotion was
performed as part of this build.


## Paired development result: not promoted

The two fresh-process hybrid runs used the same54 development cases, exact
252,600,320-byte source database and corpus metadata digest. Both actually ran
hybrid54/54; all returned quotes, metadata and reader links were independently
exact. The Python implementation hashes also match.

| Metric at5 | MiniLM128 | E5-base512 |
| --- | ---: | ---: |
| Positive MRR |0.6712|0.6515|
| Positive hits |35/44|32/44|
| Cross-language hits |8/12|5/12|
| Scoped negative abstention |2/10|0/10|
| p50 query ms |119.3|118.8|

E5-base512 is not promoted. Its larger dimension/model did not improve this
inspected development fixture. Model and document-window size both differ here;
a later context-budget ablation would need a separate owned generation.
No sealed labels were inspected or evaluated.

[Control](../experiments/e5base-dev20-v1/control-minilm.json),
[candidate](../experiments/e5base-dev20-v1/candidate-e5base.json),
[paired deltas](../experiments/e5base-dev20-v1/comparison.json),
[completed build receipt](../experiments/e5base-build20-v1.json).
