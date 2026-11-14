"""Acquire a small, pinned, licensed modern textbook selection reproducibly.

The source Markdown remains auditable in full. FB2 conversion treats Markdown,
code and LaTeX as literal text; it neither renders formulas nor executes code.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from book_rag.sources import cached_source, download_source

D2L_REVISION = "b2e2ae30898a9d0126a9699ae7e441de3e272715"
D2L_VERSION = "v1.0.3"
D2L_BASE = f"https://raw.githubusercontent.com/d2l-ai/d2l-en/{D2L_REVISION}/"
D2L_SOURCE = f"https://github.com/d2l-ai/d2l-en/tree/{D2L_REVISION}"
D2L_AUTHORS = ("Aston Zhang", "Zachary C. Lipton", "Mu Li", "Alexander J. Smola")
D2L_LICENSES = {
    "LICENSE": "a39a4b3d0f411a8217b7c6dd35dc8b06003784ce32d3593d0d2d780a7e9c2f8c",
    "LICENSE-SAMPLECODE": "ddf9b5d9dd2fded18653e0ca4c5934008d4faea1161d754493b54a23c25eac14",
    "LICENSE-SUMMARY": "eeff5a6b129a319ee80e34762695dfa5349fe385a92c02168e6d21deff539f9c",
}
D2L_CHAPTERS = (
    "chapter_introduction/index.md",
    "chapter_preliminaries/linear-algebra.md",
    "chapter_preliminaries/calculus.md",
    "chapter_preliminaries/probability.md",
    "chapter_preliminaries/autograd.md",
    "chapter_linear-regression/linear-regression.md",
    "chapter_multilayer-perceptrons/mlp.md",
    "chapter_multilayer-perceptrons/backprop.md",
    "chapter_convolutional-neural-networks/why-conv.md",
    "chapter_convolutional-neural-networks/conv-layer.md",
    "chapter_attention-mechanisms-and-transformers/queries-keys-values.md",
    "chapter_attention-mechanisms-and-transformers/multihead-attention.md",
    "chapter_attention-mechanisms-and-transformers/transformer.md",
    "chapter_optimization/gd.md",
    "chapter_optimization/adam.md",
)
FB_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PACKAGING_TITLE = "Source attribution and coverage (Book RAG packaging)"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _literal(value: str) -> str:
    # XML escaping is handled by ElementTree. Reject disallowed characters
    # instead of silently deleting source text and weakening quote fidelity.
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ValueError("Source contains control characters not representable in XML")
    return value


def _markdown_blocks(markdown: str) -> list[dict[str, Any]]:
    """Split source blocks without interpreting links, code or LaTeX content."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        start = index
        line = lines[index]
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            index += 1
            while index < len(lines):
                closing = re.match(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[index])
                index += 1
                if closing:
                    break
            else:
                raise ValueError(f"Unterminated Markdown code fence at source line {start + 1}")
            kind, level = "verbatim", 0
        elif line.strip().startswith("$$"):
            if line.strip().count("$$") >= 2:
                index += 1
            else:
                index += 1
                while index < len(lines) and "$$" not in lines[index]:
                    index += 1
                if index >= len(lines):
                    raise ValueError(f"Unterminated display math at source line {start + 1}")
                index += 1
            kind, level = "verbatim", 0
        elif heading := re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line):
            index += 1
            kind, level = "heading", len(heading.group(1))
        else:
            index += 1
            while index < len(lines) and lines[index].strip():
                if re.match(r"^\s*(`{3,}|~{3,}|\$\$)|^#{1,6}\s", lines[index]):
                    break
                index += 1
            kind, level = "paragraph", 0
        value = "\n".join(lines[start:index])
        if kind == "heading":
            assert heading is not None
            value = heading.group(2)
        blocks.append(
            {"kind": kind, "level": level, "text": value, "source_line_start": start + 1, "source_line_end": index}
        )
    return blocks


def _section(parent: Element, title: str, identity: str) -> Element:
    section = SubElement(parent, f"{{{FB_NS}}}section", {"id": identity})
    SubElement(SubElement(section, f"{{{FB_NS}}}title"), f"{{{FB_NS}}}p").text = _literal(title)
    return section


def _render_fb2(chapters: list[dict[str, Any]], attribution: str) -> bytes:
    root = Element(f"{{{FB_NS}}}FictionBook")
    description = SubElement(root, f"{{{FB_NS}}}description")
    title_info = SubElement(description, f"{{{FB_NS}}}title-info")
    SubElement(title_info, f"{{{FB_NS}}}genre").text = "sci_tech"
    for name in D2L_AUTHORS:
        first, last = name.rsplit(" ", 1)
        author = SubElement(title_info, f"{{{FB_NS}}}author")
        SubElement(author, f"{{{FB_NS}}}first-name").text = first
        SubElement(author, f"{{{FB_NS}}}last-name").text = last
    SubElement(title_info, f"{{{FB_NS}}}book-title").text = "Dive into Deep Learning — selected source chapters"
    annotation = SubElement(title_info, f"{{{FB_NS}}}annotation")
    SubElement(annotation, f"{{{FB_NS}}}p").text = attribution
    SubElement(title_info, f"{{{FB_NS}}}lang").text = "en"
    document_info = SubElement(description, f"{{{FB_NS}}}document-info")
    packager = SubElement(document_info, f"{{{FB_NS}}}author")
    SubElement(packager, f"{{{FB_NS}}}nickname").text = "Book RAG"
    SubElement(document_info, f"{{{FB_NS}}}program-used").text = "Book RAG pinned source converter v1"
    SubElement(document_info, f"{{{FB_NS}}}src-url").text = D2L_SOURCE
    SubElement(document_info, f"{{{FB_NS}}}id").text = f"d2l-en-{D2L_REVISION}-selected-v1"
    SubElement(document_info, f"{{{FB_NS}}}version").text = "1.0"
    body = SubElement(root, f"{{{FB_NS}}}body")
    packaging = _section(body, PACKAGING_TITLE, "source-attribution")
    for paragraph in (
        attribution,
        f"Book text license: CC BY-SA 4.0; {D2L_BASE}LICENSE. "
        f"Sample/reference code: modified MIT; {D2L_BASE}LICENSE-SAMPLECODE. "
        "Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.",
        f"This is a partial selection of {len(chapters)} source files, not the complete book. "
        "Book RAG converted pinned Markdown into FB2 with explicit headings and paragraphs. "
        "Markdown markup, fenced code and LaTeX remain literal source text. "
        "Images are not fetched or rendered; formulas are not rendered; no code is executed. "
        "Adapted book text is distributed under CC BY-SA 4.0. "
        "Complete original license notices and source hashes accompany this file.",
    ):
        SubElement(packaging, f"{{{FB_NS}}}p").text = paragraph
    for ordinal, chapter in enumerate(chapters, 1):
        section = _section(body, chapter["title"], f"source-{ordinal:02d}")
        stack = [(0, section)]
        skipped_title = False
        for block_index, block in enumerate(chapter["blocks"]):
            if block["kind"] == "heading":
                if not skipped_title and block["level"] == 1:
                    skipped_title = True
                    continue
                level = block["level"]
                while len(stack) > 1 and stack[-1][0] >= level:
                    stack.pop()
                nested = _section(stack[-1][1], block["text"], f"source-{ordinal:02d}-h{block_index}")
                stack.append((level, nested))
            else:
                paragraph = SubElement(stack[-1][1], f"{{{FB_NS}}}p")
                if block["kind"] == "verbatim":
                    SubElement(paragraph, f"{{{FB_NS}}}code", {XML_SPACE: "preserve"}).text = _literal(block["text"])
                else:
                    paragraph.text = _literal(block["text"])
    return tostring(root, encoding="utf-8", xml_declaration=True)


def acquire_d2l(destination: Path) -> dict[str, Any]:
    """Acquire 15 pinned source sections as one clearly labelled partial work.

    Downloaded originals, cache sidecars, complete license notices, per-section
    provenance, a derived FB2 and a single-entry import manifest are stored below
    ``destination``. Successful cache hits are reused after URL/size/hash checks.
    No full PDF, datasets, images or models are downloaded. Book code remains
    inert literal text and is never executed.
    """
    destination = destination.resolve()
    raw_dir = destination / "raw"
    provenance: list[dict[str, Any]] = []
    hits = 0
    misses = 0

    def obtain(source_path: str) -> tuple[Path, dict[str, Any]]:
        nonlocal hits, misses
        identity = f"d2l-{D2L_REVISION[:12]}-{_sha(source_path.encode())[:16]}"
        entry = {"id": identity, "format": "txt", "download_url": D2L_BASE + source_path}
        cached = cached_source(entry, raw_dir)
        if cached is not None:
            hits += 1
            path = cached
        else:
            misses += 1
            path = download_source(entry, raw_dir)
        sidecar = json.loads(path.with_suffix(".txt.source.json").read_text(encoding="utf-8"))
        info = {
            "source_path": source_path,
            "source_url": entry["download_url"],
            "source_revision": D2L_REVISION,
            "local_path": str(path.relative_to(destination)),
            "sha256": _sha(path.read_bytes()),
            "bytes": path.stat().st_size,
            "retrieved_at": sidecar["retrieved_at"],
            "final_url": sidecar["final_url"],
        }
        return path, info

    licenses: list[dict[str, Any]] = []
    for name, expected in D2L_LICENSES.items():
        path, info = obtain(name)
        if info["sha256"] != expected:
            raise ValueError(f"Pinned D2L {name} does not match its verified license SHA-256; acquisition stopped")
        _atomic_write(destination / "licenses" / f"{name}.txt", path.read_bytes())
        licenses.append(info)
    chapters: list[dict[str, Any]] = []
    for source_path in D2L_CHAPTERS:
        path, info = obtain(source_path)
        markdown = path.read_text(encoding="utf-8-sig")
        blocks = _markdown_blocks(markdown)
        title = next((b["text"] for b in blocks if b["kind"] == "heading" and b["level"] == 1), None)
        if not title:
            raise ValueError(f"Pinned source {source_path} has no top-level chapter heading")
        info.update(
            title=title,
            authors=list(D2L_AUTHORS),
            edition=f"{D2L_VERSION}; {D2L_REVISION}",
            license="CC BY-SA 4.0; sample code under separate modified MIT license",
            blocks=[
                {
                    **{k: v for k, v in b.items() if k != "text"},
                    "sha256": _sha(b["text"].encode()),
                    **({"heading": b["text"]} if b["kind"] == "heading" else {}),
                }
                for b in blocks
            ],
        )
        provenance.append(info)
        chapters.append({"title": title, "blocks": blocks})
    attribution = (
        "Dive into Deep Learning by " + ", ".join(D2L_AUTHORS) + ". "
        f"English source {D2L_VERSION}, commit {D2L_REVISION}. Source: {D2L_SOURCE}. "
        "Selected source chapters; converted and packaged by Book RAG; CC BY-SA 4.0 book text."
    )
    book_bytes = _render_fb2(chapters, attribution)
    book_path = destination / f"d2l-selected-{_sha(book_bytes)[:16]}.fb2"
    source_bundle = _sha(
        json.dumps({p["source_path"]: p["sha256"] for p in [*licenses, *provenance]}, sort_keys=True).encode()
    )
    chapter_map = [{k: v for k, v in p.items() if k != "blocks"} for p in provenance]
    entry = {
        "id": "d2l-en-v103-selected",
        "title": "Dive into Deep Learning — selected source chapters",
        "author": "; ".join(D2L_AUTHORS),
        "language": "en",
        "work_id": "d2l-en",
        "work_identity_status": "curated",
        "work_type": "book_or_text",
        "edition": (
            f"English {D2L_VERSION}, source {D2L_REVISION}; selected {len(chapters)} source sections; FB2 conversion v1"
        ),
        "source_url": D2L_SOURCE,
        "format": "fb2",
        "local_path": book_path.name,
        "rights": "Book text and derived textual packaging: CC BY-SA 4.0. "
        "Sample/reference code: original modified MIT license. Attribution and original licenses retained.",
        "rights_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "source_license_url": D2L_BASE + "LICENSE",
        "source_revision": D2L_REVISION,
        "source_bundle_sha256": source_bundle,
        "artifact_sha256": _sha(book_bytes),
        "coverage": "selected_chapters",
        "coverage_note": f"{len(chapters)} selected source sections; not the complete book",
        "attribution": attribution,
        "chapter_sources": chapter_map,
        "section_attributions": [{"title_contains": PACKAGING_TITLE, "author": "Book RAG", "role": "editorial"}],
        "retrieved_at": max(p["retrieved_at"] for p in [*licenses, *provenance]),
    }
    manifest_path = destination / "manifest.json"
    _atomic_write(book_path, book_bytes)
    _atomic_write(
        destination / "provenance.json",
        (
            json.dumps(
                {
                    "source_revision": D2L_REVISION,
                    "version": D2L_VERSION,
                    "source_bundle_sha256": source_bundle,
                    "artifact_sha256": _sha(book_bytes),
                    "licenses": licenses,
                    "chapters": provenance,
                    "modifications": "Markdown wrapped as literal FB2 paragraphs and headings; "
                    "code/math blocks explicitly preserved; "
                    "original Markdown bytes retained separately. No image or formula rendering, no code execution.",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode(),
    )
    _atomic_write(manifest_path, (json.dumps([entry], ensure_ascii=False, indent=2) + "\n").encode())
    return {
        "manifest": manifest_path,
        "book": book_path,
        "chapters": len(chapters),
        "bytes": sum(p["bytes"] for p in [*licenses, *provenance]),
        "source_revision": D2L_REVISION,
        "source_bundle_sha256": source_bundle,
        "artifact_sha256": _sha(book_bytes),
        "cached_sources": hits,
        "downloaded_sources": misses,
        "coverage": "selected_chapters",
    }
