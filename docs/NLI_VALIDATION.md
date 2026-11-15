# Local NLI development experiment

This prototype measures whether a separate NLI classifier can help inspect RAG
claims. It is **not integrated into answers, the API, generation, retrieval or
work counts**. Scores are uncalibrated model outputs, not probabilities that an
answer is true. Source quotation integrity and contributor attribution remain
separate deterministic checks. NLI does not establish that a claim answers the
question or covers its requested comparison.

## Pinned model and acquisition

The author’s [model card](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli)
identifies the model as multilingual NLI and declares MIT licensing. Its NLI
example orders labels as entailment, neutral, contradiction. The pinned
[configuration](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli/blob/8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c/config.json)
independently binds indices0/1/2 to those labels and has512 maximum positions.
The original model card is retained locally with the tokenizer/configuration.
The author also documents FP16 inference issues; this experiment loads the
stored F16 weights into CPU **float32** parameters.

Repository: `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`.
Revision: `8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c`.
Weight SHA256: `65af59b1ff4450b09ecbf13ca35c840dbf038b26ff8e10e5ea89ca724828ed1e`.

```sh
# Existing optional semantic dependencies provide torch/transformers/safetensors.
.bootstrap/bin/uv sync --extra semantic --extra dev
.venv/bin/python scripts/acquire_nli_model.py
.venv/bin/python scripts/probe_nli_validation.py
```

Acquisition is explicit, serial and pinned. Seven files total578,297,589bytes,
including the557,652,046-byte safetensors file. LFS files are checked by SHA256;
small Git files additionally bind their original blob identities, with acquired
SHA256 recorded. No pickle weights, remote model code, remote inference,
Hugging Face hub cache or second model copy is used. Files stay in
`data/models/mdeberta-nli/<revision>/`, outside Git. Partial files resume with
strict byte-range checks and become final files by atomic rename after verifying
the exact pin. Unexpected bytes, ranges, hashes and responses fail closed.
The original per-file HTTP receipt survives cached reruns; local packaging time
is recorded separately as acquisition summary time.

The downloader preserves at least4.5GiB disk after remaining bytes, checks the
floor during transfer, uses an owned acquisition lock and stops on provider
errors. Full `Retry-After` deferrals persist in the model directory and shared
`data/models/.nli-huggingface-deferred.json`; changing destination does not bypass
the shared wait. No automatic retries or duplicate global cache are created.
The initial acquisition completed in57.574s. A real cache repeat verified all
pins in0.428s with zero new files and no download branch taken.

## Inputs, inference and bounds

The only captured evaluation input is the **open development** run04 response
snapshot with SHA256
`4709e7d4d7753c6e4309a6a0ee32222563a8ad71262f7d07a0ed22aed61329c6`.
It contains13 released claims across14 questions; abstentions do not invent
claims. Each selected quote is rechecked using its absolute canonical offsets
minus the captured passage start. All22 selected spans must match literally
before inference. This experiment never reads sealed evaluation cases or a
live source database.

Each claim is compared with its joined selected quotations and with the entire
captured source passages for those quotations. Joined evidence retains its
exact source bindings; no claim text is rewritten or expanded into an assumed
answer. Nineteen separately authored CC0 development contrasts cover actors,
numbers, bounds, negation, cross-language claims, observation versus instruction,
whole-text absence and a supported claim on the wrong question axis. Their
expected relations are explicit **author assumptions**, not independently
collected human gold labels. The existing run04 review is identified as an AI
review, not human ratings.

Inference is lazy, project-local and offline, with `local_files_only=True`,
`trust_remote_code=False` and `use_safetensors=True`. A separate supervised worker
uses CPU float32, two intra-operation threads and one inter-operation thread.
The actual device, parameter dtypes and thread counts are verified and recorded.
No MPS/GPU or generative model is invoked. Pair token counts include special
separator tokens. Inputs over512 tokens receive `context_limit`, null scores and
`truncated=false`: **there is no silent token truncation**. A deliberately long
pair tests that boundary.

Worker peak RSS is checked against2.5GiB, with a supervisor monitoring resident
memory and a650s hard deadline (worker scoring deadline600s). Failure/interrupt
terminates and reaps the child and records failed status rather than leaving a
stale running receipt. Resource caps are process bounds, not a promise of a
particular system-wide footprint. Raw scores and input/provenance hashes remain
in `data/evaluations/nli-v1/`. Runtime model/source text is not committed.

## Actual result and decision

The second measured run independently confirmed all parameter devices=`cpu`,
all parameter dtypes=`torch.float32`, two threads, one inter-operation thread and
a fast tokenizer. It completed46 pairs in5.299s, including2.539s model loading
and2.413s summed forward/softmax time, with peak RSS2,287,583,232bytes (2.13GiB).
The first run took5.429s; all38 scored pair distributions and all46 statuses were
identical on repeat (maximum score difference0).

Thirty-eight pairs were scored and eight exceeded512 tokens without truncation:
one joined-quote pair, six captured-context pairs and the explicit long stress
case. The19 authored contrasts agreed with their author assumptions13times and
disagreed six times. This is exploratory agreement, **not accuracy**.

| Observed case | Entailment score | Why this matters |
| --- | ---: | --- |
| Captured Lefty instruction shift | 0.9989 | The independent AI source review identifies an instruction/observation error; NLI would admit it at common thresholds. |
| Authored Russian recipient shift | 0.9987 | Changing the requested recipient from Russia to the English is also incorrectly treated as entailed. |
| Authored48,000→4,800 pins | 0.9926 | A tenfold number error passes despite a nearly certain entailment output. |
| Captured Alice answer “A watch” | 0.1017 | A fragment judged directly supported by the AI source review receives neutral as its top label. |
| Captured Chameleon answer | 0.0081 | A response judged supported by the AI source review receives neutral; selected-quote attribution/context may matter. |
| Authored supported answer on wrong comparison axis | 0.9829 | Even successful entailment does not check what the user asked. |

The sampled actor swap and some numeric/negation contrasts were detected, and
several captured unsupported claims received neutral. Those successes do not
remove the observed high-confidence failures. Adding full captured context did
not repair the shared, scoreable cases: that variant rejected six of13 pairs,
compared with one of13 using selected quotes. The false whole-book absence claim could not be scored with its
full quoted evidence because it exceeded the limit; individual excerpt scores
could not prove a claim about the entire original work in any case.

**Decision: retain this as an offline diagnostic, not a publication gate.** A
single NLI threshold cannot safely resolve actor/number fidelity, valid short
answers, metadata attribution, missing evidence and question-axis coverage.
Further work needs evidence-bounded atomic claims, explicit numeric/actor and
question-scope checks plus independently reviewed labels. Production answers
remain unchanged. The public metadata summary is
`experiments/nli-validation-v1.json`; the CC0 contrasts are
`evals/nli-contrastives-v1.json`.

Eight isolated tests cover pinned/resumable downloads, cached receipt stability,
shared deferrals/Retry-After, disk reserve, Git blob identities, symlink refusal,
exact label mapping,512-token rejection, lazy empty inputs, memory guard,
absolute canonical support offsets and supervisor child reaping. They require no
real model/network/torch. Ruff and ty pass; actual model measurements above are
separate from those mocked boundary tests.
