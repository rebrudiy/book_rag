# Corpus import audit, iteration 1

Evidence: `experiments/corpus-audit-v1.json`, read-only snapshot of
`data/full/library.sqlite3` at **2026-11-14 11:25:20 UTC**. Background ingestion
continued afterwards, so these are fixed audit counts, not the current library
size. The audit did not modify book files, the database or parser behavior.

## Scope and results

The snapshot contained **66 indexed editions**, 65 distinct catalog-derived work
IDs, 3,054 sections and 21,410 passages. Languages were English 63, French one,
German one and Russian one. Another 154 source records were discovered but not
yet indexed. All indexed records carried explicit US public-domain rights.

All **21,410/21,410 passages** equalled their stored section's `[start:end]` slice;
all stored passage SHA-256 values matched. These checks establish internal
citation integrity against canonical text. They do not establish correspondence
to printed page numbers or prove every extraction decision faithful to layout.

The curated 20 full EPUB editions were replayed through the current parser.
**19/20 canonical hashes differed** from the first indexed versions despite
unchanged source artifacts. The earlier parser inserted spaces between inline
markup and flattened verse; the revised parser preserves inline adjacency and
explicit verse breaks. The findings were reported to the lead before reindexing.

**Required correction:** resumable import must compare the loaded parser and
chunker versions as well as source bytes and metadata. An artifact hash alone
cannot prove that an old index uses current extraction rules. A parser
fingerprint is now exposed; index migration/rebuild is tracked separately by the
lead. Preserve older experiment results with their corpus fingerprint.

## Structure and source fidelity

| Sample | Stored sections | Observed structure |
| --- | ---: | --- |
| Carroll, Alice, #11 | 13 | Opening matter and twelve labelled chapters |
| Kafka, German original, #22367 | 4 | Opening matter plus I, II, III |
| Voltaire, French Candide, #4650 | 37 | Print frontmatter, editor preface, thirty chapters |
| Pushkin, Russian poem, #5316 | 1 | Source poem with malformed title split in provider HTML |
| Darwin, first Origin edition, #1228 | 19 | Contents, introduction, fourteen chapters and index |
| Einstein, Relativity, #5001 | 41 | Numbered sections and appendix divisions |

Footnote markup was inspected directly inside the corresponding source EPUBs.
After ignoring whitespace for comparison, **50/50** identified Candide notes,
**30/30** Einstein notes and **39/39** Meditations notes were present in the stored
text. The audit identified notes by explicit footnote IDs/classes/roles. These
are counts of recognized markup, not a claim that every possible note notation
was found. Hyperlink/backlink relationships and note-to-anchor navigation are
not yet represented in the data model.

No full Gutenberg license/start/end banner was found by the recorded marker
heuristic. However, **five books** retained a short legacy production tail:
`pg10004-full`, `pg10029-full`, `pg10037-full`, `pg10039-full` and `pg10040-full`.
For example, the last paragraph of *The Warriors* is a provider end-of-book
notice. Remove these narrow, identifiable production paragraphs from the search
text on a later parser revision; keep the original bytes and rights notice in
source storage. The audit's heuristic is anchored to paragraph starts, avoiding
false positives from ordinary prose containing “produced by”.

The source itself can be defective. In #5316, the HTML heading contains the first
part of the title and the remaining word appears as the poem's first line.
Catalog metadata preserves the correct full title, while the reader section
reflects that source heading. This should be annotated as a source defect or
resolved against a better verified edition. Do not silently invent corrected
quotation text.

Several headings include print illustration captions. In #1342, a chapter title
contains a caption before its chapter number. Repeated introduction headings in
#1497 and fine-grained subheadings in Hobbes #3207 show that the current flat
section list cannot represent chapter hierarchy. Add heading depth, source node
IDs and a chapter path while retaining original text and stable offsets.

## Authorship, editions and work identities

A book-level author field is insufficient for author-position comparisons.
Plato's #1497 includes Benjamin Jowett's explicitly titled introduction and
analysis. Citations currently name the book's author, Plato; the prose in these
editorial sections must not be silently presented as Plato's own argument.
Introduce an evidence role (`body`, `editorial introduction`, `translator note`,
`index`, `bibliography`) and contributor attribution. Keep editorial material
available to search, but make role and contributor visible and filterable.

The oldest 20 imports lacked extended `book_metadata` rows in the snapshot.
Their core title, author, language, edition, source and rights columns remained
present. The current registration path backfills extended metadata when these
sources are revisited. Verify that backfill during the rebuild rather than
reporting the metadata migration complete from code inspection alone.

**65 work IDs is not proof of 65 unique book works.** Thirteen imported records
were periodical issues: twelve issues of *Punchinello* and one issue of *The
Mirror of Literature*. A volume of Aphra Behn's collected works is another
aggregate unit. Preserve edition/source IDs but distinguish monographs,
collections, volumes and periodical issues. A catalog `Text` type is too broad
to guarantee a book-level corpus target.

Darwin #1228 and #2009 are explicitly linked as first and sixth editions of the
same work; their text should remain separately citable. Other default `pgN`
work IDs are conservative catalog identities. They have not been resolved
across translations and editions. Exact canonical-content deduplication cannot
alone solve that problem: editorial matter, spelling changes and parser
versions change hashes. Use author/title/translator metadata to propose links,
then store verified relationships and provenance. Do not merge on title alone.

## Recommended next source: modern technical nonfiction

**Dive into Deep Learning** is a useful next acquisition: machine learning,
mathematics, optimization, NLP, transformers and related concepts, with an
established Chinese edition and translations. The [official project](https://d2l.ai/)
and [source repository](https://github.com/d2l-ai/d2l-en) provide structured
chapters and downloadable content. The book text uses
[CC BY-SA 4.0](https://github.com/d2l-ai/d2l-en/blob/v1.0.3/LICENSE); sample code has
[a separate license](https://github.com/d2l-ai/d2l-en/blob/v1.0.3/LICENSE-SAMPLECODE).

Pin English source tag `v1.0.3`, commit
`b2e2ae30898a9d0126a9699ae7e441de3e272715`. An HTTP HEAD request to the
[official PDF](https://d2l.ai/d2l-en.pdf) returned `application/pdf`, 44,685,994
bytes and a last-modified date of 2023-08-18. This verifies a downloadable
candidate, not completed acquisition. The PDF's internal edition/license still
needs inspection before indexing. Structured source ingestion is preferable for
math, code, section links and multilingual edition relationships. Preserve
credit, license, source commit and modification notices; retained adapted text
must carry the appropriate share-alike terms. None of this changes the software's
MIT license.

OpenStax is another promising source, but do not assume a provider-wide CC BY
license. Its [current licensing help](https://help.openstax.org/s/article/Licensing-information-of-OpenStax-textbooks)
states CC BY-NC-SA, while older book-specific pages and downloadable artifacts
can state CC BY. Verify and freeze each chosen artifact's own terms. This
contradiction is a reason for per-edition rights metadata, not a reason to infer
the most permissive license.

## Next checks

1. Rebuild with parser/chunker fingerprints and rerun citation/metadata checks.
2. Add narrow production-tail stripping and evidence-role attribution tests.
3. Separate catalog identity counts from verified work/volume/periodical counts.
4. Acquire one pinned modern technical book with its actual embedded license,
   then evaluate math/code extraction and cross-language queries independently.
