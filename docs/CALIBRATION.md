# Exploratory retrieval score floors

This experiment asks whether a raw model score can help decide when to abstain from presenting the top passage as supporting evidence. It does **not** turn cosine similarity or ranking logits into answer probabilities. No thresholds were applied to the application, no defaults changed, and no historical held-out fixture was read or changed.

The relevant research framing is selective prediction: accepting fewer cases trades coverage for fewer errors. [Geifman and El-Yaniv](https://arxiv.org/abs/1705.08500) study this risk/coverage tradeoff with explicit statistical assumptions and methods. The small development experiment here is an empirical diagnostic; it does not implement that paper's risk guarantee or inherit its guarantees.

## Labels and score units

Each example has `id`, `score`, `answerable` and `top_result_relevant`. Scores must be finite and higher must mean better. `score=None` means no retrieved result and always abstains.

A correct acceptance requires the returned top passage to match the manually labeled source book/edition and any reference evidence phrase. The runner independently loads the source section and checks exact quotation, bibliographic metadata and reader anchor. A query can be answerable while its current top result is wrong; accepting that result is a false acceptance too. Counting only explicitly unanswerable queries would miss this important failure mode.

The cosine experiment uses `semantic_score` from normalized MiniLM embedding windows. In its own process only, it sets the upstream floor to -1 to observe lower scores without censoring them at the existing 0.3 floor. Exact phrase constraints and candidate shortlist behavior remain active, so some answerable quote queries still return no eligible result. These are recorded as missing scores, not invented confidence values.

The logit experiment requests 50 hybrid candidates with the existing upstream cosine floor 0.3 and then reranks them. It reads `reranker_score`, never the hybrid RRF score. Rank-fusion values depend on list position and fusion mechanics; treating them as raw model confidence would be an invalid substitution. The cross-encoder's raw logits are ranking scores, not calibrated probabilities, as described in the [Sentence Transformers usage documentation](https://www.sbert.net/docs/cross_encoder/usage/usage.html).

## Metrics and selection

`book_rag.calibration` exposes `evaluate_threshold`, `threshold_table`, `select_threshold`, `deterministic_split` and `calibrate`. Thresholds are inclusive (`score >= threshold`), with tied scores accepted together. A threshold of `None` is a deliberate abstain-all policy; its precision is undefined rather than 100%.

The table reports:

- Precision: correct top-result acceptances divided by all acceptances.
- Recall: correct acceptances divided by all answerable queries, including answerable queries whose top result is wrong/missing.
- Correct-top retention: the fraction of initially correct top results retained by the floor.
- False acceptances split into scoped-negative acceptances and wrong-top acceptances on answerable queries.
- False-accept rate among examples whose top result is not labeled correct, negative abstention, total coverage and missing-result counts.
- Descriptive 95% Wilson intervals for precision and negative abstention. [Brown, Cai and DasGupta](https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.pdf) analyze binomial interval methods and small-sample behavior. These curated queries are not an IID population sample, so the displayed intervals are not deployment guarantees.

Precision selection uses a predeclared observed target of 0.90 with at least three accepted training cases. It maximizes retained correct results among policies meeting that target. If no policy meets it, the report explicitly says `training_target_not_met` and records abstain-all; it does not describe that fallback as successful precision calibration.

An optional utility objective uses declared rewards/costs: correct acceptance reward, false-accept cost and missed-answer cost. The public selector supports this alternative, but the recorded runs use the precision objective. Choosing a different objective after reading validation results would require a new experiment declaration and fresh validation data.

## Internal split and provenance

The 54-query expanded development fixture is partitioned by a deterministic SHA-256 ordering of query IDs, with fixed seed `book-rag-score-floor-v1` and 30% validation. No labels guide the partition. Both model runs use the same 38 training IDs and 16 validation IDs. Training contains 30 answerable and eight negative queries; validation contains 14 answerable and only two negatives.

A threshold is selected only on training examples and evaluated once unchanged on internal validation. A separate full-data table is clearly labeled `in_sample_diagnostic`; it is optimistic, not independent evidence. The validation output must not be used to search seeds or tune the chosen floor. This split is internal to a development set that has already informed retrieval work, so it is not a final generalization test.

The runner uses an owned temporary SQLite copy with read-only query connections,
so current schema migrations never touch the frozen source. It checks the exact
required edition subset, records original database/corpus/dataset digests and
active index metadata, and rejects a changed database or vector generation.
Ready vectors and large completed weights are reused without physical copying;
cache metadata is owned. See [evaluation isolation](EVALUATION.md#preserve-frozen-input-bytes).
It preserves raw per-query scores, source snippets/locations, independent source
checks, model revisions, upstream candidate settings, truncation warnings and
timing. Frozen corpus and evaluation inputs remain unchanged.

## Recorded results

Reports: `experiments/calibration-minilm-v1.json` and `experiments/calibration-reranker-v1.json`.

| Declared precision target 0.90, minimum 3 training acceptances | MiniLM cosine | Cross-encoder logit |
| --- | ---: | ---: |
| Training-selected floor | 0.768708 | No feasible floor |
| Training acceptances | 4 / 38 | 0 / 38 (target unmet) |
| Training correct acceptances | 4 | 0 |
| Internal-validation acceptances | 1 / 16 | 0 / 16 |
| Internal-validation correct acceptances | 1 | 0 |
| Internal-validation answerable recall | 1 / 14 = 0.0714 | 0 / 14 |
| Internal-validation precision | 1 / 1 | Undefined |
| Internal-validation negative abstention | 2 / 2 | 2 / 2 |

The cosine floor retains only one validation result. Its nominal 1.0 precision has a Wilson interval of approximately [0.207, 1.000]; perfect abstention over two negatives has an interval of approximately [0.342, 1.000]. Those counts cannot support a universal answerability threshold. A policy that discards nearly all useful results is also a poor product outcome.

No logit floor accepting at least three training examples reaches the declared 0.90 target. The best observed training precision among such policies is 0.75 (12 correct of 16 acceptances). All four errors there are wrong-top results on answerable queries, even though all eight training negatives are rejected. This illustrates why filtering unrelated questions alone does not establish reliable evidence selection.

The raw-score collection took 14.2 seconds for cosine and 129.4 seconds for hybrid-plus-cross-encoder on this local run, including initialization and concurrent local work. These are collection costs, not warmed per-request benchmarks. The cosine run had three missing results and the logit run two; all remain explicit abstentions in every threshold table.

These observations do not justify changing the default score floors. More independent, densely judged development cases, especially wrong-passage and wrong-source examples, are needed. Labels here require selected exact reference anchors and can reject other genuinely relevant passages; the complete reports must be inspected before interpreting every error as semantic irrelevance. A relevant top result for a comparison also does not supply every source needed for a complete answer. Contributor roles and entailment remain separate checks.

## Reproducing the experiment

```sh
BOOKRAG_MODEL_CACHE="$PWD/data/models" uv run python scripts/calibrate_retrieval.py \
  --kind cosine --data-dir data/snapshots/curated20-v3 \
  --dataset evals/dev-expanded.json --target-precision 0.9 --min-accepted 3 \
  --output experiments/calibration-minilm-v1.json

BOOKRAG_MODEL_CACHE="$PWD/data/models" uv run python scripts/calibrate_retrieval.py \
  --kind logit --data-dir data/snapshots/curated20-v3 \
  --dataset evals/dev-expanded.json --target-precision 0.9 --min-accepted 3 \
  --candidate-depth 50 --upstream-floor 0.3 \
  --output experiments/calibration-reranker-v1.json
```

The commands require the compatible frozen index and optional `semantic` dependencies. Use a new output filename for a new experiment rather than overwriting a published receipt. The script refuses a fixture whose declared split is not `dev` and never builds or modifies an index. Saved `examples` can be passed directly to the pure calibration functions for reproducible arithmetic without rerunning model inference; any additional policy exploration must remain labeled as development analysis.
