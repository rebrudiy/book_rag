# Exact window-vector reuse helper

`book_rag.vector_reuse` supplies a bounded on-disk key map for reusing unchanged encoder inputs from a completed immutable generation. It does not encode books, change query scores, activate an index or automatically modify any existing generation. The semantic builder now uses it for new generations; [builder lifecycle and real demo validation](SEMANTIC_INDEX.md#reusing-exact-encoder-inputs) document that integration.

The key map stores only a 32-byte SHA-256 key and an integer row pointing into that generation's existing `embeddings.npy`. There are no raw source windows and no vector BLOBs or second vector copy in the key database. A future generation still needs its own complete vector array; reuse saves encoder calls, not the disk writes required by the current immutable-generation architecture.

## Identity and API

```python
from book_rag.vector_reuse import GenerationReuse, KeyMapWriter, ModelSpace

space = ModelSpace.from_metadata(metadata, normalized=True)

with GenerationReuse.open(completed_generation, space) as source:
    cached = source.lookup(exact_encoder_inputs)  # <=500 inputs; copied vectors or None

with KeyMapWriter(staging_generation, space, vector_count) as keys:
    keys.add_batch(exact_encoder_inputs, start_row=0)
    # Add every subsequent row, including reused rows, in contiguous batches.
    # After vectors, complete checkpoint and final-name metadata are durable:
    keys.finalize()
```

`ModelSpace` freezes model identity, immutable revision, dimension, actual model sequence capacity, complete embedding-profile JSON and explicit normalization behavior. It rejects mutable revision names, absent/empty profiles and inconsistent profile model/revision fields. Model/profile changes reject reuse even if dimensions match.

`window_key(text, space)` hashes the **exact UTF-8 encoder input** plus the model-space identity. It performs no stripping, Unicode normalization, prefix insertion or tokenization. A different document prefix, title/author/chapter header, newline, whitespace or Unicode representation produces a different key. Pass the exact strings sent to `model.encode`, not raw passages or approximately reconstructed text.

`KeyMapWriter` stores keys in a SQLite `WITHOUT ROWID` table (schema 2), with a unique row index for ordered validation/truncation. Batches contain at most 500 inputs and must start at exactly `rows_written`; the expected total is explicit. Duplicate input keys retain the first row. Row positions in a new generation refer to that new generation's array, including vectors copied from an old source.

`GenerationReuse.lookup` accepts up to 500 inputs, preserves request order and returns independent float32 vector copies or `None`. Duplicate requested inputs receive independent copies; callers cannot mutate the source memmap or another returned vector through aliasing. One source can be held open while the caller builds a new generation in batches.

## Completion, integrity and resume

Finalization requires all expected key rows, final-generation metadata and a completed vector checkpoint with matching model space, count and vector checksum. It validates float32 shape, exact array-file length, finite values and, when declared, unit L2 norms. Full-array validation and payload hashing run in approximately 1 MiB chunks. The final receipt records schema version, model space, generation name, counts and checksums of the vector payload, unique-key stream, SQLite key map, metadata and checkpoint. The key-stream SHA256 binds each unique key to its stable first row. It is updated incrementally in the same transaction as key insertion; resume reconstructs and checks it once. Duplicate bookkeeping retains at most the current bounded batch, with existing keys resolved through SQLite.

A read source must have a completed receipt, matching final directory/generation name, compatible model space and checksums. A `staging-*` directory cannot be a source even if its keys and vectors are complete. Missing/partial checkpoints, wrong profiles, truncated/wrong-shaped/wrong-dtype/nonfinite arrays, invalid key rows and modified files fail explicitly. The SQLite map is opened read-only/immutable and the array is memory-mapped read-only. File size/identity/timestamp checks precede batch access, detecting a changed/truncated mapped file before indexing it.

This helper validates the completed vector generation and key stream used for reuse. It does not use passage IDs and does not replace the semantic builder's corpus, passage-ID-array and activation checks.

An interrupted staging map can be reopened with `resume=True`. `truncate_to(completed_rows)` reconciles a key-map prefix that advanced beyond the durable vector checkpoint. After truncation, replay exact inputs from that point. If keys lag the durable vector prefix, the caller can replay tokenizer inputs for the missing key rows without re-encoding those vectors. Do not treat a key count alone as proof that the vector checkpoint advanced. The current builder rejects a missing/lagging/corrupt map and rebuilds only staging data, since its write order makes such a prefix invalid; it never silently seeds old vectors. `checksum` exposes the durable key digest and `finalized` distinguishes receipt-only recovery.

A crash between sealing the SQLite map and renaming its final receipt can be recovered by reopening explicitly and calling `finalize` again. A sealed map cannot be appended to or truncated. Existing final receipts are never overwritten. The caller owns array flushing, checkpoint durability, generation rename/activation and build locking; use the existing builder's exclusive lock.

## Seeding old completed generations

The writer defaults to owned `staging-*` directories. Adding a first key map to an existing completed generation requires the explicit `seed_completed=True` flag. It must be invoked only by an authorized parent seeding tool, with the normal build lock and source-generation validation. This helper never seeds old generations merely because a file is missing.

Seeding must replay the **original frozen corpus, metadata/header text, tokenizer revision, profile, sequence limit, window boundaries and row order**. Matching the final row count is necessary but insufficient: the same number of incorrectly reconstructed inputs can map keys to wrong vectors. The helper cannot recover historical encoder input identity from vector values alone. Do not seed from a changed live corpus or assume old/new parser output is equivalent. Generations lacking an explicit frozen profile are rejected rather than assigned a guessed model space.

No existing real generation was seeded or changed while implementing/testing this helper. Integration was validated on a fresh isolated demo generation followed by an append and a fresh control. Legacy generations were not seeded.

## Integration order

For each new encoder batch, look up exact input strings in a compatible completed source; encode only misses and assemble the output batch in original order. Write the complete new array batch, including reused vectors. Add keys for **all** new rows with the correct new offsets. Flush arrays and reconcile durable vector/key checkpoints. On resume, reconcile prefixes before appending. After the full generation is complete, write its final-name metadata, finalize the key map, rename the staging directory and activate through the existing semantic workflow.

Keep source and destination generations distinct. A reuse miss is ordinary, while corrupt/incompatible source metadata is an explicit error that the integrating builder must handle transparently. Do not silently bypass integrity checks or call mismatched-space vectors cache hits.

## Measured toy append

A pre-integration schema-1 helper measurement used a temporary owned generation and a deterministic synthetic encoder, 384 dimensions, 1,010 vector rows and 1,000 unique exact input keys. Ten rows were deliberate duplicate inputs. A reordered next stream requested the unchanged 1,000 inputs plus 100 appended inputs, in batches of 128.

| Observation | Result |
| --- | ---: |
| Requested new rows | 1,100 |
| Reused rows | 1,000 |
| Inputs requiring synthetic encoding | 100 |
| Fresh calls without reuse | 1,100 |
| Key-map file | 57,344 bytes |
| Final receipt | 868 bytes |
| Original float32 vector file | 1,551,488 bytes |
| Key writes + finalize | 0.0160 s |
| Source open + validation | 0.0051 s |
| Lookup + synthetic result verification | 0.0444 s |

The historical schema-1 byte/timing figures above predate the row index and incremental key checksum; current schema-2 storage is reported in the real demo builder measurement. All reused values matched the synthetic expected vectors, including reordered and duplicate requests. These counts show exact reuse behavior; the timings do not measure a real neural encoder or justify capacity claims at 10–50k works. SQLite page fill, key count, dimensions, storage speed and complete-generation validation cost all affect larger runs. Opening a source requires sequential validation of its vectors and key map, while lookup memory is bounded by one batch and vector dimension. Current dimension validation permits at most 65,536 float32 elements per vector.

Tests use only owned temporary generations with NumPy/SQLite, requiring no models or network. They cover exact Unicode/prefix keys, append/reorder/duplicate behavior, independent copies, model/profile/normalization mismatch, bounded batches, resume/truncation, receipt-crash recovery, explicit seeding, corrupted checkpoints/arrays/key rows/checksums and mutation after opening.
