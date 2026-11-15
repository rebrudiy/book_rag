# Read-only canonical text overlap audit

The audit proposes text relationships for human review. It never edits books,
work IDs, epochs, passages, semantic receipts, source files or existing curated
links. It makes no network requests and uses no language model. Similarity is
not a count of confirmed unique literary works.

```sh
.venv/bin/python scripts/audit_content_overlap.py \
  --database data/full/library.sqlite3 \
  --output data/identity/content-overlap-v1
```

Use a new output directory for a new run; previous scratch/results are not
silently overwritten. Runtime outputs are ignored by Git. The source database is
opened with `mode=ro`, `query_only=ON`, and a consistent read transaction. A guard
hash binds sorted indexed IDs, titles, authors, languages, work IDs, canonical
and artifact hashes, source URLs and rights. The guard is checked again after
releasing the snapshot so a concurrent source change cannot be silently ignored.
No notes or other user records are queried.

The allowed sources are the official public Gutenberg/Wikisource/Wikibooks
hosts and the D2L GitHub project. Records need an explicit existing public-domain
or CC BY-SA rights declaration and valid artifact/canonical hashes. These are
input-scope guards, not a new legal audit of texts. Unknown rights and arbitrary
GitHub sources are skipped, never read as candidate book bodies.

## Fingerprints and proof

Only `sections.text` is scanned, in canonical chapter order. Overlapping passages
are never treated as independent text. Paragraph boundaries are blank lines;
all source offsets are retained. Fingerprinting normalizes NFC, Unicode case and
whitespace. Punctuation, numbers, original prose, code and editorial sections are
retained. Paragraphs below80 normalized characters do not enter this experiment.
Only explicit Gutenberg start/end/license packaging is excluded by content;
human prefaces and mentions of Gutenberg are not guessed away.

A bounded bottom256 sample of unique64-bit BLAKE2b paragraph hashes represents
each book. A scratch SQLite primary key groups the samples by hash. Hashes seen
in more than20 sampled books are suppressed during candidate generation to limit
common packaging/boilerplate. That frequency is measured over samples, not every
paragraph of the entire corpus. Candidate pairs require at least four shared
sampled hashes. At most100,000 pairs are counted; hitting this bound is explicit
and limits recall. The top200 pairs by shared sample count are checked further.

Candidate verification re-reads complete eligible paragraph sets with SHA256.
It records shared distinct paragraphs, paragraph Jaccard and character overlap
in each direction. **The character denominator is distinct eligible normalized
paragraph characters, not all canonical book characters.** Short headings,
repeated paragraphs and paragraphs under the threshold are outside that measure.
Three shared paragraphs per pair are read back from their exact section offsets,
checked for literal equality after normalization, and retained with raw source
SHA/offsets for review. Raw source text is never rewritten. Normalized similarity
is not permission to substitute one edition's quote for another's exact text.

Proposed relationships are deliberately tentative:

| Proposal | Interpretation |
| --- | --- |
| `high_overlap_edition_candidate` | At least75% overlap in both eligible-character directions; inspect edition and contents before linking |
| `possible_containment_or_excerpt` | At least50% overlap in one direction; a story inside an anthology is not identical to its container |
| `shared_passages_only` | Shared text below those thresholds; quotations, notes and common material may explain it |
| `cross_language_shared_text_not_translation_proof` | Exact shared text across language labels; no claim that the books are translations |

Every proposal has `identity_verified=false` and `manual_review=pending`.
Existing common work IDs are shown separately as prior curation, not evidence
newly discovered by this audit. Candidate discovery has incomplete recall,
especially a short work within a long anthology, paraphrases, resegmentation,
changed punctuation, or translations. False positives are expected and must be
measured by review rather than automatically merged.

## Resource bounds

The implementation uses one Python process, no model/accelerator and no corpus
payload downloads. Its scratch database is capped at90MiB, outputs at100MiB and
measured peak process memory at500MiB. It retains at most three full per-book
paragraph hash maps while verifying pairs. SQLite caches are bounded. Progress
is written every100 books; source guards, processed sections/characters,
paragraph/filter counts, elapsed time, candidate saturation, peak RSS and scratch
bytes are part of the result. These bounds do not include the existing immutable
source database. If a bound fails, the audit stops without source writes.

The small public result and concrete reviewed examples are recorded separately
once the actual full-corpus audit completes. The runtime report includes source
proof excerpts; it should not be copied wholesale into Git.

## Full-corpus result, 2026-11-15

The actual run audited all11,001 indexed public books:368,087sections,
2,716,496,031canonical characters and11,927,313paragraphs. It retained1,927,219
sample hashes, suppressed38 overly common sampled hashes and did not reach the
candidate cap. It found4,377 pairs with a sample vote and fully checked the
top200. Runtime was95.764s (92.887s initial scan), peak RSS125.5MiB and scratch
32.4MiB. All source guards remained current. No source or production database writes, model calls, or network
requests occurred. All600 retained pair-source examples were read
back and matched after the declared normalization.

The200 checked pairs produced3 high-overlap edition candidates,28 containment
candidates,168 shared-passage candidates and1 cross-language case. These are
proposals, not200 duplicate works. Eleven examples received a further purposive
source inspection; this is not a random precision estimate and is not human
identity approval. In particular:

| Sources | Observed relationship and implication |
| --- | --- |
| PG6342 / PG6343 | Kant1781/1787 revised editions share705 normalized paragraphs. A shared-work proposal must preserve different edition citations. |
| PG52488 / PG52489 | Lavoisier volume2 occurs within the combined volumes1–2; part and complete edition scopes differ. |
| PG10705 / PG10706 | Mommsen BookV and the five-book compilation explicitly describe the compilation in their source notes. |
| PG1001 / PG1004 | Longfellow's Inferno occurs within the complete Divine Comedy. |
| PG28146 / PG51580 | The second Nietzsche edition also includes Homer and Classical Philology, despite high overlap; whole files must not be equated automatically. |
| PG53298 / PG53388 | Different Guptill plays share extensive publisher advertisements. Their60%/53% eligible-character overlap is a false signal for whole-work merging. |
| PG58377 / PG58489 | Dated Spon publisher catalogues repeat advertisements; these are not individual scientific monographs. |
| PG10091 / PG10092 | Different Punchinello issues share subscription/advertising material and remain separate issues. |
| PG3617 / PG7541 | Editorial quotation selections from Dumas are not the complete underlying original; David Widger's explicit byline needs separate contributor scope. |
| PG23313 / PG38692 | Tonio Kröger appears in distinct printed editions, but exact paragraph segmentation retains only9–10% eligible-character overlap. Low overlap cannot prove different works. |
| PG10814 / PG53552 | The EN-labeled Bécquer educational reader contains Spanish originals with English editorial material. Shared Spanish text is not evidence of an English translation. |

All three automatic high-overlap cases were inspected: publisher catalogues,
Nietzsche with an added work, and editorial quotation selections. None is an
approved automatic merge of complete original works. This demonstrates why an
identity counter cannot subtract high-scoring pairs. Shared advertising must be
separated from narrative/body evidence before a later merge proposal can be
trusted. Low recall under paragraph resegmentation also needs measurement before
using these scores as a duplicate detector.

The runtime source inspections preserve first-section observations, source
URLs/artifact hashes, paragraph proof hashes and explicit pending human review.
The small public metadata result is `experiments/content-overlap-v1.json`; no book
text is added to the repository. Seven isolated SQLite tests cover offsets,
Unicode normalization, sample determinism, containment, cross-language caution,
common-hash suppression, saturated candidate reporting, source-rights scope,
existing-result protection and the memory guard. Ruff and ty also pass.
