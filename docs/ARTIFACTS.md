# Original artifact archive

`book_rag.artifacts` retains the acquired bytes of an edition independently of
a mutable download filename or an editable local input. It does not establish
that a transcription, OCR result or edition attribution is correct. Keep source
URLs, edition metadata, rights, revision evidence and canonical text hashes with
the book record as separate provenance.

## API and layout

```python
from pathlib import Path

from book_rag.artifacts import archive_artifact, locate_artifact
from book_rag.parsers import parse_book

record = archive_artifact(
    Path("data/downloads/provider-edition.epub"),
    Path("data"),
    expected_sha256=verified_download_sha256,
)
# Parse the immutable acquisition, not the provider cache or local input.
parsed = parse_book(record["path"])
```

The result contains `path: Path`, `sha256: str`, `bytes: int` and `method`, which
is `copy`, `hardlink` or `existing`. The returned path is within:

```text
<data_dir>/artifacts/<first-two-digest-characters>/<sha256>.<original-suffix>
```

SHA256 values are validated as exactly 64 hexadecimal characters and normalized
to lowercase. Only parser-supported book suffixes are accepted: TXT, MD, HTML,
HTM, XHTML, EPUB, FB2 and PDF. Sources must be nonempty regular files no larger
than 100 MiB, matching the extraction/download bounds. The suffix is normalized
but not inferred from contents; the parser still validates the declared format.
The original filename never becomes part of the archive path.

Identical bytes with the same suffix reuse one verified file. Distinct formats
retain separate suffixes even when their bytes happen to match. `locate_artifact`
returns verified bytes by digest without trusting an arbitrary supplied path. It
checks every matching supported-suffix candidate and returns the first suffix
in sorted order, or `None` when no candidate exists. Keep the recorded archive
path and format when reparsing; a digest alone cannot identify the intended
parser. Neither API treats a symlink as an archived original.

## Copy versus hardlink

Arbitrary local sources are copied into a private temporary payload. Later
in-place edits to those sources cannot change the archived edition, including
local files elsewhere within `data_dir` such as provider raw-source collections.
Symlink aliases and externally symlinked download roots also use copies.

On POSIX, a direct resolved single-link file under the real
`data_dir/downloads/` may share its inode with the archive. The project downloader
always writes a new temporary file and atomically replaces its cache path; this
leaves the old archived inode intact. Existing externally aliased inputs are
conservatively copied. Windows uses copies for open-source-handle portability.
Cross-device, source-permission and unsupported-link failures fall back to copy.
No source permissions are changed, and no source bytes are rewritten.

This optimization avoids a second physical copy of the owned download cache.
Its ownership contract matters: do not modify those cache inodes in place.
Files that can be edited by other tools should be passed as ordinary local
sources and must not be placed in the owned atomic-replacement cache merely to
obtain a hardlink. Direct manual edits of archived files are outside the API's
write-once contract; verification detects corruption rather than hiding it.

## Verification and atomic publication

Acquisition holds an open source descriptor, streams a bounded SHA256 and checks
source identity/size/timestamps before and after staging. The staged payload is
independently rehashed. Source replacement or changed bytes between hashing,
copying and linking cause `SourceChangedError`/`ArtifactIntegrityError`, never a
successful record describing mismatched bytes. Copies and linked payloads are
flushed to storage before publication.

Each attempt owns a private `.acquire-*` directory inside its destination shard.
The complete payload is published with a no-overwrite atomic rename: macOS
`renamex_np(RENAME_EXCL)`, Linux
[`renameat2(RENAME_NOREPLACE)`](https://man7.org/linux/man-pages/man2/rename.2.html),
or Windows nonclobbering `os.rename`. A directory-local atomic hardlink is a
fallback where the exclusive rename primitive is unavailable. A filesystem
supporting neither operation fails conservatively. Publication never uses a
check-then-`replace` sequence that could overwrite another writer's history.

A simultaneous publisher can win the destination name; the losing attempt then
verifies and reuses that file. A wrong or unreadable existing object is rejected
without replacement, even when a current source appears correct. The default
policy is conservative rejection, not automatic history repair. Diagnose and
quarantine a damaged object separately, retain the evidence, and only reacquire
from a source whose expected digest is known.

Failures and ordinary interruptions clean only the current attempt's temporary
directory. Other files and verified history are untouched. A correct complete
object can remain if the mutable source path changed just after publication;
that object remains useful by its verified digest while the caller retries.
A newly published corrupt inode owned by the failing attempt is removed. A
forced process kill can leave an uncommitted private temporary directory;
lookup ignores it, and operators may remove abandoned temporary directories
when no writers are running. The module does not delete other attempts' files
or old editions automatically.

## Verification

```sh
uv run pytest -q tests/test_artifacts.py
```

The tests exercise editable local files, atomic provider replacement preserving
hardlinked originals, format/digest deduplication, source and archive symlinks,
EXDEV/permission copy fallback, hash/copy/link races, corrupted-history rejection,
exclusive publication under concurrent writers, failed publication and interrupted
temporary cleanup. They use only local files and do not download books or models.
