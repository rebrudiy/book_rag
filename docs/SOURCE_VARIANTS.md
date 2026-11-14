# Recovering an oversized source through verified format alternatives

An image-heavy Gutenberg EPUB can exceed the project's 100 MiB artifact limit
while another electronic format of the same edition remains usable. The
`source_variants` helper proposes alternatives from the existing official RDF
catalog. It does not increase the limit, download anything, modify a library or
mark a failed book searchable.

## Contract

```python
from pathlib import Path
from book_rag.source_variants import propose_variants

alternatives = propose_variants(entry, Path("data/catalog/rdf-files.tar.bz2"))
```

`entry` is an existing Gutenberg source manifest entry. The result is a list of
candidate entry dictionaries, ordered as follows:

1. Explicitly identified EPUB without illustrations.
2. Explicitly UTF-8 plain text.
3. A textual HTML page, without downloading its external images or styles.

Every candidate preserves the book ID, work ID, original electronic-edition
credit, source URL, rights and curation such as section-contributor rules. It
changes `download_url` and `format`, sets `illustrations_included: false`, and
records `coverage_scope: text_only_without_illustrations`. Existing coverage
metadata describes the source's textual coverage; it does not imply that the
illustrated edition's visual information survives.

`source_variant` records the rationale, original download URL, exact RDF format
URL/media type, same Gutenberg ID, catalog-member path, record/catalog hashes,
structure limitations and required validation. A candidate remains
`status: proposed` until the caller actually validates it.

Artifact-specific fields such as `local_path`, old artifact/content hashes,
status and error are removed from the candidate and retained under
`source_variant.original_artifact`. This prevents a downloader from reusing an
old image-heavy local file merely because it was attached to the source entry.
The original input dictionary is not modified.

## Proof and bounds

The source URL must identify an official HTTPS Gutenberg ebook record. A
numbered book ID must agree with that record. Input provenance must explicitly
state US public-domain rights, and the matching RDF record must independently
state `Public domain in the USA` with type `Text`. Missing, copyrighted or
ambiguous rights do not yield a candidate. Neither author age nor general
provider reputation supplies missing permission.

Only HTTPS URLs listed in that exact ebook's RDF record are returned. Their
host and file/ebook path must identify the same Gutenberg number. Different
books, external hosts, credentials, custom ports, traversal paths and unverified
encodings are rejected. No download URL is guessed from a filename pattern.
Known alias/cache equivalence is used only to avoid retrying the format family
already selected. One preferred RDF-listed URL is retained per family, favoring
direct cache/file links over aliases when both are explicitly present.

For an archive, the helper streams members until `pgN.rdf` is found and parses
only that target metadata record. It does not extract archive paths to disk or
retain all catalog records. Individual metadata records retain the existing
size bound. PDFs, scans, OCR, image archives, Kindle files and ambiguous
image-bearing EPUB alternatives are outside this recovery path.

The existing regional rights caveat remains attached. Switching file formats
does not establish worldwide copyright permission, a new translation or a
newly verified underlying work identity.

## Observed failure: Gutenberg #63371

The balanced ingestion encountered an oversized image EPUB for
*Lehrbuch der Botanik für Hochschulen*, source ID `pg63371-full`. Offline metadata
identifies authors Eduard Strasburger, Fritz Noll, Heinrich Schenck and Andreas
Franz Wilhelm Schimper, language German, subject `Botany -- Textbooks`, and
explicit US public-domain rights.

The cached official RDF record lists these proposed alternatives:

| Priority | Format | Exact catalog URL |
| --- | --- | --- |
| 1 | No-illustration EPUB | [63371.epub.noimages](https://www.gutenberg.org/ebooks/63371.epub.noimages) |
| 2 | UTF-8 TXT | [63371.txt.utf-8](https://www.gutenberg.org/ebooks/63371.txt.utf-8) |
| 3 | HTML text | [63371-h.htm](https://www.gutenberg.org/files/63371/63371-h/63371-h.htm) |

These are proposals, **not verified downloads or size guarantees**. The catalog
also lists an older text file labelled US-ASCII; it is not substituted for the
explicit UTF-8 format. The HTML's image references do not authorize a separate
recursive asset download by this helper.

Target RDF SHA-256:
`706baebede999c61ebd1d26c329dfe701b061ea8621d59722fa6317615b0bd3a`.
Catalog SHA-256:
`86a11d5cf64ac9bf3f8c59cbc6bdbe505185884c66d84a0930be48b02f6d31a3`.

The actual offline proposal completed in **18.230 seconds** without HTTP
requests. Its runtime report is `data/catalog/pg63371-variants.json`. No requests
were sent to Gutenberg while the main provider ingestion was active.

## Integration sequence

The caller should try the ranked candidates after the current provider job
finishes, using the existing aggregate pacing and cache/provenance controls.
Download into a separate trial directory so a failed alternate does not replace
a previously completed source artifact.

A candidate must fit the unchanged size limit, parse successfully and pass
chapter/citation checks before its metadata is adopted. Verify headings,
footnotes, scientific tables and references to figures especially carefully;
plain text loses layout and links, while absent illustrations can carry
information essential to a technical explanation. Preserve the actual downloaded
URL, final URL and new byte hash. Show the text-only scope to readers.

Only then should normal import use the same source/book identity with the
validated alternate artifact. Keep the prior failure and the format-change
reason in the audit trail. If every candidate fails, leave the source explicitly
failed or unavailable for search. No integration, download or recovery success
is implied merely by this proposal helper returning candidates.

Tests cover exact RDF URL provenance, same-book identity, restrictive rights,
provider/path checks, alias deduplication, stale-artifact clearing, streaming
archive access, excluded formats and absence of network access.
