# Expanded development fixture over full books

`evals/dev-expanded.json` is a manually grounded **development** fixture. It expands the original excerpt experiment to 20 curated full Project Gutenberg editions representing 19 works. The two Darwin editions deliberately share a work identity while remaining separate edition targets. This fixture is not a new held-out test, and no historical held-out query file was opened, rerun or changed while creating it.

The source is the stable filtered snapshot `data/snapshots/curated20-v3/library.sqlite3`, containing exactly the 20 edition IDs listed in the fixture's `required_book_ids`: 20 indexed editions, 19 works and 12,108 passages. The snapshot excludes the eight demo excerpts, automatically discovered additions and the separately acquired modern partial work. Use this exact subset for paired model comparisons; adding more books changes retrieval difficulty and requires a new report.

The checked-in `corpus/manifest.json` is the source manifest. Its digest, each edition's artifact/content hashes, bibliographic metadata and indexing-version fingerprint are retained in `experiments/dev-expanded-audit.json`. Grounding was checked in a read-only SQLite transaction after the snapshot's offline parser refresh. The main database file had an empty WAL when its SHA-256 was measured.

| Frozen input | SHA-256 |
| --- | --- |
| Dataset | `8955a6cf471318fa70d24f518c3a08f410865f9a9f4a751f47d29de3a94deb44` |
| Source manifest | `a0916d1a489e9e99f54aca90f2f98348a46b03e5a8ffbfae42fea363ced82c4a` |
| Checkpointed SQLite main file | `ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8` |

Database byte hashes can change after legitimate database maintenance even if canonical source text is unchanged. Retain the artifact/content/metadata digests and canonical evidence anchors when rebuilding. Do not silently reuse scores after changing the source snapshot or labels.

## Query coverage

The fixture contains 54 natural-language queries: 44 answerable and 10 explicitly scoped negatives. Query languages are English (34), Russian (8), French (6) and German (6).

| Type | Cases |
| --- | ---: |
| Exact quotation | 5 |
| Idea | 3 |
| Argument | 5 |
| Scene | 5 |
| Approximate book description | 2 |
| Explanation | 4 |
| Cross-language | 12 |
| Footnote | 2 |
| Source/contributor role | 2 |
| Comparison | 4 |
| Negative | 10 |

The cross-language subset contains four Russian, four French and four German queries over twelve different English source editions. It includes the croquet scene with flamingoes and hedgehogs, Darcy's letter, rudimentary organs, public education, the creature's request for a companion, the Rachel's missing child, Prince Andrew's view of greatness, the fox/lion argument, the cave, train-relative simultaneity, over-production and the Morlocks' response to light.

Queries reach beyond book openings: 31 of 48 reference spans lie in the latter half of their book's section sequence, including 14 in the final fifth. These are section-position counts, not estimates of textual/page coverage. Examples include Alice's trial, Austen's late proposal, Darwin's conclusion, the final book of Meditations, Candide's conclusion, Ahab's last chase, the epilogue's coffin, Tolstoy's philosophy-of-history discussion and Einstein's experimental appendix.

The ten negatives ask for a situation or argument while the user has a different full book selected. Each uses an explicit `filters.book_id`. Distinguishing named entities/concepts were checked absent from that selected book's entire extracted text. They are plausible scope mistakes, not artificially exotic keyword strings. The audit records every checked concept and the selected edition. This is supporting evidence for the manual scope judgment; absence of a word alone is not a general proof that no semantically equivalent answer exists.

## Relevance and evidence labels

All answerable cases specify `relevant_book_ids`, so scoring distinguishes editions. `expected_evidence` records an exact source fragment, the book/work identity, section ordinal and title, and contributor expectations when justified. The audit additionally records source offsets, surrounding context, observed section attribution and matching passage IDs. Every one of the 48 reference fragments occurs exactly in its source section and in at least one complete indexed passage.

Passage queries use `evidence_contains` to require a labeled reference fragment. These are conservative, manually chosen reference passages, not exhaustive relevance judgments; another relevant explanation may be retrieved without matching the selected wording. Inspect apparent failures before drawing broad quality conclusions.

The two approximate-book-description queries have `relevance_unit: "book"` and deliberately omit the phrase requirement: locating the correct work is the user outcome. Their source fragments remain in `expected_evidence` for grounding the description's premise. The four comparison queries target both source books/editions and retain reference evidence for each. MRR can reward the first relevant source while recall reflects whether both labeled sources were found. The current metric named `work_recall_at_k` uses the supplied book IDs here, so interpret it as recall of the labeled **edition targets**, not a separate work-deduplication score.

The Darwin comparison asks about the complex-eye argument in the 1859 and sixth editions. It does not manufacture a generated comparative answer: exact passages from both editions support manual inspection of their different wording.

## Contributor and footnote checks

The Republic's introduction is explicitly attributed to Benjamin Jowett by the source manifest's contributor rule. Its reference span compares the Republic with the Laws and other dialogues; the expected section role is `editorial`, with Jowett as section author. The cave reference is separately grounded in `BOOK VII.` with Plato as the observed section author. This prevents treating every paragraph packaged in a Plato ebook as an unqualified statement by Plato.

A second probe finds the Meditations `NOTES` passage explaining that Casaubon's own notes were omitted and the 1634/1635 editions compared. The text plainly discusses later editorial work. The editor's personal identity is not established by this audit and is not invented. The intended fragment role is editorial, while the current stored section role is `unclassified`; the mismatch is preserved in `known_manual_role_gaps`.

The Prince footnote cases identify Georges d'Amboise and a note quoting Burd about chapter XVIII. They are notes embedded within mixed chapter text. The audit does not assign the entire surrounding chapter to an editor or invent the note compiler's identity.

The current evaluator does **not** automatically score `expected_evidence` contributor roles or enforce a particular section. Its quotation/bibliographic checks cannot be reported as proof that all inline/editorial authorship is correct. Keep these cases as explicit manual attribution probes until a separate role-sensitive metric is implemented.

## Running later comparisons

No retrieval scores were generated during fixture creation. First retain this frozen input, then run comparisons against the same snapshot, query fixture and candidate depth. For example:

```sh
BOOKRAG_DATA_DIR=data/snapshots/curated20-v3 uv run bookrag evaluate \
  --dataset evals/dev-expanded.json --mode baseline \
  --output experiments/expanded-baseline.json
```

Semantic/hybrid evaluation additionally needs a compatible index for this exact snapshot. Check `actual_search_modes` and warnings so a lexical fallback is not mistaken for a semantic run. Reranking comparisons need the same upstream candidate pool for the control and candidate. Measure cold model initialization separately from warmed requests when reporting latency.

The audit is evidence of grounded labels and source identity, not search quality. Scores on this development fixture may guide experiments; generalization claims need a newly collected, untouched final-test set. Historical held-out results remain frozen.
