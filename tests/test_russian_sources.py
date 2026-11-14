"""Bounded Wikisource access, revision proof and complete-work publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from book_rag import russian_sources
from book_rag.parsers import parse_book
from book_rag.russian_sources import RussianWork, acquire_russian
from book_rag.sources import load_manifest

TEXT = "Полный оригинальный текст главы содержит проверяемые предложения и точные слова автора. " * 3


def document(content: str, revision: int | None = 7, *, licensed: bool = True, pd: bool = True) -> bytes:
    script = '<script>{"wgRevisionId":' + str(revision) + "}</script>" if revision is not None else ""
    credit = (
        "<table><tr><td>Иван Автор, опубликовано 1850 — "
        + ("Общественное достояние" if pd else "Права не установлены")
        + "</td></tr></table>"
    )
    license_link = '<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA</a>' if licensed else ""
    return (
        "<html><head>"
        + script
        + '</head><body><div class="mw-parser-output">'
        + credit
        + content
        + "</div><footer>"
        + license_link
        + "</footer></body></html>"
    ).encode()


@pytest.fixture
def wiki_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[RussianWork, dict[str, bytes]]:
    root = "Книга (Автор)"
    chapters = (root + "/Глава 1", root + "/Глава 2")
    work = RussianWork(
        "test-work",
        "Книга",
        "Иван Автор",
        root,
        1850,
        1880,
        "Проверенная электронная редакция",
        chapters,
        require_pd_banner=True,
        author_note_signatures=("Иван Автор",),
    )
    links = "".join('<a href="/wiki/' + c.replace(" ", "_") + '">Глава</a>' for c in chapters)
    pages = {
        root: document(links),
        chapters[0]: document("<h2>I</h2><p>" + TEXT + "</p>"),
        chapters[1]: document("<h2>II</h2><p>" + TEXT + "</p>"),
    }
    monkeypatch.setattr(russian_sources, "RUSSIAN_WORKS", (work,))
    monkeypatch.setattr(russian_sources.time, "sleep", lambda delay: None)
    return work, pages


def install_wiki(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, bytes],
    *,
    revision_after: int | None = None,
    throttle: bool = False,
    displayed_revisions: dict[int, tuple[str, int]] | None = None,
) -> list[str]:
    client = httpx.Client
    calls: list[str] = []
    metadata_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls
        calls.append(str(request.url))
        assert request.headers["user-agent"] == russian_sources.USER_AGENT
        assert "gzip" in request.headers["accept-encoding"]
        assert request.url.host == "ru.wikisource.org"
        if throttle:
            return httpx.Response(429, headers={"retry-after": "3600"})
        if request.url.path == "/w/api.php":
            assert request.url.params["maxlag"] == "5"
            assert request.url.params["action"] == "query"
            metadata_calls += 1
            if "revids" in request.url.params:
                revision_id = int(request.url.params["revids"])
                displayed = (displayed_revisions or {}).get(revision_id)
                if displayed is None:
                    return httpx.Response(200, json={"query": {"pages": []}})
                title, page_id = displayed
                return httpx.Response(
                    200,
                    json={
                        "query": {
                            "pages": [
                                {
                                    "pageid": page_id,
                                    "title": title,
                                    "revisions": [
                                        {"revid": revision_id, "timestamp": "2025-01-01T00:00:00Z", "sha1": "f" * 40}
                                    ],
                                }
                            ]
                        }
                    },
                )
            titles = request.url.params["titles"].split("|")
            revision = revision_after if revision_after is not None and metadata_calls > 1 else 7
            records = [
                {
                    "pageid": 100 + index,
                    "title": title,
                    "revisions": [{"revid": revision, "timestamp": "2026-01-01T00:00:00Z", "sha1": "0" * 40}],
                }
                for index, title in enumerate(titles)
            ]
            return httpx.Response(200, json={"query": {"pages": records}})
        assert request.url.path.startswith("/wiki/") and not request.url.query
        title = unquote(request.url.path[len("/wiki/") :]).replace("_", " ")
        return httpx.Response(200, content=pages[title]) if title in pages else httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    return calls


def test_complete_work_has_revision_provenance_and_exact_stored_quotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    calls = install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 1 and report["chapters"] == 2 and not report["failures"]
    entry = load_manifest(report["manifest"])[0]
    assert entry["coverage"] == "full"
    assert entry["work_identity_status"] == "curated"
    assert entry["work_id"] == "ru-test-work"
    for source in entry["chapter_sources"]:
        assert source["revision_id"] == 7
        assert source["revision_evidence"] == "html_wgRevisionId"
        assert source["raw_sha256"] == hashlib.sha256(pages[source["title"]]).hexdigest()
        assert (tmp_path / source["raw_path"]).read_bytes() == pages[source["title"]]
    parsed = parse_book(Path(entry["local_path"]))
    assert TEXT.strip() in "\n\n".join(s.text for s in parsed.sections)
    assert report["network_requests"] == len(calls) == 5


def test_resume_has_zero_network_and_deterministic_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    _, pages = wiki_fixture
    calls = install_wiki(monkeypatch, pages)
    first = acquire_russian(tmp_path, limit=1)
    before = first["manifest"].read_bytes()
    second = acquire_russian(tmp_path, limit=1)
    assert second["network_requests"] == 0
    assert second["cache_hits"] >= 3
    assert second["manifest"].read_bytes() == before
    assert len(calls) == 5


def test_toc_duplicates_do_not_duplicate_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    links = "".join('<a href="/wiki/' + c.replace(" ", "_") + '">Глава</a>' for c in reversed(work.chapters))
    pages[work.root] = document(links + links)
    install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    entry = load_manifest(report["manifest"])[0]
    assert [p["title"] for p in entry["chapter_sources"]] == list(work.chapters)


def test_outside_host_chapter_link_is_rejected_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.root] = document(
        '<a href="https://evil.invalid/wiki/' + work.chapters[0].replace(" ", "_") + '">Глава</a>'
    )
    calls = install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 0
    assert "allowed canonical" in report["failures"][0]["error"]
    assert len(calls) == 2


@pytest.mark.parametrize("licensed,pd", [(False, True), (True, False)])
def test_missing_rights_proof_stops_before_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
    licensed: bool,
    pd: bool,
) -> None:
    work, pages = wiki_fixture
    pages[work.root] = document("<p>Index</p>", licensed=licensed, pd=pd)
    calls = install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 0 and len(calls) == 2
    assert load_manifest(report["manifest"]) == []


def test_incomplete_work_retains_verified_pages_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    missing = pages.pop(work.chapters[1])
    calls = install_wiki(monkeypatch, pages)
    failed = acquire_russian(tmp_path, limit=1)
    assert failed["works_ready"] == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["works"][work.slug]["coverage"] == "partial"
    assert state["pages"][work.chapters[0]]["status"] == "verified"
    pages[work.chapters[1]] = missing
    resumed = acquire_russian(tmp_path, limit=1)
    assert resumed["works_ready"] == 1
    assert resumed["network_requests"] == 2
    assert len(calls) == 7


def test_html_revision_mismatch_never_gets_labelled_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.root] = document("<p>Index</p>", revision=8)
    install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 0
    assert "revision mismatch" in report["failures"][0]["error"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["pages"][work.root]["status"] == "failed"


def test_metadata_pre_post_fallback_rejects_revision_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.root] = document("<p>Index</p>", revision=None)
    install_wiki(monkeypatch, pages, revision_after=8)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 0
    assert "Revision changed" in report["failures"][0]["error"]


def test_signed_author_note_survives_while_unsigned_editorial_note_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.chapters[0]] = document(
        "<h2>I</h2><p>" + TEXT + '<sup><a href="#cite_note-1">[1]</a></sup></p>'
        '<ol class="references"><li id="cite_note-1"><span class="reference-text">Авторское объяснение. '
        '<p>Иван Автор</p></span></li><li id="cite_note-2">Поздняя редакционная заметка.</li></ol>'
    )
    install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    entry = load_manifest(report["manifest"])[0]
    text = "\n\n".join(s.text for s in parse_book(Path(entry["local_path"])).sections)
    assert "Авторское объяснение." in text and "Иван Автор" in text
    assert "Поздняя редакционная заметка" not in text
    extraction = entry["chapter_sources"][0]["extraction"]
    assert extraction["retained_author_notes"] == 1 and len(extraction["omitted_notes"]) == 1


def test_host_deferral_stops_all_requests_and_persists_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    _, pages = wiki_fixture
    calls = install_wiki(monkeypatch, pages, throttle=True)
    first = acquire_russian(tmp_path, limit=1)
    assert first["deferred"] and first["network_requests"] == 1 and first["retry_at"]
    second = acquire_russian(tmp_path, limit=1)
    assert second["deferred"] and second["network_requests"] == 0
    assert len(calls) == 1


def test_acquisition_limit_is_bounded_before_network(tmp_path: Path) -> None:
    for limit in (0, 6):
        with pytest.raises(ValueError, match="one and five"):
            acquire_russian(tmp_path, limit=limit)


def test_displayed_stable_revision_is_verified_by_id_and_not_labelled_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    links = "".join('<a href="/wiki/' + c.replace(" ", "_") + '">Глава</a>' for c in work.chapters)
    pages[work.root] = document(links, revision=6)
    install_wiki(monkeypatch, pages, displayed_revisions={6: (work.root, 100)})
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 1
    record = json.loads((tmp_path / "state.json").read_text())["pages"][work.root]
    assert record["revision_id"] == 6
    assert record["api_latest_revision_at_retrieval"] == 7
    assert record["revision_evidence"] == "html_displayed_revision_verified_by_id"
    assert record["permanent_url"].endswith("oldid=6")


def test_displayed_revision_for_another_page_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.root] = document("<p>Index</p>", revision=6)
    install_wiki(monkeypatch, pages, displayed_revisions={6: ("Unrelated page", 999)})
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 0
    assert "revision mismatch" in report["failures"][0]["error"]


def test_metadata_fallback_verifies_stable_retrieval_when_html_has_no_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    links = "".join('<a href="/wiki/' + c.replace(" ", "_") + '">Глава</a>' for c in work.chapters)
    pages[work.root] = document(links, revision=None)
    install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    assert report["works_ready"] == 1
    record = json.loads((tmp_path / "state.json").read_text())["pages"][work.root]
    assert record["revision_evidence"] == "fresh_metadata_pre_post_match"


def test_invalid_persisted_state_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="Invalid Wikisource acquisition state"):
        acquire_russian(tmp_path, limit=1)
    assert path.read_text() == "[]"


def test_dedication_and_epigraph_author_credit_are_preserved_without_duplicate_verse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wiki_fixture: tuple[RussianWork, dict[str, bytes]],
) -> None:
    work, pages = wiki_fixture
    pages[work.chapters[0]] = document(
        '<div class="epigraph2"><div>Посвящается памяти <a href="/wiki/Друг">Друга</a></div></div>'
        '<div class="epigraph2"><div class="poem"><p>Строка первая.<br>Строка вторая.</p></div>'
        '<div class="align-right"><i>Из поэзии Другого Автора</i></div></div>'
        "<h2>I</h2><p>" + TEXT + "</p>"
    )
    install_wiki(monkeypatch, pages)
    report = acquire_russian(tmp_path, limit=1)
    entry = load_manifest(report["manifest"])[0]
    text = "\n\n".join(s.text for s in parse_book(Path(entry["local_path"])).sections)
    assert "Посвящается памяти Друга" in text
    assert "Из поэзии Другого Автора" in text
    assert text.count("Строка первая.") == text.count("Строка вторая.") == 1
    assert entry["coverage"] == "full" and entry["coverage_scope"] == "original_work_only"
