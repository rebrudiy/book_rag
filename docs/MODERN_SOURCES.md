# Modern technical sources: pinned D2L selection

Book RAG can acquire a small modern machine-learning textbook selection from
**Dive into Deep Learning**. It adds contemporary technical explanations to the
historical public-domain corpus without relying on a provider-wide assumption
that every accessible text is reusable.

## Source and exact rights

The source is the authors' [official repository](https://github.com/d2l-ai/d2l-en),
English release `v1.0.3`, pinned to commit
`b2e2ae30898a9d0126a9699ae7e441de3e272715`. Authors are Aston Zhang, Zachary C.
Lipton, Mu Li and Alexander J. Smola. The download URLs embed that full commit;
they do not follow the changing main branch.

Three original notices are downloaded and checked against verified SHA-256
values **before any chapter download**:

| Pinned source notice | Meaning | SHA-256 |
| --- | --- | --- |
| [LICENSE](https://raw.githubusercontent.com/d2l-ai/d2l-en/b2e2ae30898a9d0126a9699ae7e441de3e272715/LICENSE) | Book text: CC BY-SA 4.0 | `a39a4b3d0f411a8217b7c6dd35dc8b06003784ce32d3593d0d2d780a7e9c2f8c` |
| [LICENSE-SAMPLECODE](https://raw.githubusercontent.com/d2l-ai/d2l-en/b2e2ae30898a9d0126a9699ae7e441de3e272715/LICENSE-SAMPLECODE) | Sample/reference code: original modified MIT notice | `ddf9b5d9dd2fded18653e0ca4c5934008d4faea1161d754493b54a23c25eac14` |
| [LICENSE-SUMMARY](https://raw.githubusercontent.com/d2l-ai/d2l-en/b2e2ae30898a9d0126a9699ae7e441de3e272715/LICENSE-SUMMARY) | Explicit separation of documentation and code terms | `eeff5a6b129a319ee80e34762695dfa5349fe385a92c02168e6d21deff539f9c` |

The original notices are retained alongside the book. Attribution includes the
title, authors, immutable source revision, links, original copyright notice and
conversion notice. Adapted book text remains under CC BY-SA 4.0; the source's
code license remains separate. The project's MIT software license does not
replace either source license. See the [CC BY-SA 4.0 terms](https://creativecommons.org/licenses/by-sa/4.0/).

## Coverage

This is **one work with a partial selection of fifteen source sections**. It is
not the complete book and does not count as fifteen books. The selected source
files cover introduction; linear algebra, calculus, probability and automatic
differentiation; linear regression; multilayer perceptrons and backpropagation;
convolutional networks; queries/keys/values, multihead attention and transformers;
gradient descent and Adam. Other chapters, reference images, datasets, models
and the full PDF are not part of this acquisition.

The import manifest uses `work_id: d2l-en`, a pinned English edition and
`coverage: selected_chapters`. This is an explicitly curated identity; it does
not infer relationships to other language editions. The packaging attribution
section is explicitly attributed to Book RAG as editorial matter.

## Acquisition and import

From a source checkout after installing the project:

```sh
uv run python -c 'from pathlib import Path; from book_rag.modern_sources import acquire_d2l; print(acquire_d2l(Path("data/modern/d2l"))["manifest"])'
BOOKRAG_DATA_DIR=data/full uv run bookrag ingest data/modern/d2l/manifest.json
```

Run acquisition and import separately when another corpus import is active.
The acquisition helper does not open or modify a library database.

`acquire_d2l(destination: Path)` returns a report with `manifest` and `book` paths,
`chapters`, total raw `bytes`, source revision, bundle/artifact hashes and cache
hit/download counts. Its artifacts are written below the supplied destination:

- `raw/`: original source bytes and URL/hash/time cache sidecars;
- `licenses/`: complete unmodified license and copyright notices;
- `d2l-selected-<hash>.fb2`: generated, safely escaped structured book text;
- `provenance.json`: per-file title, authors, edition, immutable URL, SHA-256,
  byte size, UTC retrieval time and source line ranges for blocks;
- `manifest.json`: one entry for normal import, with the same per-section source
  map and explicit coverage/rights metadata.

All generated book text, originals and cache data stay under ignored `data/`.
Choose another ignored runtime directory if changing the destination. Do not add
the downloaded corpus to the public software repository.

Downloads use the existing verified cache and atomic source downloader. A rerun
checks source URL, file size and SHA-256 before reuse. Interrupted acquisition
retains completed raw downloads but does not publish a new manifest until the
selection is complete. Output book filenames include the content hash so an
older manifest continues to point to its original artifact during replacement.
Individual output files use atomic replacement. Long provider retry requests
are deferred rather than shortened.

## Text and technical fidelity

The converter follows source headings and paragraph boundaries while retaining
Markdown syntax as literal text. Code fences and display-math blocks become
explicit FB2 `<code xml:space="preserve">` blocks. The parser preserves tabs,
indentation, internal spaces, string literals and line breaks for these blocks;
only Unicode NFC and CRLF/CR-to-LF canonicalization apply. Ordinary prose keeps
the existing canonical whitespace rules.

The source Markdown remains byte-for-byte available with its own hash. Citation
offsets refer to the extracted canonical FB2 sections. They are not raw Markdown
byte offsets or inferred print pages. Per-file provenance and source block line
ranges allow independent comparison against the immutable source.

Formulas remain LaTeX text; Markdown references and image links remain literal.
Images are not rendered, relative links are not resolved, and code is not
executed. This preserves technical source material for search and inspection
without claiming the interactive rendered experience of the original book.

## Verified acquisition

On 2026-11-14, acquisition downloaded **18 files**: fifteen source sections and
three license notices, **433,684 bytes** in **7.004 seconds**. The derived FB2
parsed into **148 sections** and **413,906 canonical characters**. All **483**
code/math blocks were exact substrings of the canonical extracted text after
documented Unicode/newline normalization. A second run reused all 18 verified
cache entries, made zero network downloads and completed in **0.034 seconds**.
These figures describe acquisition and parsing, not search-quality evaluation.

Source-bundle SHA-256:
`d67e8a80028dfa0a1abf82e7b1e6baf1bdfaafb59e9e4ac6f426da541c4047c2`.
Derived-artifact SHA-256:
`6be29ff72610ccb489bc46ff4f90e175ed1aa65b8968aa1b70b36d5d9e88c7c4`.
The generated acquisition and verification reports are kept with the runtime
artifacts. Unit tests use mocked downloads and cover rights mismatch, safe XML,
exact code/math, deterministic resume and interrupted acquisition.
