# Bounded Russian short-work expansion

The connector in `src/book_rag/russian_short_sources.py` selects ten individual
nineteenth-century originals by eight authors. This is a fixed source selection,
not a Wikisource crawler. It creates a separate import manifest and never writes
the library database or the existing five-work manifest.

The permitted capture is complete: **10 source pages,9 complete original works
and1 explicitly partial edition** are ready in
`data/russian/short-works-v1/manifest.json`. No live import was performed by this
connector task. The initial shared cooldown was respected; acquisition started
after2026-11-14 23:06:28.869486UTC and used the established provider state.

«Человек в футляре» contains its entire verified narrative, but the authorship
of a Greek gloss (`cite_note-2`) is unestablished. That note is omitted and the
entry remains `partial`; it does not contribute to the nine complete works.

## Selection and original-publication evidence

| Work | Author; death | Specific source page | Original publication / source edition |
| --- | --- | --- | --- |
| Пиковая дама | А. С. Пушкин; 1837 | [Single text](https://ru.wikisource.org/wiki/Пиковая_дама_(Пушкин)) | «Библиотека для чтения», 1834, II, I, 109–140. Transcription cites ПСС, Наука, 1978, т. 6, 210–237. Six parts, conclusion, initial/part epigraphs must survive. |
| Левша | Н. С. Лесков; 1895 | [Specific 1902–1903 ВТ edition](https://ru.wikisource.org/wiki/Левша_(Лесков)/ПСС_1902—1903_(ВТ)) | First published 1881. Parent [edition index](https://ru.wikisource.org/wiki/Левша_(Лесков)) cites ПСС, 3rd ed., А. Ф. Маркс, 1902, т. 4. The index itself is not the text. ВТ indicates editorial spelling modernization. All20chapters are present. Both author notes are independently labeled byРВБ as authorial and retained;33printed page anchors109–141 are preserved in provenance. |
| Хамелеон | А. П. Чехов; 1904 | [Осколки variant](https://ru.wikisource.org/wiki/Хамелеон_(Чехов)/Вариант_журнала_«Осколки») | «Осколки», 1884, №36, 8 September, p.4, signed А. Чехонте. Source reconstructs the original journal variant through ФЭБ/ПСС 1975, т.3, pp.52–55, 487; this is not a scan of the journal. |
| Человек в футляре | А. П. Чехов; 1904 | [Single text](https://ru.wikisource.org/wiki/Человек_в_футляре_(Чехов)) | «Русская мысль», 1898, VII, 120–131. Source cites Комаров/ПСС 1977, т.10, 42–54 and separately Маркс 1903, XII, 130–143. Preserve that bibliographic ambiguity; do not invent a particular physical exemplar. |
| Сон смешного человека | Ф. М. Достоевский; 1881 | [Five-part single text](https://ru.wikisource.org/wiki/Сон_смешного_человека_(Достоевский)) | Source identifies 1877 and az.lib.ru. The exact printed exemplar is unknown. Five original Roman-numbered parts must be present in order. |
| Муму | И. С. Тургенев; 1883 | [Single text](https://ru.wikisource.org/wiki/Муму_(Тургенев)) | «Современник», 1854, XLIV, №3, I, 9–36. Source names turgenev.org.ru; exact printed exemplar unknown. The separate first-publication note is editorial context. |
| Премудрый пискарь | М. Е. Салтыков-Щедрин; 1889 | [Single text](https://ru.wikisource.org/wiki/Премудрый_пискарь_(Салтыков-Щедрин)) | «Общее дело», Geneva, 1883, №55, 2–4; Russian publication «Отечественные записки», 1884, №1. The Wikisource header mixes an incorrect ПСС /16-volume /АНСССР description. The linked [РВБ titlepage and contents](https://rvb.ru/saltykov-shchedrin/tocvol_16_1.htm) verify Собрание сочинений в20томах, Художественная литература,1974, т.16, кн.1,30–34. The discrepancy is retained as source metadata evidence. |
| Красный цветок | В. М. Гаршин; 1888 | [Six-part single text](https://ru.wikisource.org/wiki/Красный_цветок_(Гаршин)) | «Отечественные записки», 1883, CCLXX, №10, I, 297–310. Source names az.lib.ru; exact printed exemplar unknown. Initial dedication to Turgenev belongs to the work. Source explicitly labels explanatory notes as editorial. |
| Много ли человеку земли нужно | Л. Н. Толстой; 1910 | [Nine-part single text](https://ru.wikisource.org/wiki/Много_ли_человеку_земли_нужно_(Толстой)) | «Русское богатство», 1886, №4; РВБ/Собрание сочинений в22томах, Художественная литература, 1982, т.10. Stable and latest wiki revisions may differ. |
| Три старца | Л. Н. Толстой; 1910 | [Single text](https://ru.wikisource.org/wiki/Три_старца_(Толстой)) | «Нива», 1886, №13; same 1982 volume. Include the initial Gospel epigraph, whose quoted source is explicitly printed. Stable and latest wiki revisions may differ. |

This adds four authors absent from the initial five-work Wikisource selection:
Pushkin, Leskov, Saltykov-Shchedrin and Garshin. Individual chapters, edition-index
pages, excerpts and anthologies do not become extra curated works.

[Нос](https://ru.wikisource.org/wiki/Нос_(Гоголь)) was not selected: its current
metadata contains inconsistent first-publication claims and lacks a text-source
reference. [Кроткая](https://ru.wikisource.org/wiki/Кроткая_(Достоевский)) has an
explicit eleven-page sequence including the author's foreword; it remains a
reasonable later acquisition, but the present selection prioritizes single pages.
[Кавказский пленник](https://ru.wikisource.org/wiki/Кавказский_пленник_(Толстой))
would need extra first-publication/source verification and individual treatment
of its signed Tolstoy footnote; it was not silently substituted into this queue.

## Rights and provenance

The [Wikisource copyright policy](https://ru.wikisource.org/wiki/Викитека:Авторские_права)
and the original-work/author public-domain notices distinguish expired original
copyright from later translations, editorial material and contributor text.
The new authors' primary author pages establish deaths in
[1837](https://ru.wikisource.org/wiki/Автор:Александр_Сергеевич_Пушкин),
[1895](https://ru.wikisource.org/wiki/Автор:Николай_Семёнович_Лесков),
[1889](https://ru.wikisource.org/wiki/Автор:Михаил_Евграфович_Салтыков-Щедрин),
and [1888](https://ru.wikisource.org/wiki/Красный_цветок_(Гаршин)).
All selected originals were published during the authors' lifetimes before 1900;
no posthumous first publication or modern translation is being treated as an
expired original. Recent printed source editions do not automatically license
recent commentaries or textual contributions.

The source footer's [CC BY-SA4.0](https://creativecommons.org/licenses/by-sa/4.0/)
link must be present in each captured page. The derivative retains the author,
canonical source, exact displayed revision, contributor-history link, license,
[Wikimedia terms](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use),
modification notice and each byte-level capture receipt. Canonical HTML may be a
stable older revision: the existing connector checks actual `wgRevisionId`
against API metadata, including bounded exact revision metadata when necessary.
No oldid HTML crawler or bulk parse API is used.

Every acquired HTML page is copied to an immutable SHA-named file under the
separate output directory before publication. Later provider-cache refreshes
cannot invalidate the retained raw evidence. Derived text, source bytes and
large runtime review reports remain under ignored `data/`.

## Two-stage gate and execution

```python
from pathlib import Path
from book_rag.russian_short_sources import acquire_russian_short_works

# Run only after the shared deferral expires and root confirms provider access.
# Uses the existing host state and raw cache; does not touch its old manifest.
report = acquire_russian_short_works(
    Path("data/russian/short-works-v1"),
    shared_cache=Path("data/russian/wikisource"),
    cooldown_states=(Path("data/identity/wikidata-200-v1/queries/deferred.json"),),
)
# No reviews: records captured source evidence but publishes no full-work claim.
# Captured original bytes are archived before review, independently of URL cache.

# After individually reviewing raw body boundaries, every note and all parts:
report = acquire_russian_short_works(
    Path("data/russian/short-works-v1"),
    shared_cache=Path("data/russian/wikisource"),
    reviews_path=Path("corpus/russian-short-reviews.json"),
    offline=True,  # no HTTP client or network fallback, even during cooldown
)
```

A full-work review is keyed by work slug and includes
`review_status=verified_original_work`, `raw_sha256`, exact first/last body
anchors, a human-readable `review_basis`, and exhaustive disjoint
`author_note_ids` / `editorial_note_ids`. An explicitly partial review uses
`verified_original_body_partial_notes`, nonempty `unverified_note_ids` and
`partial_reason`; any unverified note prohibits full coverage. An unclassified note,
changed raw SHA, missing chapter marker, ambiguous body boundary, loose text that
would be dropped, missing author credit or absent transcription license fails
closed. Merely seeing a page category such as `75%` does not prove book coverage;
proofreading status and original-work completeness are separate questions.

Roman/numeric source chapter headings are retained as headings; poetry line
breaks, literal code and logical table rows/cells pass through the existing
canonical parser. Whole-work coverage is `full` only after this explicit audit,
with `coverage_scope=original_work_only` and detailed editorial omissions.
BookRAG's provenance wrapper has a separate SHA/text-guarded editorial attribution.
Epigraph authorship is not guessed from the catalog author.

HTTP429/5xx,403 and network errors stop subsequent requests to this host and
persist a shared deferral. Source pages are serial and size-bounded by the
existing connector (5MiB each,2–5seconds between requests). A cache-only run is
explicitly offline, verifies the stored capture SHA and never falls back to HTTP.
Old reviewed entries survive a capture-only or shorter-limit rerun. An unrelated
manifest or corrupt previously emitted artifact is preserved and rejected.

## Verification before live integration

The existing seventeen provider tests, thirty-six connector tests, four offline
review-packet tests and four independent projection-proof tests pass (61total).
They cover immutable source receipts, shared cooldown across fresh destinations,
zero-HTTP offline packaging, SHA-bound reviews, complete note accounting,
missing/inverted chapters, empty middle/final chapters, heading/epigraph/reference-only chapter bodies, source-body boundaries, loose-text loss, verse/table/
literal-code fidelity, throttle stop, stable cached artifact bytes and retention
of the original five-work manifest. Ruff and ty pass for both new files.

Actual measurements and source IDs are recorded in
[`experiments/russian-short-v1.json`](../experiments/russian-short-v1.json). Root
must review the resulting manifest before importing it; the connector itself
performs no live import.

The offline evidence command is:

```sh
.venv/bin/python scripts/audit_russian_short_works.py --output data/russian/short-works-v1/audit-v1
```

It reads only verified cached pages, emits every source block and reference for
inspection, and creates deliberately unapproved review templates. It never
creates an HTTP client or approves completeness. Existing audit evidence is
never overwritten. The pre-capture packet remains immutable evidence of the initial empty state.
The actual capture/audit packet at `data/russian/short-works-v1/audit-v1`
contains all ten verified pages; the post-capture review/proof uses the current
connector, including centered source headings.

A bounded read-only check of public-source Russian entries in the current live
catalog found the original five Wikisource works and five Gutenberg Russian
entries; none is one of the ten proposed work titles. Pushkin already appears
in the Gutenberg selection as a different poem, so the ten candidates add
three authors to the currently observed Russian full-text catalog, not four.
The four-author statement above refers specifically to the initial five-work
Wikisource selection. No automatic same-author or fuzzy-title merging is used.

The offline follow-up corrected only the verified RVB edition description and
chapter-body validation. Every expected marker must have at least one following
narrative block with an alphanumeric character before the next marker. Headings,
epigraph/poem/dedication-only blocks, reference markers and decorative separators
do not establish a narrative body. The audit records body block/character counts
per chapter; it does not infer completeness from those counts alone. All source
epigraphs are still retained in the projected work.

The WiseGudgeon review spec records both the erroneous Wikisource header and the
linked primary RVB titlepage, including its separate copyright notice for1974
commentary. No recent commentary is reclassified as the original author's text.
The printed source is now described as Собрание сочинений в20томах,
Художественная литература,1974, т.16, кн.1, с.30–34.

## Actual results

| Check | Observed result |
| --- | --- |
| Canonical HTML capture |10pages,1,365,989bytes,22serial requests, no new deferral |
| Original scope |9full works;1partial edition with unknown note authorship |
| Independent source/canonical comparison |252,157non-whitespace NFC characters match in exact order across all10projections |
| Original numbered structure |47chapter/part/conclusion markers preserved; each has a substantive body |
| Author notes |Both Leskov notes retained; independent primaryРВБ attribution; no inferred note author |
| Verse |12original lines of Pushkin’s opening poem match canonical text exactly; HTML formatting afterBR does not invent stanza gaps |
| Derived artifacts |577,501bytes;66parsed sections;313,599canonical characters including ten separately attributed provenance wrappers |
| Repeat |0HTTP,10cache hits; all artifact/source-bundle hashes and original retrieval timestamps unchanged;0.756s observed local packaging time |
| Live changes |0imports,0model calls,0library writes |

The independent proof command is:

```sh
.venv/bin/python scripts/validate_russian_short_works.py \
  --reviews corpus/russian-short-reviews.json \
  --output data/russian/short-works-v1/source-validation-review.json
```

This walks the original source DOM separately from the connector’s selected
blocks, removes web controls and printed-page labels, and checks every retained
non-whitespace character in order against the actual canonical parser output.
It separately verifies all original heading markers, detects dropped/duplicated
prose and proves retained author-note content. Whitespace normalization is
explicitly outside that whole-body proof; the12-line poem and literal-code
regressions supply separate checks. It is not a facsimile/scan comparison.

The actual source audit found historicalCENTER headings in «Пиковая дама» and
the fourth chapter of «Левша». The connector now retains them; generic book
parsers and retrieval code were not changed. Printed page labels109–141 in
«Левша» remain33raw-HTML anchors in provenance, not words inserted into quotes.
A canonical-offset/printed-page reader mapping is not implemented by this task.

The three source transcriptions without a verified printed exemplar remain
explicitly described that way (Dostoevsky’s dream, Mumu, RedFlower). Stable2013
Tolstoy page revisions1071293and1074298 were checked against their exact API
revision metadata rather than relabeled with newer revision IDs. All raw bytes,
source hashes, original retrieval timestamps and contributor-history URLs are
retained. A future changed raw capture requires a new review.
