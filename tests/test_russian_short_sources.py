"""Whole-work source gates, exact projection and shared-provider resumption."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from bs4 import BeautifulSoup

from book_rag import russian_short_sources as short
from book_rag import russian_sources as ws
from book_rag.parsers import parse_book

TEXT = "Оригинальные слова автора остаются в точном порядке, включая ёжика, цитаты и тире. " * 4


def document(content: str, *, license_link: bool = True, credit: str = "Иван Автор") -> bytes:
    return (
        '<html lang="ru"><head><script>{"wgRevisionId":7}</script></head><body>'
        '<div class="mw-parser-output"><table><tr><td>'
        + credit
        + ", опубликовано 1850. Общественное достояние.</td></tr></table>"
        + content
        + "</div><footer>"
        + ('<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>' if license_link else "")
        + "</footer></body></html>"
    ).encode()


def selection(*, markers: tuple[str, ...] = ()) -> short.ShortWork:
    return short.ShortWork(
        ws.RussianWork("test", "Рассказ", "Иван Автор", "Рассказ (Автор)", 1850, 1880, "Издание 1850"),
        expected_markers=markers,
        original_publication="Журнал, 1850",
        author_source=ws._url("Автор:Иван Автор"),
    )


def review(data: bytes, *, start: str = "Начало", end: str = "Конец.") -> dict[str, object]:
    return {
        "raw_sha256": ws._sha(data),
        "review_status": "verified_original_work",
        "body_start": start,
        "body_end": end,
        "review_basis": "Source work body and every note individually inspected",
        "author_note_ids": [],
        "editorial_note_ids": [],
    }


def provider_state(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "state.json").write_text(json.dumps({"schema_version": 1, "pages": {}, "works": {}}))
    (path / "manifest.json").write_text('[{"id":"original-five-untouched"}]')


def install_provider(monkeypatch: pytest.MonkeyPatch, source: bytes, *, status: int = 200) -> list[str]:
    client = httpx.Client
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.host == "ru.wikisource.org"
        assert request.headers["user-agent"] == ws.USER_AGENT
        assert "gzip" in request.headers["accept-encoding"]
        if status != 200:
            return httpx.Response(status, headers={"Retry-After": "3600"})
        if request.url.path == "/w/api.php":
            assert request.url.params["maxlag"] == "5"
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "title": selection().work.root,
                                "pageid": 55,
                                "revisions": [
                                    {"revid": 7, "timestamp": "2020-01-01T00:00:00Z", "sha1": "providerhash"}
                                ],
                            }
                        ]
                    }
                },
            )
        assert unquote(request.url.path).endswith("/Рассказ_(Автор)")
        return httpx.Response(200, content=source)

    monkeypatch.setattr(ws.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(ws.time, "sleep", lambda delay: None)
    return calls


def test_fixed_selection_is_ten_distinct_lifetime_originals() -> None:
    assert len(short.SHORT_WORKS) == 10
    assert len({s.work.slug for s in short.SHORT_WORKS}) == 10
    assert len({s.work.root for s in short.SHORT_WORKS}) == 10
    assert len({s.work.author for s in short.SHORT_WORKS}) == 8
    assert all(s.work.publication_year <= s.work.death_year <= 1910 for s in short.SHORT_WORKS)
    assert all(not s.work.chapters and s.original_publication for s in short.SHORT_WORKS)
    assert "Вариант журнала" in next(s.work.root for s in short.SHORT_WORKS if s.work.slug == "chameleon")


def test_projection_preserves_blocks_headings_verse_table_and_literal_code(tmp_path: Path) -> None:
    code = '  result = "a  b"\n\tprint(result)\n'
    data = document(
        "<p>Указатель сайта</p><p>Начало — исходный эпиграф.</p><div>I</div>"
        "<p>" + TEXT + '</p><div class="poem">Первая строка<br>Вторая строка</div>'
        "<pre>" + code + "</pre><table><tr><th>Тема</th><th>Слова</th></tr>"
        "<tr><td>Строка</td><td>один<br>два</td></tr></table>"
        "<h3>II</h3><p>Последние " + TEXT + "Конец.</p><h2>Примечания</h2><p>Поздний комментарий.</p>"
    )
    body, audit = short.project_short_work(selection(markers=("I", "II")), data, review(data))
    assert audit["body_blocks"] == 8
    assert audit["chapter_markers"] == ["I", "II"]
    assert "Поздний комментарий" not in body and "Указатель сайта" not in body
    pre = BeautifulSoup(body, "html.parser").pre
    assert pre is not None and pre.get_text() == code
    path = tmp_path / "story.html"
    path.write_text("<html><body><h1>Рассказ</h1>" + body + "</body></html>")
    parsed = parse_book(path)
    canonical = "\n\n".join(s.text for s in parsed.sections)
    assert code in canonical
    assert "Первая строка\nВторая строка" in canonical
    assert "Тема\tСлова" in canonical and "Строка\tодин\nдва" in canonical
    assert canonical.count("Оригинальные слова автора") == 8
    assert [s.title for s in parsed.sections] == ["Рассказ", "I", "II"]


@pytest.mark.parametrize("mutation", ["sha", "status", "start", "end", "basis"])
def test_unverified_changed_or_incomplete_source_cannot_claim_full(mutation: str) -> None:
    data = document("<p>Начало " + TEXT + "Конец.</p>")
    bound = review(data)
    if mutation == "sha":
        bound["raw_sha256"] = "0" * 64
    elif mutation == "status":
        bound["review_status"] = "captured_webpage"
    elif mutation == "start":
        bound["body_start"] = "Несуществующее начало"
    elif mutation == "end":
        bound["body_end"] = "Обрезанный хвост"
    else:
        bound["review_basis"] = ""
    with pytest.raises(ValueError):
        short.project_short_work(selection(), data, bound)


def test_missing_original_chapter_fails_closed() -> None:
    data = document("<p>Начало</p><h2>I</h2><p>" + TEXT + "Конец.</p>")
    with pytest.raises(ValueError, match="chapter marker sequence"):
        short.project_short_work(selection(markers=("I", "II")), data, review(data))


def test_first_heading_before_body_boundary_is_retained() -> None:
    data = document("<h2>I</h2><p>Начало " + TEXT + "Конец.</p>")
    body, audit = short.project_short_work(selection(markers=("I",)), data, review(data))
    heading = BeautifulSoup(body, "html.parser").find("h2")
    assert heading is not None and heading.get_text() == "I"
    assert audit["chapter_markers"] == ["I"]
    assert audit["chapter_bodies"] == [
        {"marker": "I", "body_blocks": 1, "body_characters": len("Начало " + TEXT + "Конец.")}
    ]


@pytest.mark.parametrize(
    "empty_body",
    [
        "",
        "<p>   </p>",
        "<p>*** — —</p>",
        "<h3>Подзаголовок без текста</h3>",
        '<p><sup class="reference"><a href="#missing">[1]</a></sup></p>',
        '<div class="epigraph">Только эпиграф, повествование отсутствует.</div>',
    ],
)
def test_present_marker_without_narrative_body_fails_closed(empty_body: str) -> None:
    # A long prelude and the next chapter used to satisfy the global length
    # check even though original chapter I had no body at all.
    data = document(
        "<p>Начало "
        + TEXT
        + "</p><h2>I</h2>"
        + empty_body
        + "<h2>II</h2><p>Существующая вторая глава "
        + TEXT
        + "Конец.</p>"
    )
    with pytest.raises(ValueError, match="no narrative body: I"):
        short.project_short_work(selection(markers=("I", "II")), data, review(data))


def test_final_marker_without_body_also_fails_closed() -> None:
    data = document("<p>Начало</p><h2>I</h2><p>" + TEXT + "</p><h2>II</h2>")
    with pytest.raises(ValueError, match="no narrative body: II"):
        short.project_short_work(selection(markers=("I", "II")), data, review(data, end="II"))


def test_wise_gudgeon_edition_uses_verified_primary_titlepage() -> None:
    work = next(item.work for item in short.SHORT_WORKS if item.work.slug == "wise-gudgeon")
    assert "Собрание сочинений в 20 томах" in work.edition
    assert "Художественная литература, 1974, т. 16, кн. 1, с. 30–34" in work.edition
    assert "титульному листу РВБ" in work.edition
    assert "заголовок Викитеки содержит ошибочное описание" in work.edition
    assert "Издательство АН СССР" not in work.edition


def test_captured_center_headings_and_conclusion_are_retained(tmp_path: Path) -> None:
    data = document(
        '<center>Начало — источник эпиграфа.</center><center style="font-size:large">I</center>'
        "<p>" + TEXT + '</p><center><span class="razr">Заключение</span></center>'
        "<p>" + TEXT + "Конец.</p>"
    )
    body, audit = short.project_short_work(selection(markers=("I", "Заключение")), data, review(data))
    path = tmp_path / "center.html"
    path.write_text("<html><body><h1>Рассказ</h1>" + body + "</body></html>")
    parsed = parse_book(path)
    assert [s.title for s in parsed.sections] == ["Рассказ", "I", "Заключение"]
    assert "Начало — источник эпиграфа." in parsed.sections[0].text
    assert len(audit["chapter_bodies"]) == 2


def test_lefty_requires_all_twenty_source_chapters() -> None:
    chosen = next(item for item in short.SHORT_WORKS if item.work.slug == "lefty")
    assert len(chosen.expected_markers) == 20
    assert chosen.expected_markers[3] == "ГЛАВА ЧЕТВЕРТАЯ."
    assert chosen.expected_markers[-1] == "ГЛАВА ДВАДЦАТАЯ."


def test_unverified_note_needs_explicit_partial_scope() -> None:
    data = document(
        "<p>Начало " + TEXT + '<sup class="reference"><a href="#cite_note-u">[1]</a></sup>'
        'Конец.</p><ol class="references"><li id="cite_note-u">Неатрибутированная gloss.</li></ol>'
    )
    bound = review(data)
    bound["unverified_note_ids"] = ["cite_note-u"]
    with pytest.raises(ValueError, match="prohibit a full"):
        short.project_short_work(selection(), data, bound)
    bound["review_status"] = "verified_original_body_partial_notes"
    with pytest.raises(ValueError, match="needs explicit"):
        short.project_short_work(selection(), data, bound)
    bound["partial_reason"] = "Narrative is complete; original note authorship is unverified."
    body, audit = short.project_short_work(selection(), data, bound)
    assert audit["coverage"] == "partial" and audit["unverified_note_ids"] == ["cite_note-u"]
    assert "Неатрибутированная" not in body and "[1]" not in body


def test_printed_page_controls_kept_as_provenance_not_quote_text() -> None:
    data = document("<p>Начало " + TEXT + '<span id="109" class="ws-noexport">[109]</span>Конец.</p>')
    body, audit = short.project_short_work(selection(), data, review(data))
    assert "[109]" not in body
    assert audit["source_printed_pages"] == [{"printed_page": "109", "raw_html_id": "109", "label": "[109]"}]
    assert audit["printed_page_mapping"] == "raw_html_anchors_only"


def test_html_formatting_after_br_does_not_invent_blank_verse_lines(tmp_path: Path) -> None:
    data = document(
        "<p>Начало " + TEXT + '</p><div class="poem"><p>Строка первая<br/>\n'
        "  Строка вторая<br/><br/>\nСтрока третья</p></div><p>Конец.</p>"
    )
    body, _ = short.project_short_work(selection(), data, review(data))
    path = tmp_path / "verse.html"
    path.write_text("<html><body>" + body + "</body></html>")
    canonical = "\n\n".join(section.text for section in parse_book(path).sections)
    assert "Строка первая\nСтрока вторая\n\nСтрока третья" in canonical


def test_loose_text_between_blocks_is_not_silently_lost() -> None:
    data = document("<p>Начало " + TEXT + "</p>ВАЖНАЯ НЕРАЗМЕЧЕННАЯ СТРОКА<p>Конец.</p>")
    with pytest.raises(ValueError, match="Unrepresented literal"):
        short.project_short_work(selection(), data, review(data))


def test_every_note_classified_once_author_note_kept_editorial_reference_removed() -> None:
    data = document(
        "<p>Начало " + TEXT + '<sup class="reference"><a href="#cite_note-a">[1]</a></sup>'
        '<sup class="reference"><a href="#cite_note-e">[2]</a></sup>Конец.</p>'
        '<h2>Примечания</h2><ol class="references">'
        '<li id="cite_note-a"><span class="mw-cite-backlink">↑</span>Авторское пояснение. (Примечание автора)</li>'
        '<li id="cite_note-e">Современное редакционное пояснение.</li></ol>'
    )
    bound = review(data)
    with pytest.raises(ValueError, match="Every source note"):
        short.project_short_work(selection(), data, bound)
    bound.update(author_note_ids=["cite_note-a"], editorial_note_ids=["cite_note-e"])
    body, audit = short.project_short_work(selection(), data, bound)
    assert "Авторское пояснение" in body
    assert "Современное редакционное" not in body and "[2]" not in body
    assert "#test-cite_note-a" in body and 'id="test-cite_note-a"' in body
    assert "↑" not in body
    assert audit["author_note_ids"] == ["cite_note-a"]
    bound["editorial_note_ids"] = ["cite_note-e", "cite_note-a"]
    with pytest.raises(ValueError, match="Every source note"):
        short.project_short_work(selection(), data, bound)


def test_license_and_explicit_author_credit_required() -> None:
    data = document("<p>Начало " + TEXT + "Конец.</p>", license_link=False)
    with pytest.raises(ValueError, match="license"):
        short.project_short_work(selection(), data, review(data))
    data = document("<p>Начало " + TEXT + "Конец.</p>", credit="И. Автор")
    with pytest.raises(ValueError, match="author credit"):
        short.project_short_work(selection(), data, review(data))
    alias = replace(selection(), credited_author="И. Автор")
    assert short.project_short_work(alias, data, review(data))[1]["rights"]["credited_author"] == "И. Автор"


def test_shared_cooldown_prevents_client_and_new_destination_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "provider"
    provider_state(cache)
    guard = tmp_path / "wikidata-deferred.json"
    guard.write_text(json.dumps({"retry_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}))
    monkeypatch.setattr(
        ws.httpx, "Client", lambda **kwargs: pytest.fail("Network client created before cooldown guard")
    )
    for output in (tmp_path / "output-one", tmp_path / "fresh-output"):
        with pytest.raises(ws.WikiDeferred, match="shared access-state cooldown"):
            short.acquire_russian_short_works(output, shared_cache=cache, cooldown_states=(guard,))
        assert not output.exists()


@pytest.mark.parametrize("value", ["garbage", "2026-01-01T00:00:00", 42])
def test_invalid_shared_cooldown_fails_closed(tmp_path: Path, value: object) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"retry_at": value}))
    with pytest.raises(ValueError):
        short._check_cooldowns((path,))


def test_real_cache_resume_uses_zero_http_and_retains_source_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = selection(markers=("I",))
    data = document("<h2>I</h2><p>Начало " + TEXT + "Конец.</p>")
    monkeypatch.setattr(short, "SHORT_WORKS", (chosen,))
    calls = install_provider(monkeypatch, data)
    cache, output = tmp_path / "provider", tmp_path / "output"
    provider_state(cache)
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps({"test": review(data)}))
    first = short.acquire_russian_short_works(output, shared_cache=cache, reviews_path=reviews, limit=1)
    assert first["works_ready"] == 1 and first["network_requests"] == 2 and len(calls) == 2
    before = json.loads((output / "manifest.json").read_text())[0]
    artifact = output / before["local_path"]
    old_bytes = artifact.read_bytes()
    original_state = json.loads((cache / "state.json").read_text())
    second = short.acquire_russian_short_works(output, shared_cache=cache, reviews_path=reviews, limit=1)
    after = json.loads((output / "manifest.json").read_text())[0]
    assert second["network_requests"] == 0 and second["cache_hits"] == 1 and len(calls) == 2
    assert before["id"] == after["id"] and before["retrieved_at"] == after["retrieved_at"]
    assert before["artifact_sha256"] == after["artifact_sha256"] == ws._sha(old_bytes)
    assert artifact.read_bytes() == old_bytes
    assert after["packaged_at"] >= before["packaged_at"]
    raw = output / after["chapter_sources"][0]["raw_path"]
    assert raw.read_bytes() == data
    assert json.loads((cache / "state.json").read_text()) == original_state
    assert json.loads((cache / "manifest.json").read_text()) == [{"id": "original-five-untouched"}]
    assert after["coverage"] == "full" and after["coverage_scope"] == "original_work_only"
    assert after["section_attributions"][0]["source_artifact_sha256"] == ws._sha(old_bytes)
    # An accidental capture-only rerun cannot erase previously reviewed books.
    third = short.acquire_russian_short_works(output, shared_cache=cache, limit=1)
    assert third["works_ready"] == 1 and third["network_requests"] == 0


def test_capture_only_has_no_full_work_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = document("<p>Начало " + TEXT + "Конец.</p>")
    monkeypatch.setattr(short, "SHORT_WORKS", (selection(),))
    install_provider(monkeypatch, data)
    cache = tmp_path / "provider"
    provider_state(cache)
    report = short.acquire_russian_short_works(tmp_path / "output", shared_cache=cache, limit=1)
    assert report["works_ready"] == 0 and len(report["captured"]) == 1
    assert report["failures"][0]["status"] == "review_required"
    assert json.loads(Path(report["manifest"]).read_text()) == []
    preserved = tmp_path / "output" / report["captured"][0]["raw_path"]
    assert preserved.read_bytes() == data
    receipt = json.loads(preserved.with_suffix(".receipt.json").read_text())
    assert receipt["raw_sha256"] == ws._sha(data) and receipt["revision_id"] == 7


def test_throttle_persists_cooldown_and_stops_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        short, "SHORT_WORKS", (selection(), replace(selection(), work=replace(selection().work, slug="next")))
    )
    calls = install_provider(monkeypatch, b"", status=429)
    cache = tmp_path / "provider"
    provider_state(cache)
    report = short.acquire_russian_short_works(tmp_path / "output", shared_cache=cache, limit=2)
    assert report["deferred"] and len(calls) == 1
    assert json.loads((cache / "state.json").read_text())["host_deferred_until"] == report["retry_at"]
    with pytest.raises(ws.WikiDeferred):
        short.acquire_russian_short_works(tmp_path / "another-output", shared_cache=cache, limit=2)
    assert len(calls) == 1


def test_existing_shared_cache_and_separate_output_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing shared"):
        short.acquire_russian_short_works(tmp_path / "out", shared_cache=tmp_path / "new")
    provider_state(tmp_path / "provider")
    with pytest.raises(ValueError, match="separate output"):
        short.acquire_russian_short_works(tmp_path / "provider", shared_cache=tmp_path / "provider")


def test_offline_packaging_during_cooldown_creates_no_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = selection()
    data = document("<p>Начало " + TEXT + "Конец.</p>")
    monkeypatch.setattr(short, "SHORT_WORKS", (chosen,))
    calls = install_provider(monkeypatch, data)
    cache, output = tmp_path / "provider", tmp_path / "output"
    provider_state(cache)
    first = short.acquire_russian_short_works(output, shared_cache=cache, limit=1)
    assert first["network_requests"] == 2 and len(calls) == 2
    state_path = cache / "state.json"
    state = json.loads(state_path.read_text())
    state["host_deferred_until"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    state_path.write_text(json.dumps(state))
    before_state = state_path.read_bytes()
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps({"test": review(data)}))
    monkeypatch.setattr(ws.httpx, "Client", lambda **kwargs: pytest.fail("Offline packaging created HTTP client"))
    result = short.acquire_russian_short_works(output, shared_cache=cache, reviews_path=reviews, limit=1, offline=True)
    assert result["works_ready"] == 1 and result["network_requests"] == 0 and result["cache_hits"] == 1
    assert state_path.read_bytes() == before_state


def test_offline_missing_or_corrupt_capture_never_falls_back_to_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(short, "SHORT_WORKS", (selection(),))
    cache = tmp_path / "provider"
    provider_state(cache)
    monkeypatch.setattr(ws.httpx, "Client", lambda **kwargs: pytest.fail("Offline mode created HTTP client"))
    report = short.acquire_russian_short_works(tmp_path / "output", shared_cache=cache, offline=True, limit=1)
    assert report["works_ready"] == 0 and report["network_requests"] == 0
    assert "verified cached capture" in report["failures"][0]["error"]


def test_partial_manifest_does_not_claim_full_original_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = document(
        "<p>Начало " + TEXT + '<sup class="reference"><a href="#cite_note-u">[1]</a></sup>'
        'Конец.</p><ol class="references"><li id="cite_note-u">Неатрибутированная gloss.</li></ol>'
    )
    monkeypatch.setattr(short, "SHORT_WORKS", (selection(),))
    install_provider(monkeypatch, data)
    cache, output = tmp_path / "provider", tmp_path / "output"
    provider_state(cache)
    bound = review(data)
    bound.update(
        review_status="verified_original_body_partial_notes",
        unverified_note_ids=["cite_note-u"],
        partial_reason="Main narrative is complete; source note authorship unverified.",
    )
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps({"test": bound}))
    result = short.acquire_russian_short_works(output, shared_cache=cache, reviews_path=path, limit=1)
    assert result["coverage_counts"] == {"full": 0, "partial": 1}
    entry = json.loads((output / "manifest.json").read_text())[0]
    assert entry["coverage"] == "partial" and entry["coverage_scope"] == "original_body_with_unverified_notes"
    assert entry["notes_excluded"] == [
        {"source_id": "cite_note-u", "reason": "unverified_original_note_authorship_or_scope"}
    ]
    text = (output / entry["local_path"]).read_text()
    assert "полнота авторских примечаний не подтверждена" in text
    assert "выделил полный оригинальный текст" not in text
