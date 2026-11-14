# Resumable local semantic index

`bookrag embed` builds overlapping tokenizer windows with the existing
`token-windows-v2` strategy. The text sent to the encoder remains unchanged:
a title/author/section header decoded from at most 24 tokens, a newline, and the
exact source substring selected by tokenizer offsets. The text budget remains
`max(32, model.max_seq_length - header_tokens - 6)`, with 20 overlapping tokens.
Quoted evidence still comes from canonical source passages; windows do not
replace the stored quotations. Cosine thresholds, phrase constraints, retrieval
ordering and reciprocal-rank fusion are unchanged.

## Model identity and shared weights

The default model is
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, pinned to commit
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`. This matches the existing local
Hugging Face `refs/main` and was verified against the
[primary model revision API](https://huggingface.co/api/models/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/revision/e8f8c211226b894fcb81acc59f3b34ba3efd5f42).
The loader uses the documented
[SentenceTransformer revision and cache options](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
and disables remote model code. The in-process model cache holds at most two
model/cache/revision combinations.

By default weights live under the selected library's `models/` directory.
`BOOKRAG_MODEL_CACHE` selects a shared directory for embedding builds and queries:

```sh
BOOKRAG_MODEL_CACHE="$PWD/data/models" \
BOOKRAG_DATA_DIR=./data/snapshot \
uv run bookrag embed

BOOKRAG_MODEL_CACHE="$PWD/data/models" \
BOOKRAG_DATA_DIR=./data/snapshot \
uv run bookrag serve --port 8002
```

For a custom remote model, `BOOKRAG_EMBEDDING_MODEL` selects its name and
`BOOKRAG_EMBEDDING_REVISION` can pin its commit. When a symbolic revision is used,
the builder resolves the loaded model's commit from its configuration or local
Hub refs. It refuses to checkpoint an unresolved revision. Metadata records the
requested and resolved revisions separately. A query loads the revision recorded
in the active index, so later environment changes cannot silently substitute
another embedding space.

## Checkpoints and activation

A deterministic staging identity includes the corpus passage fingerprint, model
name, resolved model revision, embedding dimensions, model token limit and the
window strategy parameters. Its directory is `vectors/staging-<identity>/`.
A kernel file lock prevents concurrent builders from writing the same library;
the operating system releases this lock if a process is killed.

The builder streams SQL rows and counts windows without retaining the whole
corpus in memory. It validates the planned count before resuming. Staging uses
memory-mapped `float32` vectors and `int64` passage IDs. Each completed batch:

1. Writes vectors and IDs at the next uncommitted offset.
2. Updates two incremental SHA256 digests over the committed vector/ID prefixes.
3. Flushes and fsyncs both arrays.
4. Atomically replaces `checkpoint.json` with the committed count, last passage
   and window cursor, model/source descriptor and prefix digests.

The checkpoint is constant size. Prefixes are never rehashed after every batch.
On resume each completed prefix is checked once in bounded blocks; reconstructed
hash objects then continue incrementally. Changing the requested batch size is
safe because digest boundaries do not depend on batches. Already completed rows
are skipped by passage ID, and completed windows in the cursor row are skipped
without another encoder call. Work written beyond the last atomic checkpoint is
uncommitted and is overwritten.

Malformed JSON, incompatible identity/count/dimensions/cursor, truncated arrays,
wrong array dtypes or completed-prefix checksum failures cause only that staging
copy to be rebuilt. A different corpus or model produces a different staging
identity; unrelated interrupted work is retained. Interrupted batches remain on
disk and the same command resumes them after restart.

Only a complete index with a still-matching corpus fingerprint is renamed into
an immutable `gen-<identity>-<suffix>` directory. Metadata is persisted before
`active.json` is atomically replaced. Existing active arrays are never edited by
a new build. If interruption occurs after the completed rename but before the
pointer replacement, the next run validates and activates that ready generation
without another encoder call. A matching completed generation is also reused
on subsequent invocations.

Metadata includes source fingerprint, resolved model revision, dimensions,
passage/window counts, exact strategy parameters, generation and resumed-vector
count. A change to the corpus during planning or encoding prevents activation;
retry after ingestion finishes. Query-time fingerprint checks still reject
stale active indexes and expose the existing lexical fallback.

## Verification and limits

```sh
uv run pytest -q tests/test_semantic.py
```

Tests use deterministic fake encoders/tokenizers, avoiding model downloads in CI.
They cover exact token-window coverage, durable interruption/resume with a
changed batch size, corruption/truncation rejection, stale source separation,
active-index preservation after a failed alternative-model build, recovery of
interrupted activation and shared model-cache use in builds and queries.

Resume planning still tokenizes the source once to verify window count; it does
not repeat completed model inference. A completed-index reuse also validates
its array prefixes. The index's main storage size remains proportional to the
number of windows. Retained stale staging directories and old generations are
not automatically deleted: remove only inactive generations/checkpoints when
no builder is running, preserving the directory named in `active.json` and any
interrupted work you intend to resume.

## A fixed query candidate pool

`fixed-passage-pool-v1` makes the retrieval pool independent of the number of
results displayed. The previous `max(50, limit * 4)` window pool could change
which semantic passages intersected the lexical ranking when callers requested
5, 20 or 50 results. That changed reciprocal-rank fusion even for the same query
and unchanged library.

The query policy now ranks at most 2,000 eligible windows and collects at most
100 distinct passage IDs above the existing cosine floor. Duplicate windows
contribute their best passage score once. Equal selected-window scores use their
immutable window positions as a tie breaker. The hybrid lexical pool remains
50 passages. Output truncation happens after verification, phrase constraints
and fusion, so requesting more results preserves earlier result prefixes.
These are query-time bounds; source windows, embedding profiles, checkpoints,
cosine scores, score floor and fusion formula are unchanged.

The 100-candidate evidence set is loaded through one `Library.passages` call.
It verifies canonical source slices without returning full chapter text or
reader context. Missing/nonindexed evidence is omitted and corrupt citations
are suppressed with a warning. Reader requests continue to load their full
context separately. A final corpus fingerprint check detects ingestion during
inference and returns an explicit current lexical fallback. This adds a second
O(P) fingerprint scan per semantic query; maintaining a cheap transactional
index revision is a future scaling improvement.

A pathological duplicate-heavy top 2,000 can yield fewer than 100 distinct
passages; the query does not scan indefinitely. Responses declare the strategy
and configured/actual pool counts. Tests cover empty/small arrays, overlapping
windows, exact book/language filters, quote constraints, missing/corrupt
candidates, concurrent source changes and stable 5/20/50 prefixes.

Paired development measurements use the fixed 20-edition source snapshots and
54-query `dev-expanded` split, with unchanged model profiles and thresholds:

| Model / mode | Control MRR@5 | Fixed-pool MRR@5 | Control hit@5 | Fixed-pool hit@5 | Control / fixed p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| MiniLM semantic | 0.51894 | 0.54167 | 0.72727 | 0.75000 | 46.82 / 47.34 |
| MiniLM hybrid | 0.68258 | 0.67121 | 0.79545 | 0.79545 | 115.49 / 115.12 |
| E5 semantic | 0.57159 | 0.57159 | 0.68182 | 0.68182 | 46.60 / 45.01 |
| E5 hybrid | 0.67045 | 0.67803 | 0.72727 | 0.75000 | 112.44 / 117.08 |

Citation, metadata and reader-anchor checks remained exact. Negative abstention
remained 0.20 for MiniLM and 0.00 for E5, and cross-language metrics did not
change. MiniLM hybrid MRR regressed slightly: this is a consistency correction,
not a claim that every retrieval metric improved. Timing is a single local run,
not a guarantee of throughput or performance at 50,000 works.

[Versioned paired reports](../experiments/candidate-pool-v1/summary.json) retain
both controls/candidates, compatible source/dataset digests, per-case results,
comparisons and real prefix checks for `struggle for existence` and a Russian
rabbit scene at limits 5/20/50 in both modes and model spaces. The accompanying
patch reconstructs the control implementation with its captured SHA256. Existing
experiment files and held-out data were not changed.
