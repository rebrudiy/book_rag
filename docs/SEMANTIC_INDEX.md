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
3. Commits exact-input keys and their incremental digest in the staging SQLite map.
4. Flushes and fsyncs both arrays.
5. Atomically replaces `checkpoint.json` with the committed count, last passage
   and window cursor, model/source descriptor and vector/ID/key digests.

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

Only a complete index with a still-matching corpus fingerprint and a finalized
key-map receipt is renamed into an immutable `gen-<identity>-<suffix>` directory. Metadata is persisted before
`active.json` is atomically replaced. Existing active arrays are never edited by
a new build. If interruption occurs after the completed rename but before the
pointer replacement, the next run validates and activates that ready generation
without another encoder call. A matching completed generation is also reused
on subsequent invocations.

Metadata includes source fingerprint, resolved model revision, dimensions,
passage/window counts, exact strategy parameters, generation and resumed-vector
count, separately encoded and exactly reused vectors, reuse source and disk
budget. A change to the corpus during planning or encoding prevents activation;
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
interrupted activation and shared model-cache use in builds and queries. Reuse
tests additionally cover append, reordered passage IDs, changed source/header,
model/revision/profile separation, corruption, durable keys ahead of checkpoint,
interrupted sealing, 512-input batches and disk exhaustion before/mid build.

Resume planning still tokenizes the source once to verify window count; it does
not repeat completed model inference. A completed-index reuse also validates
its array prefixes. The index's main storage size remains proportional to the
number of windows. Retained stale staging directories and old generations are
not automatically deleted: remove only inactive generations/checkpoints when
no builder is running, preserving the directory named in `active.json` and any
interrupted work you intend to resume.

## Reusing exact encoder inputs

New generations carry `vector_reuse_schema=2` and an on-disk
`window-keys.sqlite3` map. A SHA256 key binds the exact text passed to the
encoder, including its decoded metadata header, newline, document prefix and
Unicode/whitespace, to a frozen model/revision/dimension/token-limit/profile/
normalization identity. The map contains a key and the first matching vector
row only; source windows and a second copy of vectors are not stored in it.

On a changed corpus the builder considers the previous active generation as a
reuse source. It requires a complete receipt, compatible explicit model space,
valid metadata/checkpoint/map checksums, correct array length/shape/dtype,
finite normalized vectors and in-range rows. Source validation streams bounded
blocks once per build. At most 500 keys are looked up at a time; returned vectors
are copied into the new generation's own array and current passage IDs. Only
missing inputs reach the encoder. A corrupt/unavailable source is rejected with
an explicit warning, then fresh encoding continues. Changes to the source files
after opening also reject subsequent lookups.

Existing active indexes without a key map or explicit profile still support
queries. They are never silently seeded or assigned a guessed profile. A new
build writes its own keys; subsequent compatible builds can reuse them. The
build identity includes the key-map schema, separating new checkpoints from
older builder formats. A rejected ready generation with a missing receipt is
left untouched and rebuilt. A finalized staging receipt interrupted before
rename is promoted and validated; a sealed map interrupted before receipt
creation finishes its receipt without repeating encoder calls.

Key transactions are durable before the vector checkpoint is replaced. On
resume a leading uncommitted key suffix is truncated to the committed vector
offset. A SHA256 digest over each unique key and its first row is maintained
incrementally, stored in SQLite and checkpointed with the vectors. Resume checks
it once, rejecting plausible-looking but altered key bytes as well as malformed
SQLite. Key prefixes are not rescanned on every batch. Duplicate detection is
bounded to the current batch plus indexed SQLite lookups; no corpus-size set of
raw window strings is held in RAM. Finalization adds checksums over the finished
map, metadata, checkpoint and vector payload before activation.

Returned `encoded_vectors` and `reused_vectors` count committed rows handled in
this invocation. `resumed_vectors` is the already committed prefix at startup;
the three sum to `vectors`. Recovering a complete generation reports zero newly
encoded/reused rows and the whole count as resumed. Persisted generation metadata
records the invocation that created it. These counters describe computation
reuse, not retrieval quality or number of distinct works.

Before creating arrays, on resume and before every batch, the builder requires
free disk of at least `4 GiB + remaining_vectors * (4 * dimensions + 8 + 256) +
8192 bytes`. The 256-byte per-vector SQLite allowance is conservative for this
schema; model downloads, other applications and retained generations can still
consume additional disk. A completed prefix reduces the remaining estimate.
Insufficient space raises a diagnostic error without replacing the active
pointer or discarding its valid checkpoint. The check cannot reserve disk
against another process consuming it between checks; write failures still leave
the previous atomic checkpoint available.

An isolated local MiniLM run on the eight licensed demo excerpts and one declared
CC0 synthetic test fixture verified actual inference reuse on `mps:0`:

| Build | Windows | Encoded | Reused | Seconds |
| --- | ---: | ---: | ---: | ---: |
| Initial eight excerpts / 45 passages | 183 | 183 | 0 | 0.846 |
| Append fixture / 47 passages | 190 | 7 | 183 | 0.093 |
| Fresh nine-source control | 190 | 190 | 0 | 0.678 |

The appended build called the encoder once with seven inputs. Its vectors and
passage IDs were exactly equal to the fresh control (maximum absolute vector
difference 0); three quote/idea/Russian hybrid queries preserved their top-five
passage IDs and all returned citations verified. The 183-row original vector
array occupied 281,216 bytes; its SQLite map occupied 32,768 bytes. Local ignored
receipts and the reproducible runner are in `data/reuse-experiment/`. Timings
exclude the shared model's initial load and come from one small, warm local run;
this does not measure corpus-wide retrieval quality or establish 10,000-book
capacity. No existing frozen/full library or previous index was seeded or edited.

See [the reuse helper contract](VECTOR_REUSE.md) for independent validation and
storage details.

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
# Frozen-source resume and resource observations

`scripts/embed_frozen.py` builds or resumes vector files while opening the corpus
SQLite file read-only, without applying the current schema. It requires a static
database and an empty or absent WAL, checks its identity during reads and verifies
the database SHA before and after a successful build. Use it for a frozen source;
ordinary live libraries use `bookrag embed`.

```sh
BOOKRAG_MODEL_CACHE=./data/models \
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.65 \
PYTORCH_MPS_LOW_WATERMARK_RATIO=0.5 \
uv run python scripts/embed_frozen.py data/snapshots/library1308-v1 \
  --batch-size 32 --resources data/embedding-resources.jsonl
```

The MPS ratios are local resource settings, not retrieval parameters. The high
watermark limits allocator memory relative to the device's recommended working
set; the lower watermark requests earlier reclamation/command submission.
[PyTorch documentation](https://docs.pytorch.org/docs/2.15/mps_environment_variables.html)
describes their scope. They do not cap total system RAM or disk use. The script
records process peak RSS and MPS allocator/driver observations every 30 seconds.
The builder independently retains its 4 GiB disk reserve and durable checkpoints.

The first 1,308-edition build stopped at 1,315,072 of 1,865,985 vectors when free
disk briefly fell below its reserve. The cause of the host-wide observation gap
was not established. It resumed with batch32 after free space recovered; completed
prefixes were retained. Do not label a checkpoint as an active complete index.
