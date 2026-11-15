# Bounded complete-edition semantic batches

The direct-live batch builder encodes every canonical passage of explicitly
selected indexed editions into compact, resumable IVF-PQ shards. It reads the
original library through `mode=ro` without initializing or migrating it. It never
copies the live SQLite database or stores a full float32 embedding matrix.
This extends the earlier [Russian pilot](RU21_SEMANTIC_PILOT.md) to arbitrary
explicit edition IDs and languages. Building is separate from registration and
from actually serving semantic search.

Install project dependencies, including the optional native backend:

```sh
uv sync --locked --extra dev --extra semantic --extra ann
```

FAISS and Torch must run in separate processes on the measured macOS setup.
The coordinator imports neither framework; an offline Torch-only MiniLM worker
returns bounded normalized batches to a FAISS-only CPU worker. This is enforced,
not worked around with an OpenMP environment override. The optional `ann` extra
pins faiss-cpu1.16.0. A separate CI job runs the real native CC0 tests without
installing Torch or downloading model weights.

## Inputs and operation

Acquire permitted texts following [bulk source instructions](BULK_SOURCES.md),
and explicitly review their provenance and rights. The checked-in
`corpus/semantic-batches/fr-de-96-v1.ids.json` records only public catalogue IDs;
the original files, indexed corpus and model weights remain outside Git.
The codebook report is an owned output of `scripts/benchmark_ivfpq.py`, not a
download of arbitrary native artifacts. It must pin a compatible model,
revision, profile, window strategy and the measured codebook checksum.

```sh
# First prove durable interruption, without activating any new search route.
.venv/bin/python scripts/build_semantic_batch.py \
  --book-ids-from corpus/semantic-batches/fr-de-96-v1.ids.json \
  --output data/semantic-batches/fr-de-96-v1 \
  --stop-after-chunks 2

# Continue from the exact durable prefix; source/model changes fail closed.
.venv/bin/python scripts/build_semantic_batch.py \
  --book-ids-from corpus/semantic-batches/fr-de-96-v1.ids.json \
  --output data/semantic-batches/fr-de-96-v1 --resume
```

Use `--prepare-only` to produce/count exact encoder windows before allocation.
The default `--max-windows` is200,000; at most2,048 explicitly listed editions
are accepted. A selection that exceeds the window bound is rejected in full;
it is never silently truncated and presented as complete. Window manifests
default to at most512MiB, total owned output to700MiB, each worker to2GiB RSS,
and free disk reserve to4.5GiB. These per-worker bounds are not a combined-memory
guarantee. `--phase-timeout` defaults to3,600 seconds and always has a finite
1–86,400-second range. A timeout closes the native process and pipe. Failure
aborts the encoder rather than reading an outstanding encode reply as a final
model-verification receipt. Interrupted/checkpointed/failed reports are retained
under unique names; only a fully verified complete run writes `report.json`.

The complete-run receipt is required in addition to `ready.json`: a native
generation can be sealed before a later source, model-shutdown or disk check
fails. Such a generation is not approved for production registration.

## Proof and resume

All input windows come from the existing production `_windows` function,
including the bounded title/author/section prefix and overlapping tokenizer
offset windows. The manifest pins that exact function recipe, model metadata,
every input SHA and a dense ordered hash chain. Source proof binds selected
passage/section IDs and SHAs, full attribution/provenance/indexer signatures,
existing epoch namespace and the original database inode. Header, source-text,
edition, rights, artifact metadata, contributor and translator changes reject
reuse; an unrelated edition update or a failed refresh preserving canonical
content does not. Inputs are selected in global ascending passage-ID order.

Only missing256-window chunks are encoded. Immutable native shards and small
checkpoints preserve the committed prefix. Sealed redundant chunks can be
reclaimed by the existing builder; no complete raw vector matrix is retained.
An interrupted manifest can recover its internal committed receipt if its
external receipt had not yet been published. Missing or corrupt sealed payloads
fail closed. No helper automatically rebinds a changed source or activates an
unfinished batch.

## Measured first real batch

The selected96 editions,48French and48German, were outside the original1,308
edition vector subset. They contain2,451 canonical passages and2,272,993
characters. All8,518 production windows were encoded:512 before a deliberate
checkpoint, then8,006 after restart, with zero durable-window re-encoding.
Encoding took2.208+28.795 seconds on MPS, excluding model load, tokenization,
proofs, compression and publication. Encoder peak RSS was871,677,952 bytes;
native worker peak67,452,928 bytes. Complete owned output was7,108,373 bytes.
Both model-file and original selected-source checks passed. See the public
[receipt](../experiments/semantic-batch-fr-de-96-v1.json).

These are selected short editions, not a representative full-library speed or
relevance result. A complete canonical edition does not prove a distinct complete
work or correct source catalogue language. The measured batch was built before
production registration. Its vectors do not increase the number of books the
interface can semantically search until an explicitly source-bound registry is
installed, enabled and checked in the live browser.
