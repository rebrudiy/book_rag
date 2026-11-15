# Retrieval with thousands of distractor editions

The completed [v3 development comparison](../experiments/large-subset-dev-v3/summary.json)
uses the same 54 inspected development queries, canonical corpus and implementation
for baseline, enhanced lexical and hybrid retrieval. All 54 requests actually ran
in each requested mode. Source guards passed. The corpus had 3,991 editions and
1,401,120 passages; the semantic subset covered 1,308 editions, including the
known target editions. Subsequent Russian imports are outside this measurement.

Ground truth is agent-authored over 20 known target editions. Fifteen queries
search globally; others use explicit filters. Additional relevant editions may
be unlabeled. These are development results with more distractors, not blind
general relevance scores over the entire library.

| Metric at k=5 | Baseline | Enhanced lexical | Hybrid |
| --- | ---: | ---: | ---: |
| Positive MRR (44 cases) | 0.5803 | 0.5076 | 0.5928 |
| Positive hits | 30/44 | 24/44 | 32/44 |
| Cross-language hits | 4/12 | 0/12 | 7/12 |
| Correct abstention on scoped negatives | 4/10 | 9/10 | 2/10 |
| Exact returned quote / metadata / reader link | 100% | 100% | 100% |
| p50, ms | 116.6 | 54.5 | 1,896.5 |
| p95, ms | 13,038.3 | 6,135.4 | 13,465.0 |

All 597 returned passages across the three runs passed independent canonical
text, bibliographic and reader-anchor checks. This establishes quotation fidelity,
not relevance or correctness of an answer. Hybrid gains two positive hits over
baseline but worsens negative abstention. It also regresses on some description,
idea and contributor queries. It is not a uniform improvement. Search results
remain evidence to inspect; no generated answer or author stance is asserted.

The runs share a process and warm caches; process peak RSS is cumulative.
The hybrid peak was about 3.46 GiB, including its model and mapped vectors.
Treat these timings as observations, not isolated throughput comparisons.

## Rejected and fallback-only runs remain visible

- [v1](../experiments/large-subset-dev-v1/discarded.json) was discarded by the
  source-identity guard. A real external read-only SQLite client reproduced a
  false rejection from transient coordination sidecars. The exact original
  differing identity fields were not logged; this cause assessment is explicit.
- [v2](../experiments/large-subset-dev-v2/summary.json) completed with all hybrid
  requests falling back to lexical. It measures fallback availability, not
  semantic relevance. The isolated cache copy had dropped valid relative HF
  snapshot links. The fixed path was verified with a real offline MiniLM load
  and encode before v3.

Phase journals record progress and preserve explicitly unvalidated phase reports
on failure. Successful reports require unchanged canonical inputs, development
labels, runner and relevant retrieval implementation hashes.

## Known first-stage failure

The inspected Russian Kafka description is a separate regression probe, not a
held-out case. The correct opening's best window ranked **7,826 globally** and
**2,420 among German windows**. It was absent from the 2,000-window/100-passage
candidate pool. Re-encoding the exact input reproduced the stored vector with
maximum absolute difference about 1.23e-7; input variants alone are not a new
global index. Reranking the existing hundred candidates cannot recover an
excluded passage. [Full diagnostic](../experiments/kafka-candidates-v1.json)

Next quality work compares a pinned E5-base candidate on an owned copy of the
20-edition development control. No model is promoted from a card or one query.
A separate [40-case sealed final commitment](FINAL_EVALUATION.md) is kept closed
until an explicit candidate freeze.

## Performance work

The deployed CTE plan selects eligible IDs/scores before quotation hydration.
A selected book supplies safe rowid bounds inside the same SQL snapshot;
the exact book filter remains mandatory. At 3,991 editions, four bounded pairs
retained exact ID order, BM25 bits, metadata and citations. Observed scoped
Meditations latency was 12,347→88 ms and Darwin 3,125→69 ms. Global `man` remained
slow at 14,997 ms. These were single ordered pairs with cache caveats.
[Plan, ownership checks and receipts](LEXICAL_PLAN_EXPERIMENT.md)

The native-rank prototype observed 17,368→625 ms for global `man` on the
same 4,001-edition corpus within its own pair. It exhausts cutoff ties with a
bounded Python candidate heap and preserves one read snapshot. This prototype
is not yet the deployed search plan; SQLite's internal ranking memory was not
bounded or measured by the Python heap size. [Prototype and limits](NATIVE_RANK_EXPERIMENT.md)
