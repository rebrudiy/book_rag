# Russian originals and proposed English editions

The initial metadata investigation, 2026-11-14, identified **three standalone
Gutenberg editions for two existing Russian work groups**. The lead subsequently
acquired all three through the single serial downloader; the local frontmatter
and complete-chapter audit is recorded below. It does not claim
five verified Gutenberg translations or promote a new unique-work count.
No Gutenberg book HTTP request, download, ingestion, manifest edit or database
write was made while the main provider acquisition queue was active.

Machine-readable evidence is under ignored
`data/identity/russian-edition-links/`: `proposals.json`, hashed SPARQL snapshots
in `queries/`, supporting query-result reports, and exact RDF records in
`catalog/`. Additional query reports include roots, sitelinks and contributors.
The original official catalog SHA-256 is
`86a11d5cf64ac9bf3f8c59cbc6bdbe505185884c66d84a0930be48b02f6d31a3`.

## Scope and method

The current library was inspected with read-only SQLite connections. The existing
3,005-entry text-only plan and local download cache were checked before proposing
new sources. The eight candidate/rejected Gutenberg IDs below were absent from
the live library, that plan and the cache at the observation time; background
acquisition can change later library counts.

A single local streaming scan of all **79,811 official RDF records** took
**24.676 seconds**, retaining **124 English records** whose creator metadata
matched the five author identities. Exact RDF bytes, member names and SHA-256
values were retained. Titles and contents supplied candidate discovery, but a
translated title by itself did not establish work identity. Automatically
generated `marc520` summaries were not used as identity or rights proof.

The Russian snapshots already contain explicit `wgWikibaseItemId` values.
Those exact captured values were cross-checked against public Wikidata
sitelinks and bounded [P629 edition/translation relations](https://www.wikidata.org/wiki/Property:P629).
[P2034](https://www.wikidata.org/wiki/Property:P2034) supplies exact Gutenberg
identifiers where present. For the two Turgenev editions, the provider's explicit
Wikipedia-reference metadata was joined to the exact authority sitelink; no
fuzzy title match was needed.

Wikidata metadata requests used the existing two-second serial query client,
25-second operation timeout, descriptive `BookRAG/0.1` User-Agent, hashed caches
and persisted error/429 deferral. All returned valid JSON; no retries or paid API
were used. Structured Wikidata assertions are
[CC0](https://www.wikidata.org/wiki/Wikidata:Licensing), separate from book rights.
Wikisource bibliographic/translation pages were inspected as primary evidence;
no bulk chapter acquisition or export was started.

## Exact identities of the existing Russian originals

| Existing work ID | Captured Russian item | Explicit literary-work root | Evidence |
| --- | --- | --- | --- |
| `ru-ivan-ilyich` | [Q83799145](https://www.wikidata.org/wiki/Q83799145) | [Q396602](https://www.wikidata.org/wiki/Q396602) | Exact Russian Wikisource edition sitelink + P629 |
| `ru-underground` | [Q695116](https://www.wikidata.org/wiki/Q695116) | Q695116 | Exact Russian Wikisource work sitelink |
| `ru-overcoat` | [Q108937643](https://www.wikidata.org/wiki/Q108937643) | [Q402673](https://www.wikidata.org/wiki/Q402673) | Exact 1938 Russian transcription sitelink + P629 |
| `ru-fathers-and-sons` | [Q29424](https://www.wikidata.org/wiki/Q29424) | Q29424 | Exact Russian Wikisource work sitelink |
| `ru-ward-six` | [Q289770](https://www.wikidata.org/wiki/Q289770) | Q289770 | Exact Russian Wikisource work sitelink |

`proposals.json` records each actual Russian edition ID, source revision,
source-HTML hash and matching query hashes. The Russian acquisition's exclusion
of later printed editorial apparatus remains in force; work grouping does not
extend public-domain permission to that excluded material.

## Standalone Gutenberg candidates

All three exact RDF records state **Public domain in the USA**, catalog type
`Text`, English language and a named translator. These provider statements
apply to the particular ebook record. Their electronic release dates do not
establish the date of a printed source edition. Rights outside the USA and any
edition-specific editorial scope remain separate checks.

| Proposed edition ID | Exact provider title | Translator / other contributor | Proposed existing work ID |
| --- | --- | --- | --- |
| [`pg600-full`](https://www.gutenberg.org/ebooks/600) | Notes from the Underground | Constance Garnett | `ru-underground` |
| [`pg47935-full`](https://www.gutenberg.org/ebooks/47935) | Fathers and Sons | C. J. (Charles James) Hogarth | `ru-fathers-and-sons` |
| [`pg30723-full`](https://www.gutenberg.org/ebooks/30723) | Fathers and Children | Constance Garnett; editor William Allan Neilson | `ru-fathers-and-sons` |

**#600:** the exact authority assertion is `Q695116 — P2034 → "600"`. The same
Q695116 is explicitly linked to the captured Russian work page. The RDF names
Dostoyevsky and Garnett. The corresponding authority English translation item
[Q138103448](https://www.wikidata.org/wiki/Q138103448) has P629 to Q695116,
Garnett as translator, and an
[English Wikisource constituent sitelink](https://en.wikisource.org/wiki/White_Nights_and_Other_Stories/Notes_from_Underground).
That constituent corroborates the translation family; it does not make the
entire *White Nights* collection identical to *Notes from Underground*.

**#47935:** the exact Gutenberg metadata explicitly says `Translation of: Отцы и
дети.` and identifies Turgenev and Hogarth. It cites
`https://en.wikipedia.org/wiki/Fathers_and_Sons_(novel)`. The public authority
sitelink query binds that exact URL to Q29424, which in turn has the exact
Russian Wikisource work sitelink. This is an explicit bibliographic/authority
chain even though no P2034 assertion for #47935 was found in Wikidata.

**#30723:** the exact record cites the same authority URL, identifies Turgenev,
names Garnett as translator and Neilson as editor, and provides *Fathers and
Sons* as an alternative title. The same exact URL→Q29424→Russian source chain
supports the proposed relation. The editor's text must not automatically be
attributed to Turgenev or to the translator. Inspect the source frontmatter and
bind any contributor rules to the actual artifact and parsed headings after
acquisition.

The [English Wikisource translations index](https://en.wikisource.org/wiki/Fathers_and_Sons)
separately lists multiple translations, including Garnett and the later Richard
Hare version. An index link does not establish a usable complete transcription
or permission for every translation. The proposal selects the individually
rights-verified Gutenberg records, not the later version.

### Exact format evidence

These EPUB links are listed exactly in the archived per-book RDF:

| Gutenberg ID | Exact RDF no-image EPUB URL | Existing verified direct-cache resolution |
| --- | --- | --- |
| 600 | [600.epub.noimages](https://www.gutenberg.org/ebooks/600.epub.noimages) | [pg600.epub](https://www.gutenberg.org/cache/epub/600/pg600.epub) |
| 47935 | [47935.epub.noimages](https://www.gutenberg.org/ebooks/47935.epub.noimages) | [pg47935.epub](https://www.gutenberg.org/cache/epub/47935/pg47935.epub) |
| 30723 | [30723.epub.noimages](https://www.gutenberg.org/ebooks/30723.epub.noimages) | [pg30723.epub](https://www.gutenberg.org/cache/epub/30723/pg30723.epub) |

The cache resolutions use the already verified catalog alias mapping; the exact
RDF alias, media type, record SHA and mapping basis remain in the report.
All other RDF-listed formats are retained there too. No URL was probed or book
file fetched for this investigation. No-image acquisition omits illustrations
and image-only content; it is not a facsimile claim.

Bibliographic verification precedes textual verification. These are standalone
full-text source **candidates**, not newly available/searchable editions. After
the provider queue is clear, download #600 and #47935 first, then optionally
#30723 for translation comparison. Verify the original work's two parts / 21
chapters for *Underground* and 28 chapters for each *Fathers* translation,
source frontmatter, footnotes and literal citation slices before declaring full
coverage or accepting the work-group proposal. Translation differences need not
produce equal text lengths or sentence alignment.

### Explicitly rejected same-title edition

Gutenberg **#6536**, also *Notes from the Underground*, names Garnett but its
record states **Copyrighted. Read the copyright notice inside this book for
details.** It is excluded. The public-domain status of #600, a translator's death
year, or a matching title cannot override #6536's exact rights statement. The
copyright notice itself was not fetched while the provider queue was active.

## Anthologies: constituent candidates, not whole-work merges

| Original | Gutenberg collection | Candidate constituent / translator | Required action |
| --- | --- | --- | --- |
| `ru-overcoat` | [#36238, The Mantle, and Other Stories](https://www.gutenberg.org/ebooks/36238) | The Mantle; Claud Field | Review exact original-work identity and component bounds; exclude Mérimée introduction and other stories |
| `ru-overcoat` | [#1197, Taras Bulba, and Other Tales](https://www.gutenberg.org/ebooks/1197) | The Cloak; translator not identified in RDF | Resolve translator/edition and exact component identity; preserve collection identity |
| `ru-ward-six` | [#13409, The Horse-Stealers and Other Stories](https://www.gutenberg.org/ebooks/13409) | Ward no. 6; Constance Garnett | Validate all 19 story chapters and stop before the next story |
| `ru-ward-six` | [#55307, The Black Monk, and Other Stories](https://www.gutenberg.org/ebooks/55307) | Ward No. 6; R. E. C. Long | Validate story bounds and translation edition; preserve collection identity |

All four collection records explicitly state US public-domain rights. Their
exact no-image EPUB aliases are `/ebooks/36238.epub.noimages`,
`/ebooks/1197.epub.noimages`, `/ebooks/13409.epub.noimages` and
`/ebooks/55307.epub.noimages` on `https://www.gutenberg.org`; all are stored
verbatim in the report. The same verified direct-cache resolution is available.
These formats have not been downloaded or checked for narrative completeness.

The [Field edition title page](https://en.wikisource.org/wiki/The_Mantle_and_Other_Stories)
credits Field and the separate Mérimée introduction. Its
[constituent page](https://en.wikisource.org/wiki/The_Mantle_and_Other_Stories/The_Mantle)
explicitly links to the
[Wikisource versions page for The Cloak](https://en.wikisource.org/wiki/The_Cloak_(Gogol)).
That is stronger than guessing from an English title. However, no exact
P2034/P629 or English Wikisource authority sitelink binding that component to
Q402673 was found in this run. The gap stays explicit: do not automatically
assign `ru-overcoat` to the collection or claim the constituent identity fully
verified without an additional source/edition review.

For Chekhov, English constituent item
[Q107611379](https://www.wikidata.org/wiki/Q107611379) explicitly has P629 to
Q289770, translator Garnett, and the
[Ward No. 6 constituent sitelink](https://en.wikisource.org/wiki/The_Tales_of_Chekhov/Volume_10/Ward_No._6).
The [volume index](https://en.wikisource.org/wiki/The_Tales_of_Chekhov/Volume_10)
identifies the same *Horse-Stealers* collection/story sequence as the Gutenberg
contents, but **explicitly marks the Wikisource volume incomplete**. Do not claim
a complete English book or complete story merely by exporting that index.
#13409 remains a Gutenberg constituent candidate until its actual bytes and
boundaries have been inspected.

A future verified constituent needs its own source-edition ID and provenance,
for example a derived `pg13409-ward-six-en-garnett`, retaining the parent catalog
record, artifact hash, exact section range and translator. The original
`pg13409-full` must remain a collection with its own identity. A generic `work_id`
on that whole collection must never be changed to `ru-ward-six`.

## Ivan Ilyich: provider gap and a separate fallback

No standalone matching source or explicit matching contents entry was found in
the checked five-author English Gutenberg snapshot for *The Death of Ivan
Ilyich*. This bounded finding is not proof that every future or alternate
catalog lacks one. In particular, **#689 is not supported as an Ivan Ilyich
source**: its explicit contents include *Ivan the Fool*, a different work.

A promising independent source is the
[English Wikisource Maude translation](https://en.wikisource.org/wiki/The_Death_of_Ivan_Ilych).
Its exact authority item [Q83799149](https://www.wikidata.org/wiki/Q83799149)
has P629 to Q396602, English language, Aylmer and Louise Maude as translators,
1923 publication metadata and the exact English Wikisource sitelink. The Russian
source item Q83799145 also has P629 to Q396602. Thus the proposed cross-language
work relationship has explicit authority evidence on both sides.

The source index lists twelve chapters and separately marks the original and
translation as public domain, including a US publication-date statement for the
translation. Wikisource transcription contributions have their separate CC BY-SA
terms. This is a separate-provider proposal, not a Gutenberg ebook, and no
chapter bundle was acquired. A future connector must capture actual revision
IDs, per-chapter hashes/rights, all twelve chapters and translator credits before
claiming complete searchable coverage.

## Initial metadata result and acquisition handoff

There are **five verified Russian authority origins**, **three standalone
Gutenberg edition proposals**, **four blocked anthology-constituent proposals**,
one excluded copyrighted record and one independent-provider fallback. None of
these changes the library's groups or verified-work totals yet.

Once the lead reports the active Gutenberg queue complete: acquire the no-image
EPUBs #600 and #47935 serially with the existing respectful downloader, verify
coverage/attribution/citations, then present the two work-group decisions against
`ru-underground` and `ru-fathers-and-sons`. Optionally acquire #30723 as a third
English edition for translator comparison. Do not download the anthologies as
if they were standalone versions of *The Overcoat* or *Ward No. 6*.

The runtime report was validated offline for all three exact rights gates,
matching source numbers/formats, five captured Russian authority IDs, zero
live/plan/cache overlap and explicit refusal of every whole-anthology merge.
No application code change was needed for this investigation.

## Acquired-artifact frontmatter and coverage audit

The lead acquired and indexed all three proposed source IDs, then requested a
local audit before applying corrected contributor metadata. The audit opened
SQLite in `mode=ro`, read the archived EPUBs under `data/full/artifacts`, and
performed no network requests, database writes, parser edits or active-queue
edits. The subsequent parser fixes were owned by the lead; rules below were
checked against a fresh parser process after those fixes.

The redistributable **metadata-only** source manifest is
`corpus/translation-links.json`. Its byte-identical runtime counterpart is
`data/catalog/russian-translations-priority.json`. It contains no source text,
secrets, downloaded files or local absolute paths. RDF metadata is CC0; each book
retains its own explicit Gutenberg US public-domain statement and regional
rights caveat. Gutenberg edition IDs stay distinct while `work_id` links them to
the accepted existing Russian groups. The manifest does not assign a collection
ID to one constituent story.

| Source | Actual archived artifact SHA-256 | Fresh sections | Verified narrative chapters |
| --- | --- | ---: | ---: |
| 600 | `0bd0471fa0682a64ae53a4e2535ec752c77cd557455d21c6e92297793c4e333b` | 24 | 21, in parts of 11 and 10 |
| 47935 | `94cd0ed3de48de7b77243cbf880dfa57044632fa9411a7964f82a5b71ec88136` | 29 | I–XXVIII, 28 |
| 30723 | `63cbc759541310d44826736916cf1fdee3850a648385d8273cb261f7680098ca` | 40 | CHAPTER I–XXVIII, 28 |

All **77 narrative chapters** occur in order. The remaining sixteen sections are
frontmatter, an author note and a separate borrowed epigraph. Current parser
fingerprint for this audit:
`5e90d19c3bf4cd6a81a109bb8a29bb34d89094d2932f0b1b9fdfe8a65730964b`.
The source EPUB spine and canonical chapter headings were both checked; table of
contents entries alone were not counted as chapters.

The audit found that generated HTML page titles could mislabel the leading
continuation of a previous chapter as the next chapter, or even as the Gutenberg
license. The lead corrected this before the final replay. The apparently
license-labelled Hogarth fragment was a legitimate short footnote, not retained
license prose. No chapter was discarded. Separately, the Garnett *Underground*
ending still contains a short provider cover-art grant after the novel's ending;
that grant is not Dostoyevsky prose and is recorded as a remaining source-cleanup
issue.

### Printed editions actually established

**Hogarth, #47935:** the source title page identifies J. M. Dent & Sons Ltd,
London and Toronto, and E. P. Dutton & Co, New York, **1921**. The introduction is
signed **E. R.** No full name is inferred. Its canonical section also contains a
following bibliography without a separate compiler credit; the rule explicitly
labels that distinction instead of attributing the bibliography to a guessed
person.

**Garnett, #30723:** the source identifies *The Harvard Classics Shelf of
Fiction*, **[from Vol. 19]**, selection credited to Charles W. Eliot, edited with
notes and introductions by William Allan Neilson, P. F. Collier & Son, New York.
The printed line gives **copyright 1917**; a separate printing year is not stated
and is not invented. This ebook contains the complete selected novel with
frontmatter, not the entire physical volume. The source scan is explicitly
linked in the ebook as `archive.org/details/harvardclassicss19elio`.

**Garnett, #600:** the artifact identifies the translator and electronic release
(1996-07-01; updated 2026-09-21), but no printed-source publisher or year. The
edition field now states that gap. It does not borrow 1918/1923 publication data
from a different Wikisource edition of the same translation.

### Contributor evidence and guarded rules

The source manifest now has **16 explicit section rules**. Every rule has a
source-artifact SHA-256, required title fragment and a distinguishing source-text
fragment. `audited_section_ordinal` records the observed location without making
it a selector. Only the isolated two-letter title-page fragment `BY` additionally
uses `section_ordinal: 2`, since those two letters alone could match ordinary
prose. A later ordinal change must fail closed and prompt reinspection.

Each rule matched exactly one intended section in the fresh parse. Negative
checks with an incorrect artifact SHA or unrelated section text matched none.
No narrative chapter was marked editorial. Exact original EPUB members, raw
credit fragments and translator exceptions remain in each rule's provenance.

- **#600:** Contents is editorial. The separately signed author's note is
  attributed to Dostoyevsky. The standalone Part II epigraph credits
  **NEKRASSOV**, translated by **Juliet Soskice**; it is attributed to Nekrassov
  with `attribution_kind: epigraph`, retaining the surname as printed. Garnett
  remains the novel translator. The opening quoted verse inside Part II,
  chapter IX does not justify reattributing that entire narrative chapter.
- **#47935:** Introduction is editorial, labelled **E. R. (signed introduction;
  bibliography unattributed)**. The raw initials and publisher/date are retained;
  neither an expanded name nor bibliography compiler is guessed.
- **#30723:** Production notes, title-page fragments, contents and the character
  list are marked editorial with no invented personal author. The biographical
  note is signed **W. A. N.**, and the same title page explicitly credits notes
  and introductions to **William Allan Neilson**; both facts support that
  artifact-specific attribution.

There are **five** criticism excerpts, with explicit restored source bylines:

| Criticism | Credited author, retained as printed | Separate translator evidence |
| --- | --- | --- |
| I | Emile Melchior, Vicomte de Vogüé | J. L. Edmands, explicitly credited in the source excerpt citation, 1887 |
| II | William Dean Howells | No translator inferred |
| III | K. Waliszewski | No expanded name or translator inferred |
| IV | Richard H. P. Curle | No expanded initials or translator inferred |
| V | Maurice Baring | No translator inferred |

The first credit's source spelling is retained with “as credited”; it is not
silently corrected to an authority name from memory. The novel's book-level
Garnett credit does not establish that she translated these editorial excerpts.
Per-rule `translator` metadata records Edmands and Soskice, but the current
section-role model does not yet persist/display a separate translator field.
Until that is integrated, do not substitute the book-level translator on a
borrowed epigraph or separately translated critical essay. Span-level contributor
attribution inside a mixed narrative section is also not implemented.

Machine-readable audit:
`data/identity/russian-translations-frontmatter/frontmatter-audit.json`, with the
initial read-only database snapshot and local source-frontmatter extracts nearby.
The final manifest SHA-256 is
`3343f182e3f5327630bdada14b897850d67a94d7e065f49f6c1686079504f0b2`.
The lead applies the frozen manifest to the live library; this audit itself did
not modify database attribution or active downloader state.

### Safe priority merge and restart procedure

The original priority handoff used a separate output, preserving every old source
record. For a future restart, let `active_path` be the actual active manifest
(the current large expansion originally used `data/catalog/expand-text-new.json`)
and write a new filename. Load the current priority manifest first; never replace
the whole old queue with only the three priority books.

```python
import json
from pathlib import Path

active_path = Path("data/catalog/expand-text-new.json")
priority_path = Path("data/catalog/russian-translations-priority.json")
output = Path("data/catalog/expand-text-with-translations.json")
old = json.loads(active_path.read_text())
priority = json.loads(priority_path.read_text())
assert len({e["id"] for e in old}) == len(old)
assert len({e["id"] for e in priority}) == len(priority)
by_id = {e["id"]: e for e in old}
front = []
for entry in priority:
    previous = by_id.get(entry["id"])
    if previous:
        # Conflicts need explicit curation/migration, never silent overwriting.
        for key in ("source_url", "download_url", "format", "work_id"):
            if previous.get(key) != entry.get(key):
                raise ValueError(f"Conflicting {key} for {entry['id']}")
    front.append({**(previous or {}), **entry})
priority_ids = {e["id"] for e in priority}
merged = front + [e for e in old if e["id"] not in priority_ids]
assert {e["id"] for e in merged} == set(by_id) | priority_ids
assert len(merged) == len({e["id"] for e in merged})
output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name(output.name + ".tmp")
temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
temporary.replace(output)
```

Once the existing serial process has exited, resume exactly one downloader with
its normal data directory, two-second delay and disk floor:

```sh
BOOKRAG_DATA_DIR=./data/full .venv/bin/bookrag ingest \
  data/catalog/expand-text-with-translations.json --download-delay 2 --min-free-gib 4
```

Existing indexed/cached editions resume through the normal provenance checks;
provider retry deferrals must remain in the database. The three source IDs are
not renamed, so citations and cache identities remain stable. If an older merged
queue already contains earlier metadata for these three entries, refresh those
entries from the current audited priority manifest before another restart;
otherwise old edition descriptions can overwrite newer curation. The procedure
was handed to the lead; it does not imply that this audit changed the active
manifest or started a second downloader.
