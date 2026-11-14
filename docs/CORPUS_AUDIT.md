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

## Table poetry and heading recovery, 2026-11-14

A fresh import replay found a concrete extraction regression in Gutenberg
**#10912, *The Infant's Delight: Poetry***. The cached 3,873,124-byte EPUB has
source SHA-256
`0b2a28bda9cd1bac75fe88d9bc31ba7671aa8980eab0724e63095d3bea8b8dea`.
Its main narrative XHTML contains **50 tables, 50 rows and 50 cells**, with poem
lines stored directly in `TD` elements and separated by `BR`. The prior parser
ignored tables and retained whitespace-only `PRE` blocks (including NBSP).
Those blocks incorrectly made the section list appear nonempty, disabled the
fallback, and were then discarded by the final whitespace filter. The book
failed with “No extractable text” despite containing ordinary selectable text.
This is a format handling defect, not a scan or an OCR requirement.

The corrected HTML extractor rejects whitespace-only blocks before section
creation and owns each table subtree once. It retains caption/header text,
logical row and cell order, explicit BR verse breaks and nested block text.
Cells are separated by TAB, rows by blank lines. Empty span positions preserve
basic column association without copying a cell's words into multiple places.
Nested paragraphs, lists and layout tables are emitted once. A containing
quotation retains its own text before and after an embedded table. Explicit
`PRE` code inside a cell preserves its indentation, tabs, internal spaces and
string literals. Ordinary non-table prose normalization is unchanged.

This remains a linear text representation, not a facsimile or an interpreted
scientific spreadsheet. Complex merged-cell layout and nested column geometry
are not guaranteed. Spans are bounded to 100 positions and nested table-specific
rendering to 16 levels, followed by readable text fallback. Images and visual
formulas are not acquired or reconstructed by this parser change.

The same source exposed a separate heading loss: a poem title followed
immediately by an H2 biblical epigraph overwrote the first pending heading.
Consecutive headings now remain as their original strings joined by LF in the
section title, without invented labels or added attribution. The first ordinary
H1→H2 chapter can therefore have a composite title; its body text stays unchanged.
Later chapters remain separate. A trailing heading-only fragment is retained as
source text instead of creating an empty section or disappearing.

The first poem's title, `BLIND MAN'S BUFF.`, uses a styled DIV instead of a heading.
A narrow audited gate recognizes the source's `style1` + `xhtml_div_align` classes,
short uppercase text and an immediately following nonempty structural table
block. It stops at an intervening heading/prose block and does not classify
generic layout DIVs as titles. This preserves actual source words and does not
infer a contributor from formatting.

### Real-source validation

The corrected cached EPUB yields **50 nonempty sections and 25,938 body
characters**. All **57 original H1–H6 headings** occur in the resulting section
titles/text, and the source's visible-word multiset has **zero missing tokens**
after the same provider boilerplate exclusion. The first title includes both
`The Infant's Delight` and `BLIND MAN'S BUFF.`. The first verse is present with
its explicit source line breaks:

> When the win-ter winds are blow-ing,

This was measured against the original local EPUB; source bytes were unchanged.
The shorter canonical character count than a previous index entry is not used
as an extraction quality score: canonical whitespace, heading placement and
source boilerplate filtering affect that count.

The pinned D2L artifact was also replayed: **483/483 source code and math blocks**
remain exact substrings, with **148 sections and 413,906 body characters**,
unchanged. Its explicit FB2 code path was not edited. No frozen evaluation
snapshot was modified.

All 20 curated full EPUBs parsed successfully. Their flat section total changed
from **1,614 to 1,619** as formerly hidden tables and their headings became
available. **1,594 new sections** retain an exact previous body hash. Remaining
canonical changes concern newly retained source text, table whitespace/order or
heading-only fragments. These parser changes require a versioned reindex and a
new corpus fingerprint before any fresh retrieval comparison.

Evidence and the exact prior parser source are saved under ignored
`data/audit/table-parser-v1/`. `report.json` records raw source hashes, old/new
parser fingerprints, every curated section-title comparison, recovered poem
structure and the D2L literal-block checks. The current parser fingerprint is
`00ad86bd421ff2d9fc0dc17331602a0f7031b3bbeb419ae717a4c8d97b6fc44e`.

### Attribution selector regression audit

The parser replay evaluated all **13 artifact-bound attribution rules** against
the previous parser and the new extraction, plus the independently curated
Jowett introduction rule. An unchanged source-artifact hash cannot establish
that a title selector retains its old scope after heading handling changes.

**One artifact-bound rule needs explicit scope review:** Gutenberg #1661,
*The Adventures of Sherlock Holmes*, uses the book title as its selector. It
previously matched one section and now matches two:

- the source's notice about an illustrated edition;
- the recovered Contents table under the composite title
  `The Adventures of Sherlock Holmes` / `by Arthur Conan Doyle` / `Contents`
  (actual title strings are LF-separated, without the display slashes).

Neither new match is a narrative story, but the rule's count and matched body
hashes changed. The audit flags both bindings for review; it does not authorize
or automatically accept the wider selector. No rules, source manifests or
library attribution rows were changed by this work.

The other **12 artifact-bound rules** retain their match counts and exact
matched bodies. Jowett's `INTRODUCTION AND ANALYSIS` retains **three** matches.
No rule gained a match on a previously unmatched, otherwise unchanged body.
The full old/new titles and previews of changed bindings are in the runtime
report. Review #1661's selector scope before a blanket refresh; retain manual
curation precedence and make any changed attribution decision separately.

### Verification

The parser suite now has **33 tests**, including the minimal H1 + blank PRE +
TD/BR poem regression, scientific headers/values and row associations, nested
layout tables, P/LI duplication, quotation siblings, cell PRE code, spans,
consecutive poem/epigraph headings, ordinary H1/H2 chapter structure and the
narrow styled-title gate. Ruff and ty pass. Parser/modern-source/Russian-source/
attribution tests pass **77/77**, and ingestion/integration/integrity/library
regressions pass **28/28**. Measurements were read-only with respect to library
and evaluation databases.
