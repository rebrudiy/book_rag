# Explicit compact manifest registry v2

This adapter is an opt-in registry format. Existing v1 registries keep their exact
inspection and full `windows.sqlite3` requirement. No existing registry, vector,
binding, manifest or source is migrated or removed.

```sh
python scripts/install_semantic_batch_registry.py \
  --library data/full --batch data/semantic-batches/BATCH \
  --compact data/semantic-batches/BATCH data/bench/COMPACT/compact.sqlite3 \
  --destination data/semantic-overlays/NEW-OWNED-REGISTRY
```

Repeat `--batch`; use `--compact BATCH PAYLOAD` only for entries that explicitly
choose the compact representation. Mixed legacy and compact entries are allowed
in v2. Unknown schemas and manifest formats fail closed. Each compact entry pins
its absolute payload and receipt paths plus both SHA-256 hashes. The registry
continues to pin the original producer receipt, build report, ready marker,
generation descriptor/model space, codec, every shard receipt/mapping and the
original live source bindings. The conversion receipt must embed the identical
producer receipt and pin its exact on-disk SHA. The ready input chain and all
producer counts remain authoritative; compact metadata cannot replace them.

Registration exhausts `compact_window_reader.compact_windows` inside the source
snapshot, then checks every reconstructed window against native passage/book
mapping and complete canonical passage coverage. The iterator checks raw dense
row count, missing book/prefix references, canonical ownership and text SHA,
Unicode bounds, every reconstructed UTF-8 SHA, edition counts and the complete
ordered `Window.key()` chain. Artifact hashes and source proofs are checked before
and after iteration. Early exit is not successful verification: callers must
exhaust the iterator. Publication retains the existing final source check under
the installation lock and atomic new-registry publication.

Runtime needs compact payload integrity and the original receipts, but does not
need to reconstruct text or load the old full text manifest. It preserves the
same epoch/provenance guards, exact canonical hydration, explicit-book routing,
query scoring and FAISS subprocess isolation. The new reader never imports the
semantic model module, tokenizer, Transformers, Torch or FAISS. No tokenizer
sentinel is used by registration or native workers. Repacking still runs as a
separate tokenizer-only process.

Owned CC0 tests deliberately remove only their temporary original text manifest
to prove that compact-v2 registration, inspection and verified scoped query work
without it. Real original payloads must remain until a separately authorized
migration and operational recovery procedure exist. This change does not add
coverage, repair parsing, improve relevance, or claim any production disk space
has already been freed. No actual registry is installed by the tests.

```sh
PYTHONPATH=src python -m pytest -q tests/test_compact_registry_v2.py \
  tests/test_compact_window_reader.py tests/test_semantic_batch_overlay.py \
  -k 'not real_direct_live_native'
```

The bounded proof uses deterministic fake vectors/native replies and real owned
SQLite source/receipts. A fresh subprocess verifies that inspection imports no
model/native runtime. It is not an ANN numerical benchmark or model evaluation.
