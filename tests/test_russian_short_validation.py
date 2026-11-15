"""Independent raw-DOM/canonical-order proof catches omissions and duplicates."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from book_rag import russian_short_sources as short
from book_rag import russian_sources as ws

validate = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "validate_russian_short_works.py"))[
    "validate_projection"
]
TEXT = "Точные слова источника, авторская пунктуация и ёжик должны сохраняться в порядке. " * 4


def captured() -> tuple[short.ShortWork, bytes, dict[str, object], str]:
    selected = short.ShortWork(
        ws.RussianWork("test", "Рассказ", "Иван Автор", "Рассказ (Автор)", 1850, 1880, "Издание"),
        expected_markers=("I", "II"),
    )
    raw = (
        '<html><body><div class="mw-parser-output"><table><tr><td>Иван Автор,1850.</td></tr></table>'
        "<p>Начало эпиграфа.</p><center>I</center><p>" + TEXT + '<span class="ws-noexport" id="109">[109]</span></p>'
        "<h3>II</h3><p>Последняя глава. " + TEXT + "Конец.</p></div>"
        '<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC</a></body></html>'
    ).encode()
    bound: dict[str, object] = {
        "raw_sha256": ws._sha(raw),
        "review_status": "verified_original_work",
        "body_start": "Начало",
        "body_end": "Конец.",
        "review_basis": "Inspected complete source",
        "author_note_ids": [],
        "editorial_note_ids": [],
    }
    body, _ = short.project_short_work(selected, raw, bound)
    return selected, raw, bound, body


def test_actual_order_proof_retains_unicode_and_excludes_page_controls() -> None:
    selected, raw, bound, body = captured()
    proof = validate(selected, raw, bound, body)
    assert proof["source_character_stream_sha256"] == proof["canonical_character_stream_sha256"]
    assert proof["original_chapter_markers"] == 2
    assert proof["matched_nonwhitespace_characters"] > 400


@pytest.mark.parametrize("mode", ["omission", "duplication"])
def test_order_proof_detects_real_text_loss_or_duplication(mode: str) -> None:
    selected, raw, bound, body = captured()
    changed = body.replace("Точные слова источника", "" if mode == "omission" else "Точные слова источника" * 2, 1)
    with pytest.raises(ValueError, match="character stream differs"):
        validate(selected, raw, bound, changed)


def test_heading_loss_is_detected_separately_from_prose() -> None:
    selected, raw, bound, body = captured()
    with pytest.raises(ValueError, match="heading sequence"):
        validate(selected, raw, bound, body.replace("<h2>I</h2>", ""))
