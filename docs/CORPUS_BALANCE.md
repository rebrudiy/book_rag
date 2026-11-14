# Balanced corpus planning

The first large queue followed the provider catalog's order and was strongly
English and fiction weighted. `book_rag.corpus_planning.plan_balanced_corpus`
creates a separate reproducible acquisition queue balanced across requested
languages and coarse subject areas. It reads the existing official offline RDF
catalog; it does not crawl websites or download books.

## Contract and use

```python
from pathlib import Path
from book_rag.corpus_planning import plan_balanced_corpus

report = plan_balanced_corpus(
    Path("data/catalog/rdf-files.tar.bz2"),
    Path("data/catalog/balanced.json"),
    per_language=100,
    languages=["en", "ru", "fr", "de"],
)
```

The default languages are English, Russian, French and German. `per_language`
is an edition quota per language, not a claim about verified unique works.
The function atomically writes a normal import manifest and a sibling
`balanced.report.json` with availability, filtering reasons, selected counts,
subject distributions, language shortages and the source catalog's SHA-256.
An existing plan is deterministically replaced; ingestion can resume separately
using the normal source lifecycle. Runtime queue and report files belong under
ignored `data/`.

A queue represents **discovered catalog editions**. It does not establish that
books have downloaded, parsed successfully, passed local rights review or become
searchable. Separate those stages in progress displays.

## Eligibility and identity

Each source must explicitly state `Public domain in the USA`, have catalog type
`Text`, provide a supported EPUB or UTF-8 TXT file, and match a requested source
language. Other rights statements and missing rights are excluded. These are
provider determinations about US copyright; the existing per-source regional
rights caveat remains attached. Metadata is reused from the official offline
archive identified in [SOURCES.md](SOURCES.md).

Explicit periodical issues are excluded using catalog subjects and clear
volume/issue metadata. This is conservative classification, not proof that every
remaining record is a monograph: collections, volumes, dictionaries, letters and
other textual units can remain `book_or_text`.

Source IDs remain `pgN-full`; manually verified work links are retained. Known
curated identities have `work_identity_status: curated`; others remain
`provisional`. Distinct provisional IDs cannot be reported as a verified
unique-book or unique-work count.

When merging with an existing acquisition queue, retain explicit edition and
contributor curation from `corpus/manifest.json` for matching IDs. The generic
RDF-derived plan does not replace the manually verified translator/edition
notes or Republic's Jowett attribution rule. Keep curation separate from
provider metadata so a later generic catalog refresh cannot erase it.

## Subject labels and selection

Eight coarse areas are used: fiction, philosophy, history, science, economics,
biography, technical and other. Labels derive only from explicit catalog subject
terms. Each selected record stores the matched terms and rule version. Titles,
author reputation and model-generated guesses do not supply subject labels.
Unmatched or insufficient metadata stays `other`.

This is a transparent acquisition heuristic, not a scholarly classification of
a book's central argument. The fiction grouping includes explicitly tagged
poetry and drama; literary criticism and nonfiction markers do not alone
qualify as fiction. Overlapping subjects remain in the evidence metadata, while
an explicit deterministic priority chooses one primary area for quota counting.

Selection retains the lowest stable SHA-256 priorities in a bounded reservoir
for each language/primary-subject pair. Each reservoir holds at most
`per_language` entries. Memory therefore depends on requested quotas and subject
areas, rather than retaining the whole catalog's book metadata. No embeddings,
models or paid services are involved.

Languages with the least eligible source availability are assigned first.
Multilingual source editions may qualify for several language pools, but an ID
is emitted only once. `planning_language` records its assigned quota; the
original complete language list remains in `languages`. This is an assignment
to a listed source language, not verification that every paragraph is written
in that language.

Within each language, selection fills available subject areas round-robin.
Unavailable subjects spill over to other subjects within that language. A
Russian shortage is reported rather than silently filled with more English
books. Changing input traversal order does not change the selected IDs.

## Real catalog measurement

On 2026-11-14, the existing **121 MiB** official RDF archive was scanned once:
**79,811 records**, catalog SHA-256
`86a11d5cf64ac9bf3f8c59cbc6bdbe505185884c66d84a0930be48b02f6d31a3`.
Planning with 100 editions per language selected **305 editions** in
**81.392 seconds**, with **93.91 MiB** peak process RSS. The planner retained
1,796 candidate slots against a bound of 3,200. These measurements exclude
network acquisition and book indexing.

| Language | Eligible catalog records | Selected editions | Unfilled quota |
| --- | ---: | ---: | ---: |
| English | 57,992 | 100 | 0 |
| Russian | 5 | 5 | 95 |
| French | 3,929 | 100 | 0 |
| German | 2,371 | 100 | 0 |

Availability counts are per eligible catalog record and language; multilingual
records can occur in more than one availability count. Output IDs are
deduplicated. The five eligible Russian source records demonstrate a genuine
provider coverage gap under these filters. Reaching the Russian quota requires
another separately verified source; additional English records do not solve it.

| Primary area | English selected | Russian selected | French selected | German selected |
| --- | ---: | ---: | ---: | ---: |
| Fiction | 13 | 2 | 15 | 15 |
| Philosophy | 13 | 0 | 15 | 15 |
| History | 13 | 0 | 14 | 14 |
| Science | 13 | 1 | 14 | 14 |
| Economics | 12 | 0 | 14 | 10 |
| Biography | 12 | 0 | 14 | 14 |
| Technical | 12 | 0 | 0 | 4 |
| Other | 12 | 2 | 14 | 14 |

French has no eligible records matching the conservative technical rules;
German has four. These statements concern the catalog fields and current rules,
not the absence of technical literature in those languages. Missing or broad
subject metadata also accounts for substantial `other` populations.

Filtering excluded 4,214 explicit periodical issues, 1,251 non-text records,
425 records without the required rights statement, 9,527 records outside the
requested languages, 132 without a supported format and one invalid record.
The remaining 64,261 eligible records can contribute to language/subject pools.
The queue is ready at `data/catalog/balanced.json`; its detailed audit report is
`data/catalog/balanced.report.json`. No book downloads were made for this plan.

## Comparison with the initial 1,000-edition queue

The existing `data/catalog/discovered.json` queue had **942 English**, 37 French,
12 German and nine Spanish entries, **zero Russian**, and **61 explicit
periodical issues**. Applying the same subject rules to its metadata gave 492
fiction, 341 other, 99 history, 48 biography, nine philosophy, eight science,
two economics and one technical record. This is a queue comparison rather than
a comparison of successful searchable imports.

The balanced plan reduces dependence on catalog order, improves language and
subject coverage, and makes unavailable quotas visible. It also gives a useful
next acquisition priority: a licensed Russian textual source and modern science
content with richer metadata. The pinned D2L acquisition addresses a portion of
the modern technical gap separately; it is not part of the Gutenberg counts.

## Verification

Tests cover explicit rights gates, periodical exclusion, allowed formats,
metadata-grounded classification, provisional work identities, language
shortages, scarce multilingual assignment, unique source IDs, subject spillover,
ordering determinism, bounded reservoir retention and absence of HTTP access.

## Resource-aware text-only expansion

`plan_balanced_corpus(..., text_only=True)` adds a separate format policy for
larger local acquisition queues. The default remains `False`: the existing
EPUB/UTF-8 TXT choice, stable priorities, quotas and output entry metadata are
preserved. A real default-mode rerun of the previous 305-edition plan took
58.344 seconds and reproduced its manifest **byte for byte**, SHA-256
`d060171c0b7cbd1218296788c2dc4fb603c708bc53e4e17a8cf0705d1cdc54fc`.
The compatibility output is separate at `data/catalog/balanced-default-compat.json`;
the live `balanced.json` queue was not rewritten.

```python
report = plan_balanced_corpus(
    Path("data/catalog/rdf-files.tar.bz2"),
    Path("data/catalog/balanced-text3000.json"),
    per_language=1000,
    languages=["en", "ru", "fr", "de"],
    text_only=True,
)
```

The text-only order is explicit no-image EPUB, explicitly UTF-8 TXT, then textual
HTML. Image EPUBs, PDF/scans, unknown plain-text encodings, other hosts, HTTP,
wrong Gutenberg numbers, traversal paths, credentials, ports and query strings
are excluded. A source still needs the same explicit rights, type, subject and
language gates. HTML fallback can add a few eligible records that the default
EPUB/TXT-only mode excludes; the report exposes the resulting availability.
An image-only record is skipped rather than silently chosen when no text
alternative exists.

This mode requires original RDF metadata or an RDF archive, rather than the CSV
fallback, to retain exact per-record evidence. It streams each raw RDF member,
computes its SHA-256 and discards its XML after candidate processing. It does not
extract archive paths. Bounded reservoirs and language/subject assignment are
unchanged. Runtime grows with the catalog; retained metadata grows with quotas,
not the total record count.

Every selected entry records:

- `illustrations_included: false` and
  `coverage_scope: text_only_without_illustrations`;
- `source_variant.format_family`, exact `catalog_format_url`, media type,
  catalog/member SHA-256, same Gutenberg ID and source rights;
- the original image-EPUB format URL where the RDF lists one, and an explicit
  omission list covering illustrations, image-embedded text/labels, visual
  equations and diagrams;
- a proposed status requiring download, parsing and textual citation checks.

Direct format links are used exactly as listed in RDF. The sole generated URL
exception is Gutenberg's existing verified `/ebooks/N.epub.noimages` alias
mapping to `/cache/epub/N/pgN.epub`, already implemented in `catalog._download_format`.
The exact RDF alias remains in `catalog_format_url`; `url_resolution` identifies
this mapping. No other download URL is guessed, and no provider request is made
to discover formats. This avoids an extra redirect request for each generated
EPUB while retaining its catalog provenance.

`coverage: full` here describes the planned complete **textual edition**, subject
to later acquisition/parse validation and the explicit omission scope. It does
not promise a facsimile, illustrations, or complete mathematical information
contained only in images. Scientific and technical interpretation can depend on
those missing figures or visual formulas. EPUB preserves available textual
chapter/footnote structure; TXT offers less structure, and external HTML images
are not acquired. No OCR or automatic visual replacement is performed.

### Measured 3,005-edition plan

On 2026-11-14, the same official RDF snapshot produced the following separate
queue in **70.911 seconds**, with **203.73 MiB** peak process RSS. That peak covers
the sequential compatibility and text-only planning process; it is not an
indexing or download memory measurement. The planner retained **10,893 candidate
slots** against a **32,000-slot bound**.

| Language | Eligible text-only catalog records | Selected editions | Unfilled quota |
| --- | ---: | ---: | ---: |
| English | 57,996 | 1,000 | 0 |
| Russian | 5 | 5 | 995 |
| French | 3,929 | 1,000 | 0 |
| German | 2,371 | 1,000 | 0 |

The format distribution is **2,997 no-image EPUBs and 8 explicitly UTF-8 TXT
files**; no HTML file won selection. All 2,997 EPUBs use the documented generated
alias mapping; the eight TXT URLs are direct `/files/N/N-0.txt` links from RDF.
All 3,005 entries have exact raw RDF record hashes. The mode admits 64,265
eligible catalog records versus 64,261 previously; four additional English
HTML-eligible records account for the difference.

| Primary area | English | Russian | French | German |
| --- | ---: | ---: | ---: | ---: |
| Fiction | 125 | 2 | 254 | 341 |
| Philosophy | 125 | 0 | 42 | 51 |
| History | 125 | 0 | 253 | 129 |
| Science | 125 | 1 | 38 | 54 |
| Economics | 125 | 0 | 21 | 10 |
| Biography | 125 | 0 | 139 | 71 |
| Technical | 125 | 0 | 0 | 4 |
| Other | 125 | 2 | 253 | 340 |

Scarce subject pools spill over within the same language. These catalog labels
still describe explicit metadata, not independently verified subject expertise.
The Russian shortfall persists and is not hidden by expanding English quotas.
The separately acquired Wikisource works remain outside these Gutenberg counts.

All **305 IDs** from the previous balanced plan recur, with **2,700 additional
IDs**. Reusing an ID does not prove that its older image EPUB cache is the same
artifact as the new no-image URL. Keep a valid existing searchable edition unless
an explicit migration is wanted; preserve manual contributor/edition curation
when merging queues. If changing a source URL, use the provenance-aware download
cache and reindex the corresponding text instead of relabeling an old artifact.
The planner does not perform that merge or alter the live library.

Artifacts under ignored `data/catalog/`:

- `balanced-text3000.json`: the separate acquisition manifest;
- `balanced-text3000.report.json`: availability, quotas, subject/format counts,
  filtering reasons and catalog provenance;
- `balanced-text3000.measurement.json`: elapsed time, process RSS and overlap
  with the earlier plan.

No books were downloaded or indexed during this experiment. Source-file sizes,
aggregate download size and index disk requirements are **unknown**;
`artifact_sizes.estimated_bytes` is null. Choosing formats without illustrations
reduces one known oversize-file cause but cannot guarantee that every artifact
fits the existing unchanged size cap or available disk.

The planner now has **40 offline tests**, including no-image preference,
TXT/HTML fallback, image-only exclusion, strict same-record/provider URL checks,
rights/type/language gates, explicit omission flags, raw streamed RDF hashes,
unchanged default behavior and deterministic selection. Ruff and ty pass.
