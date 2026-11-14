# Lexical retrieval and experiments

## User scenarios

A reader can find an exact phrase, locate a scene using several remembered details, or search for a book using author/title metadata. A question about something absent from the library should not return unrelated paragraphs merely because they contain “the”, “a”, or “in”. A research board needs source-text support stronger than a title match.

The `baseline` mode preserves the original Unicode FTS5 OR query and BM25 ranking. The `lexical` mode is a separate candidate: remove function words, retrieve candidates through FTS5, reject weak overlaps, and rerank by meaningful term coverage. `semantic` and `hybrid` remain explicit experimental modes. Search never requires a generative API.

## Pure query helpers

`prepare_query(query)` returns `QueryPlan`: `terms`, tokenized `phrases`, safe FTS5 `expression`, and an empty-query `reason`. All FTS syntax is generated from Unicode alphanumeric tokens and mandatory quoted token phrases. A user cannot inject an FTS operator. Straight, curly, French and German quotation marks are recognized. Quoted phrases retain function words and use FTS token adjacency; punctuation and case do not distinguish a phrase.

English, Russian, French and German have separate function-word lists. Cyrillic script selects Russian; a lightweight count of function words selects a Latin-script language, with English as the fallback for ambiguous short queries. The lists are not unioned: otherwise German “war”, “die”, “hat” and French “son” erase ordinary English content words. This heuristic cannot reliably identify every language or disambiguate every homograph. Negations remain meaningful in every list. Quoting a literal phrase bypasses function-word deletion.

Latin accent folding approximates SQLite `unicode61 remove_diacritics 2`. German `ß` stays `ß`: Python's full `casefold()` changes it to `ss`, which SQLite does not match. Cyrillic `й` and `ё` remain intact. This avoids an otherwise silent exact-word regression in German.

`rerank_results(plan, candidates, limit, strict=False)` retains metadata and the input BM25 value as `baseline_score`. It returns a descending coverage `score`, `matched_terms`, `term_coverage`, and `research_supported`. Retrieve a larger candidate pool before applying it; the integrated library currently uses a bounded pool. Coverage counts distinct query terms across source text and title/author/section metadata. For two unquoted meaningful terms, partial search needs one; for three or more, it needs at least two and 35% of the terms. Quoted phrases must occur in source text. Equal-coverage candidates retain their original BM25 order, preserving term rarity/frequency signals.

`has_research_support(plan, result)` requires overlap in source text itself: at least two terms and 60% for multiword queries. Metadata alone cannot qualify. The research board uses this stricter check. The flag expresses lexical overlap; it does not establish entailment, truth, contradiction, relevance of a date, or whether the passage answers the question.

An early candidate added a tiny proximity bonus using the span from the first to last matching token. A development scene query exposed that equal-coverage passages swapped BM25 order based on insignificant span differences. The bonus was removed for all queries, preserving BM25 order on coverage ties. No held-out queries were used.

## Development evidence

The immutable development fixture has 23 queries over eight demo excerpts: 18 answerable, including four cross-language questions, and five negative queries. `experiments/baseline-dev-v2.json` and `experiments/lexical-dev-v2.json` form a paired run after the Russian poem's verse structure was preserved. The corpus metadata/content digests and fixture digests match. `experiments/lexical-dev-v2-comparison.json` records signed differences.

| Metric, k=5 | Original baseline | Enhanced lexical |
| --- | ---: | ---: |
| MRR | 0.7778 | 0.7778 |
| Hit rate | 0.7778 | 0.7778 |
| Negative abstention | 0.60 | 1.00 |
| Cross-language hit rate | 0.00 | 0.00 |
| Exact quote / attribution / reader anchor | 1.00 | 1.00 |
| Returned passages across 23 queries | 83 | 39 |

The enhanced mode removes false positives for two naturally worded out-of-corpus questions while preserving the development set's answerable retrieval. It does not solve cross-language retrieval. This is evidence on a tiny excerpt collection; it is not a quality estimate for a library of thousands of books. Process timing and memory measurements are in the JSON reports and include initialization/import effects. Do not infer production latency from these fixture runs.

## Rejected first semantic candidate

The first multilingual MiniLM index prepended book title, author and section to a whole passage, while the encoder accepted only 128 tokens. This truncated much of the passage and caused missing evidence near its end. Its development run is preserved in `semantic-dev-v1.json`: MRR 0.6963, hit rate 0.8889, cross-language hit rate 0.75, exact-quote-query hit rate 0.8571, and negative abstention 1.00. Exactness of the returned quotations themselves remained 1.00; the regression concerns retrieval of the requested quotation.

The first hybrid fusion (`hybrid-dev-v1.json`) reached MRR 0.7407 and hit rate 0.8889, with the same 0.75 cross-language hit rate, but retained the baseline's 0.60 negative abstention. Both modes are rejected as replacements for default lexical search: cross-language improvements came with exact-query ranking/retrieval losses, and hybrid still admitted function-word false positives. These v1 runs describe their recorded corpus snapshots; corpus changes must be checked by digest before computing a formal paired comparison.

The next semantic experiment should encode overlapping token windows across every passage, retain model revision/windowing/build metadata, and combine semantic candidates with the improved lexical path. Evaluate on the development split first and inspect regressions by query type. Freeze the chosen candidate before the separate held-out check; never tune against held-out output.

## Limits and next investigations

Lexical overlap does not recognize paraphrases or inflections, and stopword language detection is heuristic. The bounded candidate pool can exclude a relevant lower-ranked passage on a much larger corpus. Phrase matching follows FTS tokenization rather than byte-for-byte punctuation. Long questions with many distinct concepts can exceed overlap thresholds even when a short passage answers part of them. Strong research overlap still cannot prove a valid answer or an opposing argument. Cross-language embeddings, tokenizer-aware windows, learned reranking and larger human relevance judgments need separate measured experiments.

## Token-window semantic candidate v2

The v2 encoder covers a passage with overlapping model-token windows and preserves the source passage as the citation unit. Its compatible development comparisons against `baseline-dev-v2.json` are saved as `semantic-dev-v2-comparison.json` and `hybrid-dev-v2-comparison.json`.

| Development metric, k=5 | Baseline | Lexical | Semantic v2 | Hybrid v2 |
| --- | ---: | ---: | ---: | ---: |
| MRR | 0.7778 | 0.7778 | 0.8102 | 0.8935 |
| Hit rate | 0.7778 | 0.7778 | 0.9444 | 1.0000 |
| Cross-language hit rate (4 cases) | 0 | 0 | 1 | 1 |
| Negative abstention (5 cases) | 0.60 | 1.00 | 1.00 | 1.00 |
| Exact quote / metadata / anchor | 1 | 1 | 1 | 1 |
| Retrieval p50, milliseconds | 0.667 | 1.224 | 32.205 | 34.443 |
| Process peak memory, MiB | 36.17 | 36.75 | 841.25 | 841.23 |

These runs start a fresh Python process. The first semantic query includes model initialization; subsequent queries reuse it. Thus percentile latency mixes one cold request with warm requests, and process peak memory includes model initialization. A reliable deployment latency claim needs separate cold-start and warmed-loop benchmarks under realistic corpus sizes and concurrent load.

## Frozen v0.2 held-out check

After freezing the candidate, each mode was evaluated once on the separate 13-query held-out fixture (10 answerable, 3 negative). Reports and comparisons remain in `experiments/release-v0.2/`. These results were observed after development; they must not be used to tune this release.

| Held-out metric, k=5 | Baseline | Lexical | Hybrid v2 |
| --- | ---: | ---: | ---: |
| MRR | 0.7500 | 0.6500 | 0.9333 |
| Hit rate | 0.8000 | 0.7000 | 1.0000 |
| Cross-language hit rate (2 cases) | 0 | 0 | 1 |
| Negative abstention (3 cases) | 0.6667 | 1.0000 | 0.6667 |
| Exact quote / metadata / anchor | 1 | 1 | 1 |
| Retrieval p50, milliseconds | 0.773 | 1.695 | 26.739 |
| Retrieval p95, milliseconds | 1.406 | 5.814 | 2341.712 |

Hybrid retrieves every labeled answerable held-out case, but returns a semantic false positive for a query explicitly scoped to a book that does not contain the requested subject. Enhanced lexical avoids all negative false positives but rejects an unseen explanation query and ranks another answer lower. These are material limits: development abstention did not generalize perfectly for hybrid, and conservative lexical thresholds trade recall for fewer false positives. Exact quotation integrity does not prove relevance. The hybrid p95 is dominated by the cold model load in this short run.

No held-out query was relabeled or used to adjust a threshold. Future retrieval changes should draw on independently collected development judgments and use a new, untouched final-test split; repeating this fixture can serve only as a regression check, not a new unbiased final test.
