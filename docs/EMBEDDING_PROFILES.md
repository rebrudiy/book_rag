# Embedding profiles and the E5 candidate

Embedding profiles define model inputs, independently of individual queries.
They are experimental retrieval configurations; registering a candidate does
not change the default model or establish better search quality.

| Profile | Model / pinned revision | Document input | Query input | Token limit |
| --- | --- | --- | --- | --- |
| `minilm-v2` (default) | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` / `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` | Existing header + exact source window | Original query | Model's existing 128-token limit |
| `multilingual-e5-small` (candidate) | `intfloat/multilingual-e5-small` / `614241f622f53c4eeff9890bdc4f31cfecc418b3` | `passage: ` + bounded header + exact source window | `query: ` + original query | 512 |

The E5 model's [pinned model card](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md)
and [primary revision metadata](https://huggingface.co/api/models/intfloat/multilingual-e5-small/revision/614241f622f53c4eeff9890bdc4f31cfecc418b3)
declare MIT licensing. Its pinned Sentence Transformers configuration specifies
512 tokens. The card requires the asymmetric prefixes above for retrieval,
including queries written in other languages. The
[Multilingual E5 technical report](https://arxiv.org/abs/2402.05672) describes its
contrastive pretraining and retrieval-oriented supervised fine-tuning. These
properties motivate a measured candidate, not an assumption that its benchmarks
transfer to book scenes, ideas or unsupported questions.

## Input and compatibility contract

The original MiniLM `_windows` function is unchanged. Its context header,
source substring boundaries, overlapping-window text and query inputs remain
identical. The profile wrapper delegates directly to that function for MiniLM.
Legacy active metadata without a profile means empty input prefixes; the model
name alone never retroactively changes how an existing index is queried.

For E5, the header still contains title, author and section and is bounded at
24 tokens. The passage prefix and decoded header are tokenized together before
calculating the remaining source budget. Six tokens are reserved for separators
and boundary effects; source windows overlap by 20 tokens. The full input is
bounded by both the profile's 512-token limit and the loaded model's capacity.
A check on the first 100 real source rows produced 102 windows whose largest
encoded input was 508 tokens, including special tokens. Source text is sliced
using offsets rather than decoded or rewritten. Returned quotations still come
from verified canonical passages, never from the prefixed model input.

A registered model chooses its profile automatically. To select a profile
explicitly, set `BOOKRAG_EMBEDDING_PROFILE`; a mismatch between profile and model
is rejected. Unregistered custom models retain unprefixed behavior. The active
metadata freezes profile name, model revision, query/document prefixes, token
limit and window strategy. Queries use these stored values even when current
environment variables or registry definitions change.

Profiles are also part of the deterministic checkpoint identity. Changing a
prefix or profile contract cannot resume vectors from another contract simply
because the embedding dimensions match. Interrupted staging from the original
profile remains available for that profile. See
[resumable index details](SEMANTIC_INDEX.md) for committed-prefix checksums,
activation and shared weights.

## Isolated comparison

Keep the source bodies and passage IDs fixed, and build the candidate in a
separate SQLite snapshot. For a fresh destination, for example:

```sh
BOOKRAG_DATA_DIR=./data/snapshots/curated20-v3 \
uv run bookrag snapshot ./data/snapshots/curated20-e5 --no-include-saved

BOOKRAG_DATA_DIR=./data/snapshots/curated20-e5 \
BOOKRAG_MODEL_CACHE="$PWD/data/models" \
BOOKRAG_EMBEDDING_MODEL=intfloat/multilingual-e5-small \
BOOKRAG_EMBEDDING_PROFILE=multilingual-e5-small \
uv run bookrag embed --batch-size 32

# The active snapshot now supplies its own model revision and query prefix.
BOOKRAG_DATA_DIR=./data/snapshots/curated20-e5 \
BOOKRAG_MODEL_CACHE="$PWD/data/models" \
uv run bookrag search 'девочка следует за белым кроликом' --mode hybrid
```

The candidate does not activate in `data/` or the MiniLM comparison snapshot.
Weights are downloaded into the ignored shared cache, without a paid API.
Keep the model revision, profile metadata, corpus fingerprint and evaluation
split hash with comparison results.

## Scores, abstention and evaluation

The existing cosine floor (`BOOKRAG_SEMANTIC_THRESHOLD`, default `0.30`), phrase
constraints, candidate processing and reciprocal-rank fusion are unchanged by
this experiment. Cosine similarity values from different model spaces are not
calibrated to a common meaning and are not answer probabilities. The E5 card
specifically notes its high cosine distribution; a floor inherited from MiniLM
can therefore admit unsupported queries. An improvement in positive-query rank
alone does not establish adequate abstention or justify default promotion.

Compare the fixed candidate and baseline on the same development corpus and
queries, including language transfer, scoped negatives, exact citations,
indexing time and window/storage count. Any later threshold calibration must be
reported as a separate development experiment. The held-out set remains sealed
and is not consulted by profile implementation or candidate indexing.

```sh
uv run pytest -q tests/test_embedding_profiles.py tests/test_semantic.py
```

Tests cover prefix contracts, header/token budgeting, exact MiniLM behavior,
legacy metadata, frozen query prefixes after environment/registry changes,
shared-cache use and profile-specific checkpoint invalidation without downloads.

The initial local candidate build used the fixed 20-edition/12,108-passage
snapshot (fingerprint
`526909cfd6f8ff2ef0203f17c59f7e4e49d7a80b45303c18214ee6d0a1531f12`).
It produced 12,343 vectors of 384 dimensions in 285.896 seconds with batches of
32, compared with 49,738 windows in the existing MiniLM snapshot. The source
fingerprint and passage IDs are identical. This measures indexing behavior;
retrieval quality and abstention require the separate development comparison.
