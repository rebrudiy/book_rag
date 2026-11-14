# Russian source strategy: a bounded Wikisource acquisition

Research checked on 2026-11-14. The Gutenberg balance audit found only five
eligible Russian textual records. Russian Wikisource provides a practical next
source for complete nineteenth-century originals, with page-level revision
metadata and explicit reuse terms. This document separates original literary
rights, modern transcription/editorial rights and responsible access.

## First five complete works

The selection contains works first published during their authors' lifetimes,
not newly published posthumous manuscripts. Dates below are supported by the
linked work pages and author credits. A cited modern collected edition is the
transcription's source, not the work's first publication date.

| Work | Original publication | Proposed source and coverage |
| --- | --- | --- |
| Tolstoy, *Смерть Ивана Ильича* | 1886 | [One complete page](https://ru.wikisource.org/wiki/Смерть_Ивана_Ильича_(Толстой)), chapters I–XII; narrative before later editorial notes |
| Dostoevsky, *Записки из подполья* | 1864 | [Work index](https://ru.wikisource.org/wiki/Записки_из_подполья_(Достоевский)); Part 1 chapters 1–11 and Part 2 chapters 1–10 |
| Gogol, *Шинель* | 1842 | [Specific electronic edition](https://ru.wikisource.org/wiki/Шинель_(Гоголь)/ПСС_1938_(СО)), complete narrative; the unqualified title is an editions index |
| Turgenev, *Отцы и дети* | 1862 | [Work index](https://ru.wikisource.org/wiki/Отцы_и_дети_(Тургенев)), all 28 chapters |
| Chekhov, *Палата № 6* | 1892 | [Work index](https://ru.wikisource.org/wiki/Палата_№_6_(Чехов)), chapters I–XIX |

The Tolstoy page declares a corrected/formatted electronic transcription from
az.lib.ru. Its print-edition identity must not be invented from the first
publication date. Gogol's selected transcription names the 1938 collected
edition, volume 3, pages 139–174. Chekhov's index names the 1977 collected
edition, volume 8, pages 72–126. Turgenev's index credits ilibrary.ru; this must
remain in source attribution. The Dostoevsky chapter sequence is a Wikisource
electronic transcription whose exact wiki revisions define the acquired edition.

## Rights and editorial boundaries

[Russian Wikisource copyright guidance](https://ru.wikisource.org/wiki/Викитека:Авторские_права)
distinguishes public-domain originals, contributor translations and separately
licensed text. The Tolstoy, Gogol and Turgenev source pages display explicit
public-domain notices. The curated originals were first published before 1900;
the authors died in 1910, 1881, 1852, 1883 and 1904 respectively. Record each
work's evidence, publication date and author identity rather than accepting any
page merely because it appears on Wikisource.

[Wikimedia's content terms](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use#7._Licensing_of_Content)
allow reuse of contributor text under CC BY-SA 4.0 with attribution and
share-alike conditions. A page/history link can supply contributor attribution;
retain imported-source credits and modification notices. Public-domain original
content remains public domain; the wiki's contribution license does not create
new copyright in that original. Store both bases explicitly. Non-text media
have separate terms and are outside the first connector's scope.

Modern scholarly notes must not be treated as the nineteenth-century author's
text. The first connector should retain the complete original narrative and
clearly identified author notes, while excluding later publication-history or
scholarly apparatus when its rights or contributor cannot be established.
For example, Dostoevsky's first chapter has an explanatory note explicitly signed
by him; preserve that note. Tolstoy's page appends later editorial commentary;
isolate the original twelve chapters before it. Record every omitted note or
paratext group and the reason. Do not label the whole 1938 or 1977 collected
edition public domain merely because Gogol or Chekhov wrote the narrative.

## Verified identities and chapter routes

A bounded, serial Action API metadata probe returned HTTP 200. Observed revisions:

| Page | Revision | Revision timestamp |
| --- | ---: | --- |
| Tolstoy complete page | 5726104 | 2026-06-28T14:51:06Z |
| Gogol selected edition | 5631032 | 2025-09-02T02:06:15Z |
| Dostoevsky index | 4715724 | 2023-01-19T02:06:26Z |
| Dostoevsky Part 1, chapter 1 | 1070196 | 2013-09-27T22:47:07Z |
| Turgenev index | 5627479 | 2025-08-31T18:45:13Z |
| Turgenev chapter 1 | 5670497 | 2025-11-07T13:37:01Z |
| Chekhov index | 5680738 | 2026-01-04T07:54:22Z |
| Chekhov chapter I | 5646963 | 2025-09-06T00:15:17Z |

Exact chapter route examples:

- [Dostoevsky Part 1, chapter 1](https://ru.wikisource.org/wiki/Записки_из_подполья_(Достоевский)/Часть_1/Глава_1)
  and [Part 2, chapter 1](https://ru.wikisource.org/wiki/Записки_из_подполья_(Достоевский)/Часть_2/Глава_1).
- [Turgenev chapter 1](https://ru.wikisource.org/wiki/Отцы_и_дети_(Тургенев)/Глава_1).
- [Chekhov chapter I](https://ru.wikisource.org/wiki/Палата_№_6_(Чехов)/Глава_I).

The API's link order is lexical: chapter 10 appears before chapter 2. Use the
curated numeric/part order and verify the complete expected set. Do not follow
arbitrary external links, author-page links, categories or alternate editions.
The observed indexes expose exactly 21, 28 and 19 chapter links respectively.

A revision citation can use `https://ru.wikisource.org/w/index.php?oldid=REVISION`
for a human-facing permanent link. Such links are not an instruction to crawl
old revisions. A root-page revision alone does not freeze transcluded pages or
templates; captured raw response hashes and per-chapter identities remain
necessary.

## Access policy and acquisition design

Primary policies:

- [Robot policy, updated March 2026](https://wikitech.wikimedia.org/wiki/Robot_policy)
- [Foundation API usage guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines)
- [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)
- [Action API etiquette](https://www.mediawiki.org/wiki/API:Etiquette)
- [Site robots directives](https://ru.wikisource.org/robots.txt)

Use canonical `/wiki/Title` URLs without query parameters for cacheable HTML.
Do not use Action API parsing to retrieve book HTML, crawl oldid/curid website
variants, or enumerate the entire website. Robots directives exclude dynamic
routes from general crawling. Use the documented Action API for bounded batched
metadata, not as a substitute bulk crawler. For large-scale expansion, prefer
[official offline dumps](https://dumps.wikimedia.org/ruwikisource/) and reassess
the acquisition design.

Current robot guidance specifies one concurrent unauthenticated Action API
request and fewer than five requests per second. The proposed connector is
stricter: **one request at a time with a two-second host-wide interval**, including
metadata calls. A response taking over a second is followed by at least five
seconds before another request. Send gzip and an informative user agent such as
`BookRAG/0.1 (https://github.com/rebrudiy/book_rag)`. Use `maxlag=5` for background
metadata. Honor 429, `Retry-After`, maxlag and throttling responses; defer the
host rather than proceeding with another chapter. Limits are global across
Wikimedia properties and may change. These controls are local operating limits,
not a claim of unlimited provider permission.

For each work, obtain batched metadata, fetch its bounded canonical HTML pages,
and establish which revision the HTML actually represents. Prefer an explicit
HTML revision identifier. Otherwise compare fresh metadata before and after
retrieval; record the evidence method and reject a mismatch. A cached response
is a historical captured artifact and should retain its original verification;
do not relabel it with a newer live revision. Store raw HTML SHA-256 even when
revision evidence is available.

## Connector acceptance conditions

A successful work should emit one local book artifact, an import manifest and
per-chapter provenance: canonical URL, wiki page ID, verified revision and time,
provider SHA-1 where available, raw SHA-256, retrieval UTC time, source/edition
credit, rights basis, extraction boundary and omitted material. Keep chapter
headings, original paragraphs, links to retained author notes and the note text.
Credit wiki contributors through page/history links and disclose conversion.

Only label coverage complete after every expected chapter has passed rights,
revision and extraction checks. Missing chapters remain a failed/partial stage
with an explicit reason; they must not be advertised as a full work. Resume
completed cached pages, publish artifacts atomically, and stop further requests
to the host after a deferral. Tests must cover chapter order/deduplication,
external-path rejection, source-rights gates, incomplete sequences, revision
mismatch and exact citation correspondence to stored raw material.

Research fetched a few metadata/pages for proof. It did not perform a bulk
Wikisource acquisition or add downloaded text to the public repository.

## Implemented connector

`book_rag.russian_sources.acquire_russian(destination: Path, limit: int = 5)` now
implements this bounded selection. It returns `manifest: Path`, `works_ready`,
`chapters`, `network_requests`, `cache_hits`, `failures`, and deferral/retry-time
fields. The maximum scope is the five curated works above; the helper cannot
enumerate or crawl arbitrary Wikisource pages.

```sh
uv run python -c 'from pathlib import Path; from book_rag.russian_sources import acquire_russian; print(acquire_russian(Path("data/russian/wikisource"), limit=5))'
BOOKRAG_DATA_DIR=data/full uv run bookrag ingest data/russian/wikisource/manifest.json
```

Run one acquisition for this provider at a time. Rate control applies to the
requests issued by that acquisition; it cannot coordinate unrelated external
processes. The connector preserves a host cooldown in `state.json`, so restarting
it during a provider-requested wait does not issue an early request.

Artifacts stay below the destination:

- `raw/`: verified HTML and batched metadata responses, each with URL, SHA-256,
  byte count and retrieval time;
- `state.json`: discovered/downloaded/verified page stages, partial/ready/failed
  work stages and persisted host deferral;
- content-addressed HTML books with credit, license/modification notices and
  canonical/revision links for each contributing source page;
- per-work provenance with edition credit, original/transcription rights,
  actual verified page revisions and extraction/annotation exclusions;
- `manifest.json`: complete original-work entries only;
- acquisition and verification reports.

The manifest uses `coverage: full` together with
`coverage_scope: original_work_only`. This means the original narrative chapter
sequence and identified author notes, not the complete modern scholarly edition
or every element of a wiki page. `notes_excluded` and
`editorial_sections_excluded` explicitly identify withheld annotations and
editorial boundaries. Failed or incomplete works have partial state but are not
advertised in the import manifest.

### Displayed revisions can differ from latest revisions

The real acquisition exposed an important source behavior. Canonical HTML can
show a displayed/stable revision whose `wgRevisionId` differs from both
`wgCurRevisionId` and the latest revision returned by the metadata API. The
connector does not relabel such HTML with the latest number. It queries revision
metadata by the displayed ID and verifies that the returned title and page ID
match the requested page. Only then is that displayed revision recorded as the
citation target. Unknown revisions or revisions belonging to another page fail.
No oldid HTML request is used for this check.

Five narrative pages required this explicit displayed-revision proof: one in
Turgenev and four in Chekhov. Other narrative pages matched their current
metadata directly. When an HTML identifier is absent, a fresh metadata
before/after match is required instead. Immutable captured HTML hashes remain
the exact extraction source in every case.

### Fidelity checks on the acquired selection

All five originals were acquired and parsed on 2026-11-14:

| Work | Narrative chapters | Parsed sections, including credits/notes | Canonical characters |
| --- | ---: | ---: | ---: |
| Смерть Ивана Ильича | 12 | 13 | 110,174 |
| Записки из подполья | 21 | 23 | 221,578 |
| Шинель | 1 complete narrative | 2 | 65,893 |
| Отцы и дети | 28 | 29 | 361,998 |
| Палата № 6 | 19 | 20 | 106,791 |

Total: **five original works, 81 narrative chapters/units, 87 parsed sections,
866,434 canonical characters**. Character counts include clearly attributed
source-credit material. One explicitly signed Dostoevsky author note is retained;
72 annotations with unverified editorial attribution/rights are recorded as
excluded. Later editorial sections are tracked separately from note counts.
Turgenev's dedication and the Dostoevsky epigraph, including its Nekrasov credit,
are retained; a regression check ensures the verse is not duplicated.

The final cache-only verification reused **75 page accesses**, made **zero HTTP
requests** and completed in **1.130 seconds**. The detailed result is kept at
`data/russian/wikisource/final-verification-report.json`. These are acquisition
and extraction checks; search evaluation and library import are separate stages.

Mocked tests verify licensing/rights gates, complete TOCs and duplicate links,
external host/path rejection, preserved partial stages and resume, revision
mismatch, verified displayed revisions, metadata fallback, signed author notes,
dedication/epigraph credits, deterministic output and persisted host cooldowns.
