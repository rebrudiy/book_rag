# Installing a ready semantic subset

`book_rag.index_installation` prepares and explicitly installs a **completed**
generation into an independent existing library. It does not start encoding or
activate a staging checkpoint. Installation is a separate action from a build;
no incomplete generation has been promoted by this implementation.

```python
from pathlib import Path
from book_rag.index_installation import install_partial_index

# Run only after the source build has completed and its frozen inputs remain static.
metadata = install_partial_index(
    Path("data/snapshots/completed-source"),
    Path("data/live-library"),
    model_cache=Path("data/models"),
)
```

The command-line equivalent installs into `BOOKRAG_DATA_DIR`:

```sh
BOOKRAG_DATA_DIR=./data/live-library uv run bookrag install-semantic-index \
  data/snapshots/completed-source --model-cache data/models
```

Use `--copy-arrays` when hardlink ownership is unsuitable; budget a second
matrix on disk. The source needs
`library.sqlite3`, `vectors/active.json` and a sealed generation containing
`embeddings.npy`, `ids.npy`, `window-keys.sqlite3`, `metadata.json`,
`checkpoint.json` and `vector-reuse.json`. Legacy generations lacking ready
key-map evidence are rejected. No Library constructor is called for either
directory. Source SQL uses `mode=ro&immutable=1`; a pending or symlink WAL is
rejected initially, after verification and again before activation. Source and
target database inodes must differ.

## What is proved

1. `GenerationReuse` verifies the completed descriptor, pinned model/profile/
   dimension/normalization space, finalized key-map schema and checksums, complete
   finite normalized float32 vectors and exact vector-file length.
2. The installer independently reads `ids.npy` as read-only int64 with one row
   per vector, exact NPY file length and the complete checkpoint ID checksum.
   Every vector position must equal the actual passage ID for its encoder window.
3. Coverage capture checks actual UTF-8 text, stored passage hashes, source-slice
   bounds and section ownership. It includes passage/section mappings, complete
   section-text hashes and exact title/author/language/chapter headers. A matching
   claimed `p.sha256` alone does not establish identity.
4. A locally cached tokenizer at the exact pinned revision reproduces every
   window in source passage order, including bounded headers, overlap and frozen
   document prefixes. No weights are loaded, no model is encoded, and
   `local_files_only=True, trust_remote_code=False` prevents downloads/remote code.
   Each recomputed first-occurrence key must occupy its actual first row, and
   every duplicate must point to an already verified first occurrence.
5. The target's actual covered inputs must match the source proof. Only an
   explicit target migration installs epoch tracking. Strict binding then records
   the target database identity, namespace, schema cookie and per-book revisions.
   A final short `BEGIN IMMEDIATE` guard prevents covered input drift between
   validation and pointer publication. Full scans occur before that writer lock.

The proof is scoped to indexed passages in its explicit edition list. The
installer does not turn preliminary work IDs into verified unique works or
establish corpus/relevance quality. [Coverage API and revision semantics](INDEX_COVERAGE.md)

## Schema-2 duplicate reconciliation

The current schema-2 `key_checksum` records **unique keys and first rows**. The
installer computes an additional all-window `key + row-position` digest and
verifies every ID/key association. It never labels the old unique stream as a
recorded duplicate-row history.

- All keys are unique: the recorded unique checksum also covers the full stream;
  the original immutable generation can be linked unchanged.
- Duplicate payloads are bitwise identical to their verified first rows: an
  explicit `schema2_reconciled_exact` installation proof permits unchanged
  hardlinks. Signed-zero and every other byte difference count as different;
  numeric tolerances are not accepted as association evidence.
- Any duplicate differs: a new deterministic `gen-reconciled-…` target generation
  is materialized exclusively from verified first-row vectors. This is exact
  existing-vector reuse, with **zero new encoding**. Its new metadata, vector/ID
  checksums, complete checkpoint, all-window digest and ready receipt describe
  the resulting bytes. Source files remain unchanged.

The first-row map uses temporary disk scratch of eight bytes per window. Input
text is not persisted. Materialization streams bounded 500-row buffers and does
not repeat tokenization. It can require a full additional float32 matrix on disk;
space checks reserve four GiB for scratch/copy/materialization and recheck during
matrix writes. A source checksum field does not bypass schema-2 reconciliation.

## Publication and ownership

The target uses the same `.build.lock` convention as embedding builds. Each
generation is prepared under a unique owned temporary directory and published
without replacing an existing name. A valid matching previously prepared
generation can be reused; conflicting immutable files are rejected. Source
generation basenames and sealed files stay unchanged when linking. Rewritten
generations receive a distinct deterministic basename and new sealed metadata.

Hardlinks avoid a second physical vector copy for an owned completed source.
EXDEV/permission/unsupported-link cases fall back to fsynced copies; callers may
set `hardlink=False`. The caller must preserve the completed generation's
immutable-file contract. Neither endpoint may modify these arrays/maps/metadata
in place; replacement requires another generation. The installer never chmods
or opens source arrays writable. Paths/components and consumed files reject
symlinks; cleanup removes only its own temporary names.

An immutable `vectors/installations/install-<uuid>.json` records source/installed
file checksums, exact-input derivation, actual target file identities, model space,
coverage proof and epoch binding. The active metadata adds exactly:

```text
coverage_kind = "subset"
coverage = <portable strict proof, including covered_book_ids/counts>
coverage_binding = <explicit target namespace/revision receipt>
installation_receipt = {path: "installations/install-….json", sha256: "…"}
```

The binding uses the SHA of the **installed** ready `vector-reuse.json`, including
when it was derived. Generation files are not rewritten with target coverage.
Receipt and active-pointer payloads are fsynced; directories are synced on POSIX.
Publication/fsync failures restore the previous active bytes. Failures before the
atomic pointer commit leave the previous active index available. A crash after a
valid generation/receipt is published can leave an inactive immutable object;
retries reuse matching generations. A process kill at the active-pointer commit
can leave the old or new complete pointer, rather than a partial JSON payload.

## Query/restart integration

```python
from book_rag.index_installation import read_installation_receipt
from book_rag.index_coverage import validate_coverage

receipt = read_installation_receipt(target_directory, active_metadata)
before = validate_coverage(library, receipt["coverage"], receipt["coverage_binding"])
# Only score covered candidates if before["valid"].
# Run the same guard afterward and require an unchanged state token.
```

The reader verifies receipt SHA, frozen model space, **all** sealed generation
metadata fields (only the four wrappers above may differ), and actual generation
file inode/device/size/mtime. It does not rescan vector payloads or load a model.
Together with epoch binding this supports a cheap restart guard without silently
reproving changed inputs. It assumes immutable payload ownership; periodic full
checksums remain appropriate for detecting silent storage corruption that leaves
filesystem stamps unchanged.

The source fingerprint remains the source fingerprint. Integrated queries use
this explicit coverage contract before and after retrieval. Hybrid lexical
candidates span the entire live library within the selected filters; semantic
candidates belong to covered editions only. An uncovered selected book falls
back to words before model loading. Missing, changed or corrupt installation
evidence also falls back to current lexical results. Ranking and thresholds are
unchanged.

Responses include `semantic_coverage` with `indexed_books`, `searchable_books`,
`active` and `requested_book_covered`. The interface displays edition coverage
and explicitly identifies word search for uncovered books. This is availability
information, not a relevance score or a claim that all editions are unique works.
Library startup explicitly installs revision tracking in its writable database;
frozen source inspection never constructs a Library or migrates its schema.

## Verification

```sh
uv run pytest tests/test_index_installation.py tests/test_index_coverage.py
```

Temporary fake-encoder fixtures cover completed/legacy/staging sources, strict
input headers/text/slices and resealed wrong maps, full ID integrity and forged
membership, identical/different duplicate rows, copy fallback, interruption,
directory-fsync rollback, drifting target epochs, a source WAL appearing during
verification, disk reserve, receipt/file tamper and path/model-space checks.
The optional local completed reuse experiment also passed: 190 real MiniLM
windows installed only into an owned temporary target, with every source DB/
active/generation SHA and source-tree entry unchanged. No running large build,
live library or frozen production database was installed or modified by those
fixtures. `tests/test_partial_semantic_search.py` checks real subset installation,
global lexical availability, selected-book fallback, changing covered text,
unrelated notes/failed refreshes, inference-time drift and corrupt receipts/files.
`scripts/browser_partial_coverage.py --real-backend` checks the integrated API and
reader in Chromium on an isolated three-edition library with a fake encoder.
