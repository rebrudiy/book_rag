# Explicit index coverage and target revision bindings

`book_rag.index_coverage` prepares safe use of a completed snapshot index over
an explicitly declared subset of a growing library. It does not install or
activate vectors, change ranking, load a model, or migrate a source snapshot.
Library startup explicitly installs tracking in its writable target database.
The [installer and query integration](INDEX_INSTALLATION.md) use the proof and
pre/post-query guards; inspection never migrates a frozen source snapshot.

The intended product behavior is semantic retrieval over verified covered
**edition IDs**, combined with lexical retrieval over every indexed edition.
Edition counts do not establish unique-work counts. A completed 1,308-edition
index is not a semantic index of a larger live catalog, and an unfinished
staging prefix is never an available partial index.

## Helper contract

| Function | Purpose |
| --- | --- |
| `ensure_epoch_schema(conn)` | Explicit writable target migration; returns its tracking namespace. |
| `capture_coverage(conn, book_ids=None, kind="subset")` | Strict portable proof from one coherent SQL snapshot. |
| `bind_coverage(target, proof, receipt_sha256=...)` | Explicit one-time strict verification and binding to target revisions. |
| `validate_coverage(target, proof, binding)` | Cheap pre/post-query validation; never silently rebinds changed inputs. |

`target` supplies `db_path: Path` and `connect()` yielding a fresh SQLite
connection. The normal Library object is compatible. A source snapshot should
instead use a direct read-only SQLite connection, without constructing Library
over the frozen path. Helpers respect an existing caller transaction; their
own read snapshots are rolled back without committing caller writes.

The portable proof contains `schema_version`, `kind`, sorted
`covered_book_ids`, edition/passage/section counts, `passage_fingerprint`,
`mapping_fingerprint`, `section_text_fingerprint`, and `book_headers` keyed by
edition ID. IDs are bounded to 50,000 unique nonempty entries.

The passage fingerprint preserves the existing ordered `p.id:p.sha256\n`
algorithm. Capture independently checks every stored passage SHA against its
actual UTF-8 text. A separate digest binds passage IDs to book/section IDs and
start/end offsets. Actual canonical section text is hashed once per section;
header digests bind book title/author/language and section ID/ordinal/title.
Capture also checks integer bounds, section ownership and exact
`section.text[start:end] == passage.text`. Its four-entry chapter LRU normally
holds at most 32 MiB of Python string storage; one larger chapter may remain
alone so neighboring spans do not repeatedly fetch the whole chapter. This
cache belongs only to one-time proof creation/binding, never query guards. A `full`
proof must include all currently indexed editions; `subset` permits unrelated
editions outside coverage. Proof capture never normalizes text.

A binding contains `schema_version`, `coverage_sha256`, the verified generation
`receipt_sha256`, target `db_identity` (resolved path/device/inode), epoch
`namespace`, `schema_cookie` and `state_token`. Binding checks actual input
content in one SQL snapshot, then reads fresh target state to reject covered
changes during the scan. The caller must already have verified the completed
index and its receipt. A supplied receipt SHA is validated syntactically here;
this module does not establish vector provenance.

Persist the proof and binding in a verified installation receipt. Query guards
return `valid`, `tracked`, `cache_hit`, `state_token`, `reason`, `covered_books`
and `covered_passages`. Compare pre/post `state_token` values as well as both
`valid` flags. Each call uses a fresh snapshot: holding the same read transaction
for both checks would hide concurrent changes. If covered input changes, the
whole bound index becomes invalid; do not silently remove that book or approve
it again from unchanged claimed SHA fields.

## Tracking and fail-closed behavior

The explicit migration creates STRICT `book_index_epochs` and
`book_index_epoch_meta` tables and 11 owned triggers. It uses a savepoint and
does not commit an outer transaction. SQLite 3.37+ is required for STRICT tables.
Production migration must be coordinated by the owner of the live database.

Source passage/section inserts, updates and deletes increment affected book
revisions. Book title/author/language/status changes also increment revisions.
Moving a row updates both its previous and new book. No-op updates do not bump
revisions. Notes and source-attempt state do not affect unchanged indexed input;
rolled-back indexing also rolls back its revision changes.

Tombstone epoch rows intentionally have no cascading book FK: deleting and
reinserting the same ID cannot reset its revision and create an ABA cache hit.
Before-insert conflict guards handle SQLite `INSERT OR REPLACE`, whose implicit
DELETE triggers can otherwise be suppressed with `recursive_triggers=OFF`.
Conflicting insert attempts may conservatively invalidate an index even when
the caller ignores the conflict. Revision overflow aborts a source write rather
than wrapping to an old revision.

Tracking definitions and namespace are verified. Repair of missing owned
objects rotates the namespace; incompatible definitions are rejected. A
different database inode/path, namespace or schema cookie invalidates a binding.
Even unrelated DDL requires explicit rebind. This is deliberately conservative.
Never use connection-local `PRAGMA data_version` as a cross-connection cache key.
Tracking assumes its private epoch rows are not deliberately forged by a DB
administrator; it is an integrity protocol, not an authorization boundary.

Validation has an eight-entry LRU protected by an RLock. Cache keys include the
target identity, proof/binding identity and current covered state. A persisted
verified binding remains usable after process restart when these match, without
rescanning passage text. New or changed **outside** editions leave a subset
binding unchanged; a newly indexed edition invalidates a `full` binding.

An old read-only database without tracking may validate an unbound proof by a
strict actual-content scan each time. No schema is created and that scan is
not cached. A tracked target requires explicit binding. Losing tracking on an
already bound target fails closed; it does not fall back to trusting stored SHA
claims. Copies/moves of a database need their own explicit binding. Evaluation
copies must not silently inherit a source target's binding as valid.

## Required installer and query gates

1. Select only an explicitly named completed immutable generation. Validate its
   model/profile/revision/strategy, finalized receipt, metadata/checkpoint
   consistency, vector length/dtype/normalization/checksums and key-map integrity.
   A unique-key first-row checksum alone does not prove every window's exact
   input/row association. Require that proof; do not manufacture or reseal it.
2. Independently validate **ids.npy**: int64, exact shape/file length, complete
   checkpoint ID checksum, positive IDs and exact source coverage membership.
   GenerationReuse validates vectors/key maps but does not establish this ID
   array's query mapping. Match passage counts and the proof's passage digest
   to the source generation. Source schemas are never migrated for inspection.
3. Acquire the target build/install lock. Reuse immutable large files by hardlink
   in owned target storage when supported, preserving the generation basename
   and original receipt bytes. Copy small metadata and publish an owned active
   wrapper/installation receipt atomically. Do not chmod shared hardlink inodes,
   edit original generation metadata, follow an arbitrary source pointer, or
   treat source-directory cleanup as permission to lose target files. Refuse or
   explicitly budget a copy fallback; no hidden multi-gigabyte copy.
4. Establish the target binding only after strict source/input checks. Recheck it
   before activation. Query guards must reject later covered changes even if
   activation raced with an update. A stale import retains lexical availability.
5. Hybrid lexical candidates remain global within the user's book/language
   filter. Semantic evidence is restricted to covered IDs. For a selected book
   or language with no covered edition, return explicit lexical fallback before
   loading model/arrays. On failed guards return global lexical fallback rather
   than incomplete or stale vector results.

Suggested wording: «По смыслу: 1 308 изданий; по словам: вся доступная
библиотека». For an uncovered scope: «Выбранное издание пока не входит в
семантический индекс. Показан поиск по словам». Coverage warnings describe the
semantic portion; they must not imply that lexical search excludes newer books.
Never present similarity scores as answer probability or partial coverage as a
full semantic corpus. Keep existing legacy full-index validation until explicit
coverage metadata and a verified installation receipt are present.

## Measurements and verification

`experiments/index-epochs-v1.json` records an owned synthetic benchmark using
actual `Library.import_book`: 20 original CC0 TXT editions, 1,000 paragraphs of
1,300 characters per book, 20,000 indexed passages, three fresh runs per
condition and alternating order. Models/network were not used. Both conditions
used the same archive/parser/SQLite/FTS path; only tracking differed.

| Median wall time | Without epochs | With 11 triggers | Ratio |
| --- | ---: | ---: | ---: |
| First import, 20 editions | 1.826 s | 2.922 s | 1.60x |
| Replacement refresh, 20 editions | 2.381 s | 4.082 s | 1.71x |

This is a material write cost. On a guard-only fixture extended to 1,308
editions and 21,288 passages, 50 validations had p50 8.520 ms and p95 9.228 ms.
The 20-edition guard measured p50 1.355 ms. The recorded strict bind took 343 ms
for the synthetic 1,308-edition fixture before the additional canonical-span
checks were added. Those numbers are retained as historical measurements;
current one-time binding may take longer. The cheap guard and trigger paths are
unchanged. This is not a measurement on 445,466 passages.

A separate read-only live probe, while ingestion and embedding were active,
observed 2,516 indexed editions/838,436 passages. The existing full fingerprint
took 9.882/7.806 s; its 1,308-edition, 445,466-passage subset took 5.154/5.763 s.
That subset digest matched the frozen build descriptor. Its 59,691 header rows
took another 0.986 s. These are contextual probes under contention, not a
controlled speedup experiment. Epoch guards avoid those repeated scans after
the one-time strict binding. Synthetic vocabulary, cache warmth and background
work limit timing generalization; no retrieval-quality promotion follows.

```sh
uv run pytest tests/test_index_coverage.py
uv run python tests/benchmark_index_coverage.py --output artifacts/epochs-new-run.json
```

The opt-in benchmark never opens a live/frozen library, cleans only its owned
temporary files and refuses to overwrite an existing report. Tests cover direct
text tampering with unchanged SHA claims, header/mapping changes, REPLACE,
rollback, deletion/reinsertion, outside additions, full-coverage drift, tracking
repair, DDL/inode changes, read-only legacy inputs, races, cache isolation and
boundedness. Production schema and background embedding jobs were not changed
while implementing or measuring this helper.
