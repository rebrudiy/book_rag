# Guarded SQLite clone experiment

This is a storage experiment on invented CC0 data. It changes no application code,
production database, frozen corpus, source permissions, SQLite configuration, models,
or installed index. It is preparation for a separately authorized large capture.

On this APFS workspace, a 21,028,864-byte synthetic SQLite file produced an exact clone
with a new inode. The native `fclonefileat` stage took 0.191 ms; the complete guarded
capture took 55.355 ms, including source/target SHA-256 scans and an SQLite integrity
check on the target. These are individual local observations, not a scale extrapolation
or a statistical benchmark. The report is in ignored
`data/bench/sqlite-clone-v1-final/report.json`.

| Check | Measured result |
| --- | --- |
| Native method | macOS `fclonefileat`, a descriptor form of `clonefile` |
| Filesystem | APFS, independently observed for the matching device |
| Initial main-file SHA-256 | `8c485231bc7a5e3b991f5ec769607dd9cc673aaabede4dd0034504a7452378e2` |
| Source bytes/mode/inode/mtime/ctime before vs after capture | Equal |
| Source WAL, SHM, rollback journal at capture boundaries | Absent |
| Target inode | Different from source |
| Target bytes and SHA-256 immediately after capture | Exactly equal to source |
| Target `PRAGMA integrity_check` | `ok` |
| Target-only SQLite update | Source remained byte-identical |
| Source-only SQLite update | Target remained byte-identical |
| Physical copy fallback | Disabled; zero bytes authorized |

The two databases left by the experiment are **intentionally modified afterward** to
test isolation. Their final hashes differ from the initial capture hash. The report
records each phase explicitly; it does not claim that the entire experiment leaves
its own synthetic source unchanged.

## Run on owned synthetic data

```sh
.venv/bin/python scripts/benchmark_sqlite_clone.py \
  --output data/bench/sqlite-clone-reproduce --size-mib 20
.venv/bin/python -m pytest -q tests/test_sqlite_clone_probe.py
.venv/bin/ruff check scripts/benchmark_sqlite_clone.py tests/test_sqlite_clone_probe.py
.venv/bin/ty check scripts/benchmark_sqlite_clone.py tests/test_sqlite_clone_probe.py
```

The output directory must be new. The CLI creates only its own 10–50 MiB synthetic
database and never accepts an external source database path. No Library constructor
or application endpoint is used. On Linux or an unsupported filesystem, native cloning
fails closed. An explicit physical-copy budget is available when intentionally needed:

```sh
.venv/bin/python scripts/benchmark_sqlite_clone.py \
  --output data/bench/sqlite-copy-reproduce --size-mib 20 --copy-budget-mib 24
```

The budget authorizes fallback only if native cloning is unsupported or crosses
filesystems. Permission, I/O, and disk-full failures do not silently invoke a copy.
If native cloning succeeds, the budget remains unused. Growth past the authorized
fallback size is rejected. Failed captures remove only their owned staging files;
existing or concurrently created destinations are never overwritten or deleted.

For the filesystem observation on macOS, `df -P <owned-output>` identifies the mount.
`diskutil info -plist <mount>` reports `FilesystemType`; comparing `os.stat(...).st_dev`
confirms that the owned output and the observed mount refer to the same device.

## Capture contract and limits

The reusable prototype `capture_sqlite(source, destination, copy_budget_bytes=0)`:

1. Opens a regular, non-symlink source with `O_RDONLY` and checks the SQLite header.
2. Rejects a nonempty WAL or rollback journal. It never checkpoints either.
3. Hashes main/WAL/SHM/journal and records identity, size, mode, mtime and ctime.
4. Clones from the held source descriptor into a newly owned staging directory.
5. Requires an independent target inode and exact main-file size/SHA-256 equality.
6. Opens **only the target** with SQLite `mode=ro&immutable=1`, `query_only=ON`, and
   checks integrity. All SQLite handles are closed explicitly.
7. Rechecks the full source state, rejecting any observed identity, main-byte, mode,
   or sidecar drift. The target is published without replacing an existing path.

The caller must first quiesce writers through its normal operational procedure. A
consistent online SQLite capture under continuing writes requires SQLite's own backup
or equivalent coordination. Hash-before/after guards detect observed changes; they do
not prove absence of an adversarial change-and-restore race. A transient capture of
uncommitted rollback-journal data is not accepted merely because the clone syscall is
atomic. A source with a pending WAL requires its owner to stop writes and checkpoint;
this helper never performs that operation on a source.

Native `clonefile` support and bidirectional isolation demonstrate the COW behavior.
No physical shared-block accounting was measured: summing `st_blocks` or file lengths
would double-count shared extents and would not prove actual additional disk use.
Subsequent COW writes may allocate storage and fail with `ENOSPC`; successful cloning
does not reserve space for future migrations. SHA scans and integrity checks remain
linear in database size, even when the native clone operation itself is fast.

This helper does not create a durable complete application snapshot, copy vector
generations, install/rebind an index, or validate epoch receipts. A byte-identical
SQLite clone still has a **new inode**. Existing partial-index bindings to the original
database identity cannot be treated as valid for it; use the normal verified installation
or rebind protocol. The current owned evaluation workflow and its source-change guards
remain unchanged. No actual large-library clone has been authorized or attempted here.

Twenty focused tests passed, including a real native COW check on the current filesystem,
Linux rejection, explicit-copy budget, pending real WAL, rollback journal, source/inode
drift, source SHM preservation, corruption, hardlink rejection, publication collision and
cleanup. Native-only tests skip on unsupported platforms; portable guard tests use owned
independent copies and do not claim APFS support there.

The native semantics were checked against the installed macOS `clonefile(2)` manual
(`man 2 clonefile`), including independent attributes, shared initial blocks, independent
subsequent writes, unsupported-filesystem errors, and possible later `ENOSPC`.
