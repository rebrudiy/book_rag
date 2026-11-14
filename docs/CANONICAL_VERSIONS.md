# Immutable canonical context for saved quotations

`book_rag.canonical_versions` preserves the exact parsed strings behind a saved
quotation. An immutable acquired file alone is insufficient: a later parser can
normalize or structure that file differently. Historical context therefore
needs its own identity, independent of the current parser and current book row.

Archive lazily when the user first saves a quotation, rather than duplicating
every ingested book. The caller constructs `ParsedBook` from one coherent SQLite
snapshot, with its exact ordered titles/text and bibliographic metadata. This
module never reparses or modifies an original archive and never accesses a DB.
Library/API/reader integration is separate from this helper.

```python
from book_rag.canonical_versions import (
    preserve_canonical,
    read_canonical_manifest,
    read_canonical_section,
)

version = preserve_canonical(
    parsed_book_from_sqlite,
    data_dir,
    artifact_sha256=source_sha256,
    parser_fingerprint=None,
    indexer_fingerprint=stored_indexer_fingerprint,
    title=stored_title,
    author=stored_author,
    language=stored_language,
    edition=stored_edition,
    source_url=stored_source_url,
    rights=stored_rights,
    rights_url=stored_rights_url,
    section_metadata=stored_section_contributors,
)
toc = read_canonical_manifest(data_dir, version["version_id"])
chapter = read_canonical_section(data_dir, version["version_id"], section_ordinal)
```

`preserve_canonical` returns `version_id`, absolute manifest `path`,
`section_count`, `canonical_bytes` and `stored_bytes`. Version IDs have the strict
form `cv1-<64 lowercase hex characters>`. `canonical_bytes` counts uncompressed
UTF-8 JSON for every logical section plus its manifest. `stored_bytes` counts
the compressed objects uniquely referenced by this version plus its manifest;
it includes already-existing shared objects and is not newly allocated disk.

`parser_fingerprint` and `indexer_fingerprint` are independent optional SHA-256
values. Both may be `None`: unknown provenance remains explicit. In particular,
do not label a combined indexer digest as a parser digest or claim that today's
loaded parser produced an older SQLite snapshot. Supplied hashes are validated
and stored; this helper cannot establish whether caller-supplied provenance is
historically correct. The canonical payload digest identifies the saved strings.

The book header allows only `title`, `author`, `language`, `edition`, `source_url`,
`rights` and `rights_url`. No arbitrary metadata, secrets or user notes are
accepted. `section_metadata` is either a list aligned exactly with all sections,
or a mapping from zero-based section ordinals. Its whitelist is
`section_author`, `section_role`, `section_translator`, `attribution_basis`.
Values must be exact strings; omit unknown/null SQL fields. Roles are the
project's `main_text`, `editorial`, `unclassified`, or empty. No contributor is
inferred from a heading, translation or book author. Attribution changes create
a new version while unchanged text objects remain shared.

The manifest reader returns the verified book/provenance header and a TOC with
section ordinal, title, payload SHA-256, bytes, character count and `metadata`.
The section reader returns the same header plus `section`: ordinal, title, exact
text, SHA-256 and explicit contributor fields. It decompresses only that section.
Offsets in returned text are Python Unicode code points; non-NFC Unicode,
emoji, CR/LF, tabs, indentation and trailing newlines are preserved verbatim.

## Storage and integrity

```text
canonical/versions/<sha-prefix>/<manifest-sha>.json
canonical/sections/<sha-prefix>/<section-sha>.json.gz
```

JSON is UTF-8 with sorted keys and fixed separators. A section hash covers its
decompressed JSON containing schema, title and text; ordinal and attribution
belong to the manifest. The version hash covers the entire manifest, binding
ordered sections, provenance and bibliography. Gzip uses a blank filename and
`mtime=0`; the content identity does not depend on compressed byte encoding.

Publication writes an owned same-directory temporary, flushes/fsyncs, verifies
it and atomically creates the final name without replacement. Concurrent writers
reuse verified objects. A manifest is published only after all its sections are
available. Existing corruption is rejected and never overwritten. Direct files
and storage directories must not be symlinks. Readers verify file bounds,
decompression, payload hash, canonical encoding, strict fields and the requested
section's agreement with its manifest.

Limits: 100 MiB aggregate uncompressed JSON, 20 MiB per section, 16 MiB manifest,
100,000 sections, and 64 KiB per book/contributor header field. Empty sections are
preserved, but a book must contain at least one section. Oversized or malformed
input raises `ValueError`; corruption raises `CanonicalIntegrityError`.
Absent objects raise `FileNotFoundError`; out-of-range ordinals raise `IndexError`.

Owned temporaries are removed on exceptions, including interruption. Valid
content-addressed sections or versions completed before a later failure remain
reusable; removing them could break concurrent references. Process termination
or transaction rollback may therefore leave unreferenced immutable objects.
Garbage collection, retention, backups and quota management are not implemented
here. Preserve this directory with saved-version references when backing up.

## Verification and local measurement

```sh
uv run pytest tests/test_canonical_versions.py
```

Checks cover exact non-NFC Unicode/newlines, explicit translator/editorial
metadata, deterministic identities, deduplication, earlier versions after text
or parser changes, strict paths and bounds, gzip/hash corruption, malformed
manifests, isolated section loading, concurrent publishers and failure cleanup.

A temporary original CC0 synthetic book with 40 sections contained 7,938,253
canonical bytes and used 218,110 stored bytes. Initial preservation took
203.058 ms; 25 warmed reads of section 23 had p50 2.049 ms and p95 2.093 ms. That
read loaded a 6,783-byte manifest and a 198,737-byte section payload rather than
decompressing all sections. Repetitive fixture text compresses unusually well;
these local macOS measurements are not a corpus compression ratio or a cold-I/O
guarantee. The source-text fidelity and rights of an edition remain separate
from byte integrity of its captured canonical context.
