# Reproducible retrieval evaluation

`evals/dev.json` is the development fixture. `evals/heldout.json` is a separate final check. Both are small, manually labeled queries over the demo excerpts; neither represents quality over a large general library. The held-out queries must not guide query expansion, synonyms, score weights, chunking, or prompt changes. Expand the test corpus and ground truth before making broader quality claims.

Run the `bookrag evaluate` command against a local index, select the dataset and search mode, and save JSON under `experiments/`. Compare the lexical baseline and any candidate on exactly the same corpus snapshot, fixture digest and `k`. Runtime reports include the fixture SHA-256 and library counts. Model identity and index revision must accompany semantic experiments. Run each mode in a fresh process for meaningful memory comparisons.

Dataset version 1 contains `split`, a list of `queries`, and optional descriptive fields. Each query has `id`, `query`, explicit `answerable`, `language`, `type`, and `relevant_work_ids` or `relevant_book_ids`. `evidence_contains` optionally requires one of the listed source phrases in a relevant passage. A correct work alone is insufficient for a scene or exact-quote hit when this condition is present. Optional `filters` select a book or language. Empty results constitute the non-generative search baseline's abstention signal.

The evaluator records:

- MRR@k and hit@k over answerable queries, including scene, idea, quote, description and cross-language cases.
- Work recall@k over explicitly labeled works, independently of the phrase condition; precision@k uses the conventional denominator `k`, even when fewer results return.
- Negative abstention accuracy, separately from answerable retrieval scores. Semantic search may return irrelevant neighbors; their presence must not be treated as evidence of an answer.
- Per-query type and query-language breakdowns. The language label describes the query, which can differ from the source text.
- Exact quote, bibliographic metadata and reader-anchor rates over every returned passage, including irrelevant ones.
- End-to-end retrieval p50/p95 wall time and process peak resident memory. Memory includes Python and imported modules; this is not incremental model memory. These numbers do not measure index-build cost or paid API cost.

Citation verification independently loads the book and section and checks `section.text[start:end] == result.text`, correct title/author/edition, their presence in the displayed citation, section title, and all three reader query parameters. It never trusts the search response's `verified` boolean. Offsets are zero-based Unicode code points into canonical parsed text, not UTF-8 byte counts, printed page numbers, or PDF coordinates. This proves fidelity to extracted text; it cannot establish OCR accuracy or validate a physical edition.

Keep measured failures in reports. In particular, cross-language lexical failures and out-of-corpus semantic false positives are expected limitations to investigate, not cases to relabel away. A larger catalog changes retrieval difficulty and must have a separate evaluation run. A score from eight excerpts cannot be transferred to thousands of books.

The automated evaluation tests deliberately corrupt quotes, offsets, attribution and reader anchors and assert that verification detects the corruption. They also check rank metrics and avoid reporting perfect citation rates when there are no returned passages.

Reports include a corpus digest and source snapshot (artifact/content hashes and bibliographic metadata), Python-module hashes, Python/platform information, actual returned search modes, and embedding model names when supplied by retrieval. Requested semantic search that falls back to lexical search is visible in `actual_search_modes` and per-query warnings. `compare_reports(baseline, candidate)` rejects different dataset/corpus digests or `k` and returns signed metric deltas. Pin model revisions and retain model-build metadata for full semantic reproducibility.

The initial development fixture contains 23 queries: 18 answerable questions, including 4 cross-language questions, and 5 negatives. Some negatives use normal question wording with common function words; a keyword search must not be credited with correct abstention solely because artificially exotic query tokens never occur. The held-out fixture contains 13 separate questions and is reserved for the final check of a frozen candidate. Evaluation tests ensure labeled evidence phrases actually occur in the checked-in demo source and query text does not overlap across splits.
