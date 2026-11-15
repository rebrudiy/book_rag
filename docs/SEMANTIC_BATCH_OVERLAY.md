# Explicit-book registry for direct-live semantic batches

This opt-in registry exposes completed
[`build_semantic_batch.py`](../scripts/build_semantic_batch.py) batches to the
same live library that supplied their canonical text. It reuses their existing
source bindings: **no generation, shard, mapping or binding is copied or
rewritten**. Registry installation writes only new metadata in a separate owned
destination. It performs no embedding build, model download or quality tuning.

This differs from the older [RU21 wrapper](SEMANTIC_OVERLAY.md): RU21 was built
against an owned canonical copy and needed an explicit new live binding. A
direct-live batch already carries the original database inode, namespace and
epoch proof. Copying its database or resetting its epochs cannot silently create
another supported target.

## Scope and behavior

The first version activates only for `semantic` or `hybrid` requests naming an
explicit registered `book_id`. The language filter, when supplied, must match
that book. Global requests, language-only requests, uncovered books and other
library database paths continue through their existing search routes. The RU21
language route and the primary frozen1308 index keep their own behavior.

The response includes exact current source text/citations and partial-coverage
metadata. For the initial96-edition batch, a covered book reports a96-edition
batch against the actual current searchable corpus, plus
`scope=book-batch-overlay`, `scope_book_id`, its actual source language and
`scope_searchable_books=1`. This does not advertise96 additional books as global
semantic coverage. It does not count editions as verified unique works.

Native queries produce a fixed pool of up to100 unique passage candidates,
independent of requested output length. Live hydration rejects missing,
unverified, misowned, duplicate or wrong-language evidence. Quoted phrases remain
mandatory. Hybrid search combines those candidates with up to50 current lexical
candidates from the same selected book, using reciprocal ranks with constant60.
Approximate PQ scores are labeled as such; no old cosine cutoff is applied.
They do not establish relevance, entailment, answerability or a safe abstention
threshold. RAG answer validation remains a separate requirement.

## What registration proves

Registration accepts only a successfully finalized `report.json` with complete
native output, unchanged source and model-file verification. An existing
`ready.json` alone is insufficient: a final provenance/model/resource check can
fail after shard publication. Failed, checkpointed or staging outputs are
rejected. Report and producer encoder model spaces must agree, and reports must
confirm native-library isolation.

The registry pins report, ready, descriptor, manifest database/receipt, model
space, codebook, mappings, shard receipts, bindings and source identity. It
checks that the exact producer input stream matches native mappings in window
order and covers every selected canonical passage with stable original IDs.
The stored MiniLM model/revision, prefixes, window recipe and acquired model-file
SHAs remain authoritative. Source SHA claims are verified against actual
canonical text and exact section offsets, not accepted as self-reported hashes.

Each original shard binding is validated against the actual target database.
Registration never calls `bind_coverage`, repairs epochs or changes schema.
Duplicate edition routing across batches is rejected. The final guarded
publication uses the same short SQLite transaction with `BEGIN IMMEDIATE` so
source writers cannot interleave with the last source check and directory rename.
The migration-free `InstallationTarget` reserves this lock through a `mode=rw`
connection whose authorizer forbids DML, DDL and write PRAGMAs. Runtime source
connections remain `mode=ro` and `query_only`.

The current bounded registry permits at most16 batches,8,192 distinct editions,
200,000 windows per batch and128MiB per native generation. These are deliberate
first-version limits, not a claim of finished50,000-book semantic coverage.
Native artifacts must be trusted, locally produced outputs; checksums do not
make arbitrary third-party FAISS files safe to deserialize.

## Explicit installation and activation

Prepare the batch first using its separate documented acquisition/build
workflow. Then choose a **new** destination outside the original library and all
batch directories:

```sh
.venv/bin/python scripts/install_semantic_batch_registry.py \
  --library data/full \
  --batch data/semantic-batches/fr-de-96-v1 \
  --destination data/semantic-registries/fr-de-v1
```

Repeat `--batch` to register additional disjoint completed batches. Existing
destinations are never overwritten. A source change requires explicit review
and a suitable newly completed batch; registration will not reapprove changed
source by inventing replacement bindings.

The command loads no embedding model, leaves application configuration unchanged
and prints the absolute registry path. Enable it in the existing server
environment, then use the normal documented server launch command:

```sh
export BOOKRAG_SEMANTIC_BATCH_REGISTRY="$PWD/data/semantic-registries/fr-de-v1/registry.json"
export BOOKRAG_MODEL_CACHE="$PWD/data/models"
```

Removing `BOOKRAG_SEMANTIC_BATCH_REGISTRY` disables this route. The primary index,
original batches and lexical search need no rollback. Normal model encoding
uses the same verified offline MiniLM adapter as RU21; no paid API is required.

## Guards, isolation and limitations

Registration performs the complete canonical source scan. Before and after
native search and live hydration, runtime verifies the original producer epoch
token, exact epoch-trigger DDL/schema, namespace, inode and covered headers in
one coherent SQL snapshot. These triggers cover all relevant passage and section
mutations, including replacement and deletion. The unchanged token permits reuse
of the registered canonical proof without repeatedly scanning every passage body.
Edition/source/rights/attribution/indexer fields outside those triggers still
require exact hashes and counts for `books`, `book_metadata`, `index_versions`
and `section_roles`, compared with the existing producer signature. No receipt
is rebound or rewritten. Returned quotes are independently verified against the
live canonical sections. A fresh transaction after hydration catches concurrent
writers that a held read snapshot cannot observe.

Artifact digests use a bounded 256-entry cache keyed by path, device, inode,
size, modification time and change time. Access time is excluded. This caches
only the digest: every caller still compares its expected receipt SHA. Missing,
symlinked, replaced or modified files invalidate the entry; metadata is checked
again after hashing and cached reads. The native child remains isolated from
Torch and uses its own bounded cache.

This first version conservatively disables the whole affected batch if any
covered edition or its provenance changes. Other batches can retain their own
valid routes. Unrelated new books, private saved notes and failed refreshes that
preserve the last successful edition do not invalidate its content. Missing or
stale source, unavailable models/native dependencies, bad candidates or native
worker failures yield an explicit current lexical response for the selected
scope; they do not cause automatic rebinding.

Native FAISS runs in a fresh JSON stdin/stdout subprocess, with no pickle, fork,
Torch or model-module imports. It has a15-second deadline,512MiB RSS supervision,
one native slot with a1-second wait and cleanup on failure/shutdown. The existing
model adapter verifies cached weight SHAs and post-encode file stamps. Optional
FAISS absence does not affect ordinary lexical operation.

Aggregate producer signatures cannot supply an independently trusted per-book
provenance baseline. Therefore current queries retain the full batch scope;
this is intentionally stricter than inspecting only one selected book. A
read-only preflight on the actual96-edition,2,451-passage,1,212-section batch
matched its saved state in all four trials:351.0ms cold, then98.6/92.8/105.8ms
warm. The median was102.2ms. A request performs several such checks and native
startup; those measurements are not end-to-end query latency or search quality.

The recorded direct-live build has8,518 canonical MiniLM windows. It resumed
after512 durable rows and encoded the remaining8,006 without repeating durable
work. That demonstrates availability and resumability, not heldout retrieval
quality. No sealed queries, labels or final scores are used by this registry or
its tests. Actual live installation/browser results are recorded separately by
the integration owner after review.

## Reproducible checks

The independent tests use newly owned French/German/English CC0 text, fake
tokenizers/encoders and original-live bindings. One optional native test trains
and builds a tiny index in a separate process, invokes the actual registration
CLI, and queries the live source through the real worker. It checks unchanged
main-database SHA/schema and byte-identical existing bindings/artifacts. Its
fake vector assignment tests transport/proof correctness, not language quality.

```sh
.venv/bin/pytest -q tests/test_semantic_batch_overlay.py \
  tests/test_semantic_batches.py
```

The suite covers staging/failed finalization, conflicting encoder spaces,
corruption, duplicate routing, no-rebind behavior, controlled database copies,
global/language isolation, quoted constraints, stable output prefixes, namespace
or untracked provenance drift, invalid native hits, registry swaps, source
changes during encoding/hydration and monitoring-command timeouts.
