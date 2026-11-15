# Optional offline generation runtime

`book_rag.local_generation` runs a pinned local instruct model through
`llama-cpp-python`. It supplies bounded text generation and optional JSON-schema grammar.
It contains no retrieval, answer-writing policy, citation validation, or QA prompt.
Application orchestration must supply source context and validate generated claims and
references before presenting them as supported answers.

The default public model is the official
[Qwen2.5-1.5B-Instruct-GGUF repository](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF).
Its [pinned license](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/blob/91cad51170dc346986eccefdc2dd33a9da36ead9/LICENSE)
is Apache-2.0. The [pinned Q4_K_M artifact](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/blob/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf)
and its downloaded bytes were checked independently against the official LFS SHA-256.

| Artifact | Pin |
| --- | --- |
| Repository | `Qwen/Qwen2.5-1.5B-Instruct-GGUF` |
| Revision | `91cad51170dc346986eccefdc2dd33a9da36ead9` |
| Filename | `qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| Model bytes | `1117320736` (about 1.041 GiB) |
| Model SHA-256 | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |
| License SHA-256 | `832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e` |
| Runtime | `llama-cpp-python==0.3.19` |
| Tested Metal wheel SHA-256 | `a8a744bc6369938f8b31344db74fd62abc76a1da5251c211dd1593c424e5befe` |

The runtime's [license is MIT](https://github.com/abetlen/llama-cpp-python/blob/v0.3.19/LICENSE.md).
The tested binary is the package author's
[0.3.19 Metal release](https://github.com/abetlen/llama-cpp-python/releases/tag/v0.3.19-metal),
for CPython 3.12 on macOS arm64. Its 11,840,108 bytes were independently hashed and 99
installed archive payload files matched it; the installer-generated RECORD was excluded.
No paid inference service or external API key is required.

## Install and acquire explicitly

`generation` is an optional extra. Normal search/test installation does not import or
require the native runtime. The cross-platform lock uses the ordinary pinned PyPI source
distribution, so a Linux installation never resolves a macOS wheel:

```sh
uv sync --extra dev --extra semantic --extra generation
```

Building the PyPI distribution requires the platform compiler/build tools. The upstream
[installation documentation](https://llama-cpp-python.readthedocs.io/en/latest/)
describes source builds and Metal wheels. For the exact tested Python 3.12/macOS arm64
binary, install this verified wheel after the project sync:

```sh
uv pip install --python .venv/bin/python --no-cache \
  'https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.19-metal/llama_cpp_python-0.3.19-cp312-cp312-macosx_11_0_arm64.whl#sha256=a8a744bc6369938f8b31344db74fd62abc76a1da5251c211dd1593c424e5befe'
.venv/bin/python scripts/acquire_local_model.py --reserve-gib 3
```

Run the macOS wheel command only on that matching platform. CPU installations can use the
same GGUF with `gpu_layers=0`; CPU performance was not measured here. A later project sync
may replace a direct-URL installation with its locked source build; repeat the verified
wheel command if that exact tested binary is required.

Model acquisition is the only network path. It uses a fixed official revision, exact
length and SHA-256, direct streaming into an owned `.part` file, checked HTTP Range resume,
and atomic verified publication. A complete partial can be finalized without downloading
it again. An OS acquisition lock prevents concurrent writers and releases after a crash;
the harmless lock file can remain. Existing corrupt final files are rejected without
overwriting them. A corrupt partial is retained for explicit inspection.

Every chunk preserves a 4 GiB disk floor plus the declared concurrent-work reserve
(default 3 GiB). Increase the reserve when other corpus jobs need more space. This guard
does not control future writes by unrelated processes. No duplicate Hugging Face model
cache is created. Weights, license, acquisition receipt, and the observed runtime receipt
remain outside Git under `data/models/local-generation/`.

## Runtime contract

```python
from book_rag.local_generation import GenerationLimits, LocalGenerator


def main() -> None:
    runtime = LocalGenerator(context_tokens=8192, n_threads=4, gpu_layers=-1)
    try:
        result = runtime.generate(
            [{"role": "user", "content": "Return one invented greeting as JSON."}],
            GenerationLimits(max_tokens=64, timeout_seconds=45),
            response_schema={
                "type": "object",
                "properties": {"greeting": {"type": "string"}},
                "required": ["greeting"],
                "additionalProperties": False,
            },
        )
        print(result["text"])
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
```

Standalone callers need an importable script/module and the usual `__main__` guard because
workers use multiprocessing `spawn`. A heredoc/REPL with no importable main file is not a
reliable spawn entry point. ASGI integration can reuse one application-scoped instance.
The module-level `generate(messages, limits=None, *, response_schema=None)` uses one lazy
singleton. It reads these variables at first construction:

- `BOOKRAG_GENERATION_MODEL`: acquired GGUF path; default `data/models/local-generation/`
  plus the pinned filename. A different model's bytes are rejected.
- `BOOKRAG_GENERATION_CONTEXT`: default `4096`, allowed `512`–`8192` tokens.
- `BOOKRAG_GENERATION_THREADS`: default `4`, allowed `1`–`8` CPU threads.
- `BOOKRAG_GENERATION_GPU_LAYERS`: `-1` for requested full offload or `0` for CPU.

Restart the singleton/application to apply new configuration. Keep one runtime per
application process, and close explicitly created instances when finished. Multiple
application workers each hold a model; their combined memory must be budgeted separately.

Messages accept `system`/`user`/`assistant` roles, a system message only first, and a final
user message. The adapter serializes the pinned model's ChatML format, preserves content
characters, and rejects reserved chat control tokens. It adds no default system or QA
instructions. Input is bounded to 32 messages and 32,768 content characters. The complete
prompt is tokenized exactly; prompt plus requested output must fit the context. Evidence
is never silently truncated.

`GenerationLimits` defaults to 384 output tokens, 45 seconds, 12,000 output characters,
temperature zero, and seed 7. Hard maxima are 1,024 output tokens and 120 seconds. One
thread lock serializes requests. A spawned native worker lazily verifies the whole pinned
model SHA, then loads the model. The same deadline covers queue wait, verification,
native import/loading, prompt IPC, grammar compilation and inference. On expiry, partial
output is discarded and the owned worker is terminated; bounded terminate/kill cleanup
can add up to about two seconds. A later request starts a clean worker. Successful calls
reuse one model while resetting request context. No prompt or private context is logged
or written to a file by the adapter.

`response_schema` uses the pinned library's Python JSON-schema-to-GBNF conversion and
then creates a `LlamaGrammar` in the worker. The
[pinned converter implementation](https://github.com/abetlen/llama-cpp-python/blob/v0.3.19/llama_cpp/llama_grammar.py)
expands bounded repetition into optional grammar groups. A short JSON schema can therefore
produce unsafe native-parser depth: an observed `maxLength: 2000` aborted a worker.

Preflight now rejects unsafe expansion **before worker/model creation**. Finite JSON is
limited to 16 KiB and depth 24. `minLength`, `maxLength`, `minItems`, and `maxItems` must be
integers from 0 to 64 with ordered minimum/maximum. Nested repetition, including local
references and bounded regex groups, has a 256-unit path budget and a 1,024-unit total
budget. For example, two records with two quotes of at most 64 characters fit the path
budget exactly. Long string maxima should be enforced by the caller's post-validation
and output-token/character limits, rather than expanded into a native grammar.

Patterns are limited to 512 characters and small bounded repetitions. Property/definition
maps and alternative/tuple lists have fanout at most 32. Only resolved, acyclic local
references with at most 16 reference hops are accepted; remote schema fetching is rejected.
The converted GBNF receives a second check before native consumption: at most 64 KiB,
256 rule lines and 64 nested groups, ignoring parentheses inside literals/classes.
These are conservative parser budgets, not complete JSON-schema validation or a proof
that every permitted grammar/native version is safe. Worker isolation and hard deadlines
still apply. Callers must parse and validate output independently; token/character limits
can leave incomplete JSON, and syntactic constraints do not establish claim correctness.

The result contains `text`, `finish_reason`, `usage`, `model`, `backend`, `elapsed_ms` and
`warnings`. `character_limit` is explicit. Exceptions `GenerationTimeout`,
`ContextLimitExceeded` and `ModelUnavailable` derive from `GenerationError`. Missing native
packages/models fail lazily; inference never invokes acquisition. `capabilities()` is a
cheap presence hint, not a checksum or quality claim. Backend metadata distinguishes
offload support and requested layers; no GPU-memory profiler measurement is implied.

## Verification and limits

```sh
.venv/bin/python -m pytest -q tests/test_local_generation.py
.venv/bin/python scripts/smoke_local_generation.py --json-schema \
  --output data/bench/local-generation-schema-reproduce.json
```

Seventy-five synthetic unit tests passed, including hard cold-start/large-prompt deadlines,
termination/recovery, concurrent response isolation, exact context bounds, checksum drift,
optional-runtime absence without network, disk reserve, resumed/interrupted downloads,
complete partials, acquisition locking, receipt cleanup, pre-worker rejection of huge
length/item/pattern expansion, nested/reference budgets, and pre-native GBNF checks.
Ruff and ty passed. Tests
do not require the optional native package or real model.

On the current 16 GiB arm64 Mac, the real offline synthetic Russian context produced the
expected integer inside the requested JSON schema: 71 prompt tokens, six output tokens,
about 1.17 seconds including cold load/hash. A neutral nine-token greeting took about
162 ms in the same worker. The worker was then closed. Plain-text smoke also passed.
Reports are in ignored `data/bench/local-generation-*.json`; provenance is in
`data/models/local-generation/runtime-receipt.json`. These are individual runtime checks,
not RAG accuracy, citation correctness, complete memory profiling, or multilingual quality
evaluation. No real book query, retrieval evaluation, or sealed final case was used.

## Reviewed model profiles

`BOOKRAG_GENERATION_PROFILE` selects a fixed artifact at application import time;
restart the service when changing it. The default is `qwen2.5-1.5b`. An unknown
profile leaves ordinary search available and fails generation lazily. Parent
file length/SHA pins are passed explicitly into spawned workers, so a later
environment change cannot silently choose another worker model.

The optional `qwen3-4b-instruct-2507` candidate uses
[Unsloth's GGUF conversion](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/tree/a06e946bb6b655725eafa393f4a9745d460374c9)
of [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507).
This is an Unsloth conversion, not an official Qwen GGUF release. The original
model card describes this variant as a non-thinking instruct model. Its
[reviewed base license snapshot](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/cdbee75f17c01a7cc42f958dc650907174af0554/LICENSE)
is Apache-2.0; that reviewed revision identifies the license/base snapshot,
not a proven exact source-weight commit used by the converter. Conversion base
revision remains explicitly unknown.

| Candidate artifact | Pin |
| --- | --- |
| Converter repository/revision | `unsloth/Qwen3-4B-Instruct-2507-GGUF` / `a06e946bb6b655725eafa393f4a9745d460374c9` |
| Filename | `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` |
| Bytes | `2497281120` |
| SHA-256 | `3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597` |

The exact file metadata was checked against the converter's primary Hugging Face
API/LFS response; downloaded bytes independently matched the pin. Profiles use
separate default directories and per-model acquisition receipts. A custom shared
directory keeps another model's common `receipt.json` and writes the new model's
own `.gguf.receipt.json`. Existing weights and licenses are not replaced.

```sh
BOOKRAG_GENERATION_PROFILE=qwen3-4b-instruct-2507 \
  .venv/bin/python scripts/acquire_local_model.py --reserve-gib 2
BOOKRAG_GENERATION_PROFILE=qwen3-4b-instruct-2507 \
  .venv/bin/python scripts/smoke_local_generation.py --json-schema \
  --output data/bench/qwen3-runtime-reproduce.json
```

The candidate's default directory is
`data/models/local-generation/qwen3-4b-instruct-2507/`. An explicitly configured
`BOOKRAG_GENERATION_MODEL` must contain the selected profile's pinned bytes.
The same finite generation/context limits apply. A successful runtime smoke is
not a reason to promote a model: compare actual open development questions,
source integrity, factual support and resource costs separately.
