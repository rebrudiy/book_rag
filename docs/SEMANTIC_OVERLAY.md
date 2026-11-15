# Explicit scoped compressed semantic overlay

This optional bridge makes the owned
[RU21 pilot](RU21_SEMANTIC_PILOT.md) available to the actual live library. It
does not change the primary float32 index, global retrieval or corpus database
schema. It is disabled unless `BOOKRAG_SEMANTIC_OVERLAY` names a successfully
prepared `installation.json`.

The dispatch contract is deliberately narrow:

- `semantic` or `hybrid` with an explicitly covered `book_id`; or
- `semantic` or `hybrid` with `language=ru`.

An unscoped global query, an uncovered selected book, another language, or a
different library path returns to the existing search implementation. In
particular an inherited environment variable cannot redirect a frozen control
or an owned evaluation database to the live overlay. Replacing the database at
the same target path invalidates its inode/namespace binding and falls back to
current lexical evidence. There is no implicit `semantic_reference` alias.

## Prepare and enable

Only a complete owned pilot root is accepted: `report.json`, the canonical
snapshot/receipt, sealed window manifest, and ready compressed generation. The
helper checks the recorded ready SHA, descriptor/model identity, full input
hash chain and every window's original passage/book assignment against the
sealed shard mappings. Source and artifact checksums are verified. Native index
type, dimensions, codebook/metric and every window ID are checked separately in
a FAISS-only process before publication.

The original pilot's bindings belong to its owned snapshot. They cannot be
used for the live database. Preparation creates a new wrapper generation:
immutable shard artifacts use hardlinks, with a verified copy fallback across
filesystems. The small shared codebook is copied so its original link metadata
does not change. New bindings point to the **actual target database** inode,
epoch namespace, schema and covered revisions. Its new ready SHA and a derived
installation proof identify this wrapper without rewriting pilot metadata.
Hardlink creation changes pilot shard link counts/ctime; source bytes remain
identical. No original1308 vector matrices or frozen databases are opened or
copied by this helper.

Before binding, the live database must match the pilot's exact canonical
coverage and full selected-edition signature in the same SQLite transaction.
Coverage verifies actual passage SHAs, exact section slices, IDs, boundaries,
book/section headers and status. The additional signature includes edition,
artifact/source/rights metadata, section contributors/translators and indexer
fingerprints, which the existing embedding epoch triggers do not all track.
Only indexed Russian editions are accepted. The final source guard and atomic
directory publication hold the same short `BEGIN IMMEDIATE` transaction; source
writes are blocked during that last guard/publication boundary. No SQL DDL or
data mutation is issued. Metadata publication is flushed before enabling.

Preparation requires a **new** destination outside the pilot and primary vector
directory. It rejects an existing destination and never rebinds automatically.
This first bounded bridge accepts at most25 editions /50,000 windows, a
128MiB source-generation payload and64MiB per linked file. These limits describe
the bridge, not a full-corpus ANN service. Native artifacts must be trusted,
self-produced local files; a checksum is not a safe parser for arbitrary public
FAISS downloads.

```sh
.venv/bin/python scripts/install_semantic_overlay.py \
  --library data/full \
  --pilot data/semantic-pilots/ru21-v1 \
  --destination data/semantic-overlays/ru21-live-v1
```

The command uses a migration-free target adapter, loads no embedding model, runs no
retrieval-quality queries, and prints the installation path. A `mode=rw`
connection is required solely to reserve the final writer lock: SQLite
`query_only` would reject `BEGIN IMMEDIATE`. An authorizer rejects all DML, DDL
and write PRAGMAs; only source reads, schema-version reads and transaction locks
are allowed. Runtime native query connections remain `mode=ro` /`query_only`.
Existing target
epoch tracking must already be installed by normal application startup. It does
not set environment variables or restart a server. To enable the prepared
wrapper, use the absolute printed path in the server environment:

```sh
export BOOKRAG_SEMANTIC_OVERLAY="$PWD/data/semantic-overlays/ru21-live-v1/installation.json"
export BOOKRAG_MODEL_CACHE="$PWD/data/models"
```

Start the normal server using its existing documented command. Disable the
bridge by removing `BOOKRAG_SEMANTIC_OVERLAY`. Existing primary index files and
lexical search require no rollback. A covered edit requires explicit review and
a new preparation destination; automatic retries never approve changed content.

## Query and failure behavior

Parent-side encoding loads the stored pinned MiniLM profile from the local
snapshot path, with no Hub download. Model-file SHAs are checked against the
acquisition model space and cached by device/inode/size/mtime/ctime. Stamps are
rechecked after encoding, and the loaded query token limit must still be128.
The local-path model may occupy an additional entry in the existing bounded
two-model cache. The optional FAISS wheel is lazy and absent from ordinary
imports/CI paths.

The native worker is a fresh `python -m book_rag.semantic_overlay --worker`
process using bounded JSON stdin/stdout, not pickle or fork. It imports neither
Torch nor `book_rag.semantic`; it reads the actual target via a read-only
adapter. The parent supervises a15-second deadline and512MiB RSS, allows one
native worker with a1-second slot wait, and cleans up workers on timeout or
shutdown. Query vectors are finite normalized float32 with shape `(1,384)`.

Native queries use existing scoped ID selectors and a fixed pool of100 unique
passage candidates, independently of output limit. The parent hydrates through
live `Library.passages` in a coherent SQLite snapshot, rejecting missing,
unverified, misowned or wrong-language evidence. Quoted constraints remain
mandatory. Hybrid search fuses this ranking with up to50 lexical candidates in
the **same requested scope**, using existing reciprocal ranks with constant60.
The50 value reflects the current public lexical API limit; this bridge does not
change global retrieval behavior. A Russian language scope can therefore find
new uncovered books through words while semantic coverage remains partial.

Overall canonical epochs and the full selected-edition signature are checked
before encoding, in the worker, during live hydration, and in a fresh transaction
after hydration. The fresh check observes writers that a held read snapshot
cannot see. Installation/ready identities are checked around the operation as
well. Any covered source/provenance change disables the entire overlay for that
query; shared-shard partial survival is not silently presented as unchanged21
coverage. Invalid native evidence, worker failures, missing optional packages
and unavailable model weights yield an explicit current lexical response.
Uncovered additions and private saved notes do not invalidate covered texts.

Successful responses add honest `semantic_coverage` fields:

```json
{
  "indexed_books": 21,
  "searchable_books": 11001,
  "active": true,
  "requested_book_covered": null,
  "scope": "ru21-scoped-overlay",
  "scope_language": "ru",
  "scope_searchable_books": 21
}
```

Counts are read from the actual database; this example is not a promised corpus
size. Results label `semantic_score_approximate`, and warnings disclose scope
and coverage. PQ scores are **not calibrated answerability or entailment**. No
cosine threshold is transplanted into the compressed score space. The genuine
wrong-book pilot negative still gets an irrelevant nearest passage; exact source
verification cannot make it an answer. Existing RAG citation/answer validation
remains necessary. No sealed evaluation labels are accessed by this module.

## Validation

`tests/test_semantic_overlay.py` uses owned CC0 sources and fake encoders. One
optional test trains/builds a tiny real native index entirely in subprocesses,
then exercises actual preparation and querying. That is a protocol test, not
retrieval-quality evidence. Tests cover original byte preservation, actual live
inode binding, control-library isolation, quoted constraints, fixed candidate
prefixes, metadata drift missed by epochs, source changes during hydration,
corrupt/missing native hits, model-weight changes, failed publication cleanup,
copy fallback and bounded worker termination. The actual CLI subprocess is
tested against an owned native pilot, including unchanged main-database SHA and
schema version. Dispatch tests live separately.

```sh
.venv/bin/pytest -q tests/test_semantic_overlay.py \
  tests/test_semantic_overlay_integration.py
```

Read-only preflight on the real11,001-edition source found exact RU21 coverage:
the full canonical proof took120.5ms, four shard bindings79.7ms, and eight epoch
checks a median4.57ms over seven warm trials. Full per-query provenance signature
checks add work beyond those epoch timings. These are availability/source-proof
measurements, not semantic-quality or production latency claims. Actual live
installation/browser evidence is recorded separately after integration review.
