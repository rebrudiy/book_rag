# Optional local multilingual reranking

## Purpose and selected model

A hybrid search can place a broadly related passage above the passage that actually discusses the remembered situation. An optional cross-encoder scores each query/passage pair jointly and reorders a bounded retrieval pool. It does not create an answer, change a quotation, retrieve a missing candidate, or establish that a passage supports a claim.

The default is [`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/blob/1427fd652930e4ba29e8149678df786c240d8825/README.md), pinned to revision `1427fd652930e4ba29e8149678df786c240d8825`. Its model card declares Apache-2.0 and includes English, Russian, French and German among the languages used in multilingual MS MARCO training. That training uses translated web-search data; its suitability for historical books and author comparisons must be measured separately.

The [model configuration](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/blob/1427fd652930e4ba29e8149678df786c240d8825/config.json) specifies a 12-layer, 384-hidden-dimension XLM-RoBERTa classifier. The [repository files](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/tree/1427fd652930e4ba29e8149678df786c240d8825) include approximately 471 MB safetensors weights plus tokenizer files. Local downloads and caches go under ignored `data/models/`; no paid inference service is used.

[Sentence Transformers documentation](https://www.sbert.net/docs/cross_encoder/usage/usage.html) explains that cross-encoders score pairs individually and are typically used after retrieval. The model's outputs are raw ranking logits. They are not calibrated answer probabilities, and applying a sigmoid would not change ordering or prove answerability.

## Runtime contract

Install the existing optional dependencies:

```sh
uv sync --extra semantic
```

```python
from pathlib import Path
from book_rag.reranking import rerank

ranked = rerank(query, hybrid_candidates, Path("data/models"), limit=10)
```

The caller supplies verified source candidates; reranking preserves their text, offsets, attribution, rights and URLs. It changes only ordering and score fields, retaining the previous score as `retrieval_score` and adding `reranker_score`. Output includes model/revision, elapsed time, warnings, scoring semantics and explicit availability. No candidate results avoids loading a model at all.

The implementation caches at most one model, serializes model loading/inference, scores at most the first 50 candidates, and uses batches of eight. `BOOKRAG_RERANKER_DEVICE` optionally selects a supported local device; the default delegates device selection to Sentence Transformers. `BOOKRAG_RERANKER_MODEL` can select another model, with `BOOKRAG_RERANKER_REVISION` for a reproducible custom revision. Only the default model's license and configuration have been reviewed here. Remote code is disabled and weights are loaded through safetensors.

Input uses the model's sequence limit, capped at 512 tokens per query/passage pair. Queries are bounded to 96 tokens and passages to the remaining pair budget. Every truncation is reported. This first experiment uses the beginning of a long passage; evidence near its end can therefore be missed. The returned source quotation remains complete and unchanged. A future independent experiment can compare window pooling or query-focused source windows before changing the default.

Missing optional dependencies and inaccessible weights/inference failures return `available=False`, no ranked results and an explicit warning. A caller may show its existing retrieval results as a clearly labeled fallback; it must not silently describe fallback results as reranked output. Nonfinite or malformed model scores cannot become ranked evidence. Equal logits retain the original retrieval order.

## Evaluation protocol

The development experiment retrieves up to 50 hybrid candidates per query and reranks them down to the evaluator's k=5. Candidate generation uses the current local demo index. The development fixture remains unchanged. The frozen v0.2 held-out set is not rerun or used for tuning.

A 50-candidate reranking run should be compared with a fresh hybrid run that also requests 50 candidates and then takes the first five. It is not a controlled comparison to a historical hybrid report that requested only five, because candidate depth can affect fusion ordering and eligibility. Both runs must share corpus/dataset digests and k. Reports include actual warnings, truncation, model revision, process peak memory and wall time. Cold first-request model load and warmed requests need to be distinguished.

CI tests inject a fake scorer/tokenizer and require no model download. They verify stable ordering, preserved exact source records, bounded candidate and token counts, finite-score validation and explicit dependency/inference failure reporting. They do not establish real model quality or performance.

## First development result

The pinned model downloaded and ran successfully on the local Apple `mps:0` device. The initial two-passage smoke test took 62.3 seconds including the one-time model download and initialization; that duration is not an inference benchmark. Both development measurements below ran in separate fresh processes using cached weights, with the first query including model initialization and later queries reusing the loaded model.

Reports are `experiments/reranker-dev-v1-control.json`, `experiments/reranker-dev-v1.json`, and their compatible comparison `experiments/reranker-dev-v1-comparison.json`. The corpus and fixture digests match. Both requested the same 50-candidate hybrid depth and evaluated the first five results. Actual upstream eligibility produced 207 query/passage pairs for reranking across the 23 queries, rather than 50 pairs for every query.

| Development metric, k=5 | Hybrid candidate-50 control | Cross-encoder after same pool |
| --- | ---: | ---: |
| MRR | 0.9213 | 0.9352 |
| Hit rate | 1.0000 | 1.0000 |
| Cross-language MRR, four cases | 0.6458 | 0.8333 |
| Negative abstention, five cases | 1.0000 | 1.0000 |
| Exact quotation / metadata / reader anchor | 1.0000 | 1.0000 |
| Precision@5 | 0.3556 | 0.3444 |
| End-to-end retrieval p50, ms | 31.925 | 108.688 |
| End-to-end retrieval p95, ms | 47.530 | 596.209 |
| Process peak RSS, MiB | 833.34 | 1326.98 |

The aggregate MRR gain is small (+0.0139), with improved cross-language ranks and one explanation-query regression from rank one to two. Hit rate and abstention do not improve. The reranker does not implement a relevance cutoff, so it cannot independently establish that a query has no answer. Six query runs reported one truncated candidate each; those warnings are preserved, rather than describing full-passage scoring where the model saw only a prefix.

This result supports keeping reranking optional. It does not justify replacing the default search: the fixture is tiny, latency and memory rise, one query type regresses, and no independent final-test evaluation was performed. Historical hybrid reports that requested a different candidate depth are contextual evidence only, not the control for this experiment. The frozen v0.2 held-out reports and labels were not accessed for this experiment or changed.

The next useful evaluation would collect a larger development set of paraphrases and explanation queries from independent books, compare prefix scoring with coverage-preserving windows, and measure explicitly warmed 10/25/50-candidate batches. A fresh final-test split would be required before claiming generalization improvements.
