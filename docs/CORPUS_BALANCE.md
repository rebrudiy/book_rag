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
