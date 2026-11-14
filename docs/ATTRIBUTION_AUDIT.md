# Contributor attribution audit of the curated full-book corpus

The targeted audit found 13 additional section rules that can distinguish clear editorial or electronic-production prose from the catalog book author in **future live imports**. They cover 13 parsed sections and 65 current passages across seven editions. Seven further findings need section splitting or safer selectors. These counts describe the inspected candidates, not an estimated authorship error rate for the library.

The audit read the 20-edition, 19-work `curated20-v3` snapshot and original cached EPUB artifacts without changing them. It inspected 79 front-matter, heading-selected and final-tail sections. Every original artifact hash was checked against the recorded hash; the snapshot database digest remains `ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8`. Manifest, frozen evaluation labels and the snapshot were not edited. This is a targeted review, not an exhaustive attribution of every quotation in every book.

`experiments/attribution-proposals.json` contains the proposed `section_attributions`, all matched titles, exact short evidence anchors with offsets, descriptive context, artifact/content/section hashes, contributor metadata, confidence and application blockers. `eligible_rules_by_book` contains only the 13 candidates whose selectors match the intended sections exactly. It is a proposal artifact; no rules have been applied by this audit.

## Rules suitable for a future live import

| Edition | Target section selectors | Proposed contributor | Basis |
| --- | --- | --- | --- |
| Meditations, #2680 | `INTRODUCTION`, `NOTES`, `GLOSSARY` | Unidentified editorial contributor | Retrospective biography and translation bibliography, decisions about Casaubon's notes, and an English translation glossary. |
| The Prince, #1232 | `INTRODUCTION`, `YOUTH`, `OFFICE`, `LITERATURE AND DEATH`, `THE MAN AND HIS WORKS` | Unidentified editorial contributor | Later biography of Machiavelli, including his death and subsequent editions/scholarship. |
| Leviathan, #3207 | The long title beginning `Thomas Hobbes of Malmesbury Printed for Andrew Crooke` | Unidentified editorial contributor | The speaker explains preparing an electronic text and converting typography/marginal notes to ASCII. |
| Beyond Good and Evil, #4363 | `Translated by Helen Zimmern` | Unidentified editorial contributor | The section describes a later reprint and electronic spelling/formatting changes; it is not Nietzsche's dated preface. |
| Moby Dick, #2701 | `By Herman Melville` | Unidentified editorial contributor | Contents and a digital-production note about combining electronic source versions. |
| The Republic, #1497 | `Translated by Benjamin Jowett` | Unidentified editorial contributor | A short digital cross-reference to another ebook, separate from Jowett's introduction. |
| Sherlock Holmes, #1661 | `The Adventures of Sherlock Holmes` | Unidentified editorial contributor | Digital notice about a separately numbered illustrated edition and a contents list. |

All rules are **book-specific** and use `role: "editorial"`. The placeholder expresses uncertainty; it is not a claim that an unknown historical person has been identified. Catalog proofreader/production credits cannot establish which individual wrote an unsigned note. Likewise, a translator credit alone does not prove authorship of an introduction or an electronic-production notice. The [Prince catalog](https://www.gutenberg.org/ebooks/1232) credits Marriott as translator; the [Leviathan catalog](https://www.gutenberg.org/ebooks/3207) names production contributors. Neither establishes the speaker of the unsigned passages inspected here.

The exact evidence and every title match are in the JSON. Do not apply the title fragments globally across books. A future import must first match the edition ID and artifact hash, then revalidate all matched section titles. The existing three Jowett `INTRODUCTION AND ANALYSIS` sections remain a positive control and are already correctly labeled editorial.

Some editorial sections quote an author's letters or historical sources. A section-level compiler label does not identify every nested quotation speaker. The proposals improve the framing voice's attribution; local quotation credits must still be retained and, for isolated quotation claims, resolved at a finer level.

## Findings that must not become broad rules

**George Saintsbury's signed preface.** The imported Pride and Prejudice artifact explicitly credits the preface to Saintsbury and ends it with his signature. Its heading is identical to the preceding contents section, however. A `title_contains` rule would affect both. The personal attribution is supported, but the current selector cannot isolate the intended preface; use a structural/body selector or a future parser split. The [official catalog](https://www.gutenberg.org/ebooks/1342) lists Austen as book author and does not supply this preface contributor in its author table.

**W. S. Dallas's glossary.** The sixth Origin edition explicitly credits Dallas for the glossary, but the parsed section begins with Darwin's first-person acknowledgement. Assigning the entire section to Dallas would misattribute that opening. Split acknowledgement and glossary definitions before assigning their separate contributors. The [official catalog](https://www.gutenberg.org/ebooks/2009) remains book-level metadata; the contributor evidence comes from the actual imported text.

**Einstein's appendix and electronic-production tail.** Appendix IV contains scientific prose followed by a modern conversion/review note and a 2018 date. The whole section cannot be relabeled editorial without overriding Einstein's science. Split the production tail first. The [catalog](https://www.gutenberg.org/ebooks/5001) distinguishes Einstein, translator Lawson and production contributors; these roles do not automatically identify the note's speaker.

**Meditations correspondence appendix.** It mixes later commentary with letters by Marcus and Fronto. A single unknown-editor rule would conceal the letters' different authors. Separate letters from their framing commentary or use span-level provenance.

**Darwin edition-navigation notices.** Both editions contain an initial electronic navigation notice. The title fragment also matches other front matter/epigraph or contents sections, so it cannot isolate the digital notice safely.

**The Prince's short front biography.** Its generic title fragment also matches main-chapter headings containing the plural word “Princes.” Substring matching is not whole-word matching. The JSON lists these collisions and rejects the rule.

Other unresolved cases are recorded as controls rather than broad proposals: Voltaire's inline notes signed only with an initial, Marriott-edition footnotes quoting Burd, and Melville's anthology of explicitly credited extracts. An initial or a translator credit is insufficient to invent a note compiler's identity; an anthology needs nested quotation attribution rather than an assumed single speaker.

## Controls against overclassification

Einstein's preface is signed by Einstein. Darwin's introductions describe his own research. Smith's introductions explain his own argument and plan. Hobbes's dedication is signed by Hobbes. Nietzsche's dated preface and translated closing poem remain authored content. Wells's chapter called Introduction is a narrated fictional scene. Austen's chapter caption mentioning a note is narrative, not an editor's note.

These cases demonstrate why a generic rule that every Preface, Introduction, Appendix or Notes heading is editorial would create new errors. A translator-named heading can introduce either genuine translated literary content or an electronic notice; the body must be inspected.

## EPUB contributors and current product behavior

The original EPUB OPF metadata was inspected directly for all 20 artifacts. Six explicit translator records appear across War and Peace, The Prince, The Republic, Beyond Good and Evil and Relativity. The Communist Manifesto has two author creator records. OPF metadata does not exhaust contributor knowledge: Saintsbury and Dallas are explicit in body text but missing from those EPUB creator/contributor lists.

The current parser's EPUB metadata helper takes the first creator and does not expose the full contributor-role list. Curated manifest metadata currently preserves the two Manifesto authors and places several translator credits in edition descriptions, but a generic local import can lose structured contributor information. A future format layer should preserve all creator/contributor identities and MARC role refinements, while keeping book authors, translators, editors, illustrators and production credits distinct.

In the inspected implementation, search results retain catalog `author` separately from `section_author`, `section_role` and attribution basis. A verified different section contributor prefixes the citation while the original book author remains in the bibliographic context. Full-text author indexing includes an explicitly different contributor, while the catalog's title/author substring filter searches book metadata. Search has book/language filters but no dedicated contributor-role filter. Consequently an author comparison must not treat every hit packaged inside a Plato or Marcus ebook as that author's own position.

The `verified` quotation flag proves text/offset/hash integrity, not correct speaker identity. An unclassified section still has the book author as catalog context, so clear anonymous editorial prose can appear under a misleading author heading unless its role is surfaced or explicitly classified. Frozen benchmark quote-exactness scores do not clear this attribution risk; the expanded fixture already retains manual role probes, including the unresolved Casaubon notes case.

Apply any accepted proposals only after the paired frozen-snapshot search work, to a new live corpus state with fresh attribution/index fingerprints and validation. Keep the historical snapshot and evaluation receipts intact.
