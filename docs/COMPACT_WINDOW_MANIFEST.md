# Compact window manifest prototype

The isolated prototype replaces repeated encoder-input text with canonical
passage references and exact character offsets. It does not change production
retrieval, vectors, coverage, registries or existing manifests.

```sh
.venv/bin/python scripts/repack_window_manifest.py
.venv/bin/pytest -q tests/test_compact_window_manifest.py
```

The command requires the already cached pinned MiniLM tokenizer and the sealed
`data/semantic-batches/multilingual1000-v1/windows.sqlite3`. It writes a new file
under `data/bench/compact-windows1000-v1/`, refusing an existing destination and
retaining a 4.5 GiB reserve. No model weights or downloads are required.

Each dense window row stores its original passage ID, edition index, prefix ID,
passage-relative Unicode character bounds and a binary SHA-256. Exact decoded
headers are deduplicated separately. Offsets come from the production tokenizer
recipe: 128-token maximum, up to 24 header tokens, six reserved tokens and a
20-token overlap. Substring search is not used, so repeated passages retain
their original positions.

Before publication, every computed input must equal the sealed original input,
SHA and ordered `Window.key()` chain. A second pass reads the persisted compact
representation and reconstructs every input again. Source text, attribution,
origin, epochs and SQL namespace pass `verify_batch_source` before and after.
Verification counts all raw window rows and rejects missing prefix/edition
references using left joins. Once a final receipt provides a compact-file SHA,
that exact file hash is checked before and after reconstruction. Receipt
publication failures remove only the linked output inode owned by this run;
a replacement output is preserved. CLI probe reports use an absent-path
preflight and atomic no-overwrite publication, including against symlink races.

The measured 1,000-edition batch contains 190,601 windows: 99,586,048 original
bytes versus 12,857,344 compact bytes, an 87.09% reduction. Repacking and proof
took 29.83 seconds, with 493 MiB peak RSS. These are prototype measurements,
not production savings: both files remain on disk. The measured run predates
the subsequent integrity/publication hardening; its bytes and timing have not
been replaced with an unmeasured claim. Sixteen temporary-CC0 regression tests
cover the final implementation.

Installed Transformers imports Torch while checking availability even with
`USE_TORCH=0`. The fresh tokenizer-only process therefore temporarily marks
Torch unavailable using Python's `sys.modules["torch"] = None` sentinel, removes
that sentinel after loading and verifies that no actual Torch module was loaded.
An earlier failed probe imported Torch but no model, weights or GPU inference;
it stopped before producing output. The successful measured run loaded no
Torch, FAISS or llama.cpp module.

This format depends on the original canonical database namespace and cannot
serve as a standalone text archive. The production registry still requires the
old manifest. Do not delete it or activate this format until a separately
reviewed consumer, immutable-file verification and resume/registry migration
exist. Partial staging output is removed on failure; repacking itself does not
resume an interrupted staging transaction.
