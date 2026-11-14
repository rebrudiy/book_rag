"""Read-only Chromium check of the actual indexed pinned Wikibooks reference.

Requires the live library and previously acquired source bundle. Never invokes
acquisition/import, mocks responses, saves annotations or mutates the library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from playwright.sync_api import Locator, Page, expect, sync_playwright

BOOK_ID = "ruwikibooks-regex-266423"
ARTIFACT_SHA256 = "83d7579ab5fc2a6120d14569243c5109e0d09371224138b567072214eed27737"
QUERIES = (
    ('"положительной опережающей проверкой"', "Проверка сложности пароля через lookahead"),
    ('"границы текста"', "Отличить границу текста от границы фрагмента"),
    ('"архивов"', "Выбрать имена архивов по расширению"),
)


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def canonical(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def dom_text(node: Any) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    if isinstance(node, Tag):
        return "\n" if node.name == "br" else "".join(dom_text(c) for c in node.children)
    return ""


def open_details(locator: Locator) -> None:
    if locator.get_attribute("open") is None:
        locator.locator(":scope > summary").click()


def read_section(page: Page, base: str, section: dict[str, Any]) -> str:
    page.goto(base + "/?" + urlencode({"book": BOOK_ID, "section": section["id"]}), wait_until="networkidle")
    expect(page.locator("#reader-section-title")).to_have_text(section["title"])
    expect(page.locator("#reader-text")).not_to_be_empty()
    actual = page.locator("#reader-text").evaluate("el => el.textContent")
    assert actual == section["text"], f"Reader DOM altered section {section['ordinal']} whitespace/text"
    return actual


def check_source(page: Page, book: dict[str, Any]) -> None:
    expect(page.locator("#reader-coverage")).to_have_text("Снимок текста · полнота произведения не подтверждена")
    open_details(page.locator(".source-details"))
    source = page.locator("#reader-source")
    expect(source).to_contain_text("CC BY-SA 4.0")
    expect(source.get_by_role("link", name="Открыть первоисточник")).to_have_attribute("href", book["source_url"])
    assert parse_qs(urlsplit(book["source_url"]).query)["oldid"] == ["266423"]
    expect(source.get_by_role("link", name="Условия использования источника")).to_have_attribute(
        "href", "https://creativecommons.org/licenses/by-sa/4.0/"
    )
    credits = source.locator(".source-upstream-credits li")
    expect(credits).to_have_count(2)
    for index, expected in enumerate(book["source_metadata"]["upstream_attribution"]):
        credit = credits.nth(index)
        expect(credit).to_be_visible()
        expect(credit).to_contain_text(expected["author"])
        for label, field in (("Текст источника", "source_url"), ("История участников", "history_url")):
            link = credit.get_by_role("link", name=label)
            expect(link).to_be_visible()
            assert unquote(link.get_attribute("href") or "") == unquote(expected[field])
    expect(source.locator(".source-variant-note")).to_contain_text("нет иллюстраций")
    open_details(source.locator(".source-attribution"))
    expect(source.locator(".source-attribution")).to_contain_text("текущие версии шаблонов")
    expect(source.locator(".source-attribution")).to_contain_text(book["source_metadata"]["source_wikitext_sha256"])
    expect(source.get_by_role("link", name="Скачать зафиксированный файл", exact=True)).to_have_attribute(
        "href", book["artifact_url"]
    )


def scoped_search(page: Page, base: str, query: str, initial: bool) -> dict[str, Any]:
    if initial:
        page.goto(base, wait_until="networkidle")
        page.locator('[data-mode="lexical"]').click()
    else:
        page.locator("#reader-back").click()
    page.locator("#query").fill(query)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/api/search" and parse_qs(urlsplit(response.url).query).get("q") == [query]
        )
    ) as response:
        page.locator("#search-form").get_by_role("button", name="Найти").click()
    assert response.value.status == 200
    if initial:
        page.locator("#language-filter").select_option("ru")
        page.locator("#book-scope-query").fill("Регулярные выражения")
        expect(page.locator(f'#book-filter option[value="{BOOK_ID}"]')).to_have_count(1)
        with page.expect_response(
            lambda result: (
                urlsplit(result.url).path == "/api/search"
                and parse_qs(urlsplit(result.url).query).get("book_id") == [BOOK_ID]
                and parse_qs(urlsplit(result.url).query).get("q") == [query]
            )
        ) as scoped:
            page.locator("#book-filter").select_option(BOOK_ID)
        response = scoped
    payload = response.value.json()
    assert payload["results"], f"No real source result for {query}"
    hit = payload["results"][0]
    assert hit["book_id"] == BOOK_ID and hit["verified"] is True
    card = page.locator("#fragments-panel .passage-card").first
    expect(card.locator(".verified-label")).to_contain_text("ЦИТАТА ПРОВЕРЕНА")
    assert card.locator("blockquote").evaluate("el => el.textContent") == hit["text"]
    expect(card.locator(".passage-citation")).to_have_text(hit["citation"])
    card.get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#found-passage")).to_be_visible()
    assert page.locator("#found-passage").evaluate("el => el.textContent") == hit["text"]
    expect(page.locator("#reader-citation > p")).to_have_text(hit["citation"])
    page.locator("#reader-citation").get_by_role("button", name="Копировать цитату", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == hit["text"] + "\n\n" + hit["citation"]
    return hit


def copy_literal(page: Page, value: str) -> None:
    result = page.locator("#reader-text").evaluate(
        """(el, literal) => {
          const start = el.textContent.indexOf(literal);
          if (start < 0 || el.childNodes.length !== 1) return false;
          const range = document.createRange();
          range.setStart(el.firstChild, start); range.setEnd(el.firstChild, start + literal.length);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          return range.toString() === literal && selection.toString() === literal;
        }""",
        value,
    )
    assert result, "Could not select the exact literal in actual reader DOM"
    page.keyboard.press("ControlOrMeta+C")
    assert page.evaluate("navigator.clipboard.readText()") == value, "Browser copying changed literal whitespace"


def check_mobile(
    page: Page, base: str, section: dict[str, Any], sample: str, artifacts: Path, name: str
) -> dict[str, Any]:
    page.set_viewport_size({"width": 390, "height": 844})
    actual = read_section(page, base, section)
    assert sample in actual
    layout = page.locator("#reader-text").evaluate(
        """el => ({pageWidth:document.documentElement.scrollWidth, viewport:window.innerWidth,
          readerWidth:el.clientWidth, readerScrollWidth:el.scrollWidth,
          whiteSpace:getComputedStyle(el).whiteSpace, overflowX:getComputedStyle(el).overflowX})"""
    )
    assert layout["pageWidth"] <= layout["viewport"], f"Mobile page overflow: {layout}"
    assert layout["readerScrollWidth"] <= layout["readerWidth"] or layout["overflowX"] in {"auto", "scroll"}, (
        f"Mobile text overflows without local scrolling: {layout}"
    )
    assert layout["whiteSpace"] in {"pre-wrap", "break-spaces", "pre"}
    page.locator("#reader-text").evaluate(
        """(el, sample) => {
          const at = el.textContent.indexOf(sample); const range = document.createRange();
          range.setStart(el.firstChild, at); range.setEnd(el.firstChild, at + Math.min(sample.length, 100));
          window.scrollBy(0, range.getBoundingClientRect().top - 220);
        }""",
        sample,
    )
    page.screenshot(path=str(artifacts / name))
    return {"section_id": section["id"], "section": section["title"], "literal_sha256": sha(sample), **layout}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--source-bundle", type=Path, default=Path("data/wikimedia/wikibooks-regex-266423"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/wikibooks-browser"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((args.source_bundle / "provenance.json").read_text())
    raw = (args.source_bundle / "source-revision-266423.wikitext").read_text()
    html = (args.source_bundle / "rendered-revision-266423.html").read_bytes()
    assert sha(raw) == provenance["source_wikitext_sha256"]
    assert sha(html) == provenance["rendered_html_sha256"]
    examples = [canonical(m.group(1)) for m in re.finditer(r"<source\b[^>]*>(.*?)</source\s*>", raw, re.I | re.S)]
    assert len(examples) == 27
    rendered = BeautifulSoup(html, "html.parser").select_one(".mw-parser-output")
    assert rendered is not None
    tables: list[dict[str, Any]] = []
    for table in rendered.find_all("table"):
        rows, cells = [], []
        for row in table.find_all("tr"):
            values = []
            for cell in row.find_all(["th", "td"], recursive=False):
                literal = dom_text(cell)
                cells.append(canonical(literal))
                values.extend([literal] + [""] * (int(str(cell.get("colspan", "1"))) - 1))
            rows.append("\t".join(values))
        tables.append({"text": canonical("\n".join(rows)), "cells": cells, "rows": len(rows)})
    assert len(tables) == 4 and sum(t["rows"] for t in tables) == 44 and sum(len(t["cells"]) for t in tables) == 172
    report: dict[str, Any] = {"result": "running", "book_id": BOOK_ID, "library_writes": 0, "checks": []}
    errors: list[str] = []
    writes: list[dict[str, str]] = []
    started = time.monotonic()
    expect.set_options(timeout=30000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, permissions=["clipboard-read", "clipboard-write"]
        )
        context.on(
            "request",
            lambda request: (
                writes.append({"method": request.method, "url": request.url})
                if request.method not in {"GET", "HEAD"}
                else None
            ),
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            response = page.request.get(f"{base}/api/books/{BOOK_ID}")
            assert response.status == 200
            book = response.json()
            assert book["artifact_sha256"] == ARTIFACT_SHA256
            assert book["source_metadata"]["coverage"] == "partial"
            assert book["source_metadata"]["work_identity_status"] == "provisional"
            assert book["source_metadata"]["book_completion"] == "unverified"
            sections = book["sections"]
            assert len(sections) == 25
            queries = []
            for index, (query, scenario) in enumerate(QUERIES):
                hit = scoped_search(page, base, query, index == 0)
                section = next(s for s in sections if s["id"] == hit["section_id"])
                assert section["text"][hit["start"] : hit["end"]] == hit["text"]
                assert page.locator("#reader-text").evaluate("el => el.textContent") == section["text"]
                assert all(book[k] in hit["citation"] for k in ("author", "title", "edition"))
                check_source(page, book)
                queries.append(
                    {
                        "query": query,
                        "scenario": scenario,
                        "passage_id": hit["id"],
                        "section": section["title"],
                        "quote_sha256": sha(hit["text"]),
                        "citation": hit["citation"],
                        "verified_mark_and_clipboard": True,
                    }
                )
                if index == 0:
                    page.locator(".source-details").screenshot(
                        path=str(args.artifacts / "wikibooks-credits-desktop.png")
                    )
                    page.locator(".source-details > summary").click()
                    page.locator("#found-passage").screenshot(path=str(args.artifacts / "wikibooks-exact-quote.png"))
            report["queries"] = queries
            report["checks"].append("three_real_russian_searches_exact_quotes_citations_clipboard_and_source_credits")
            dom_sections = []
            for section in sections:
                dom_sections.append(read_section(page, base, section))
                expect(page.locator("#reader-toc button")).to_have_count(25)
                if section["ordinal"] == 0:
                    assert section["section_author"] == "Book RAG" and section["section_role"] == "editorial"
                    expect(page.locator("#reader-contributor")).to_contain_text("Автор раздела: Book RAG")
                    expect(page.locator("#reader-contributor")).to_contain_text("Редакционный раздел")
                else:
                    assert section["section_author"] == book["author"] and section["section_role"] != "editorial"
                    expect(page.locator("#reader-contributor")).not_to_contain_text("Book RAG")
            joined = "\n\n".join(dom_sections)
            cursor = 0
            for example in examples:
                at = joined.find(example, cursor)
                assert at >= 0, f"Lost source example in reader DOM: {sha(example)}"
                cursor = at + len(example)
            for index, table in enumerate(tables):
                assert sha(table["text"]) == provenance["projection_audit"]["tables"][index]["text_sha256"]
                assert table["text"] in joined
                assert all(cell in joined for cell in table["cells"])
            assert all(canonical(c["projected_text"]) in joined for c in provenance["projection_audit"]["inline_code"])
            for heading in provenance["projection_audit"]["headings"]:
                assert canonical(heading["text"]).strip() in "\n".join(s["title"] for s in sections)
            report["reader"] = {
                "sections": len(sections),
                "source_headings": 24,
                "source_examples_ordered_verbatim": 27,
                "inline_code_values": 333,
                "tables": 4,
                "table_rows": 44,
                "table_cells": 172,
                "canonical_dom_sha256": sha(joined),
                "editorial_packaging_attribution": "Book RAG",
                "all_section_text_matches_api_exactly": True,
            }
            report["checks"].append("all_reader_sections_source_examples_inline_code_and_table_content")
            copied = []
            for example in (examples[2], examples[3], examples[25]):
                section = next(s for s in sections if example in s["text"])
                read_section(page, base, section)
                # Native Chromium selection can omit terminal block LF from
                # rendered selection text. Probe that separately; copy the
                # interior literal including all meaningful internal spacing.
                selection_probe = page.locator("#reader-text").evaluate(
                    """(el, literal) => {
                      const at = el.textContent.indexOf(literal), range = document.createRange();
                      range.setStart(el.firstChild, at); range.setEnd(el.firstChild, at + literal.length);
                      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
                      return {rangeExact:range.toString() === literal, selectionExact:selection.toString() === literal,
                        sourceLength:literal.length, renderedSelectionLength:selection.toString().length};
                    }""",
                    example,
                )
                assert selection_probe["rangeExact"]
                interior = example.strip("\n")
                copy_literal(page, interior)
                copied.append(
                    {
                        "section_id": section["id"],
                        "literal_sha256": sha(interior),
                        "characters": len(interior),
                        "internal_whitespace_exact": True,
                        "outer_source_delimiter_lf_selected": False,
                        "full_source_block_native_selection_probe": selection_probe,
                    }
                )
            report["literal_clipboard"] = copied
            code = max(examples, key=lambda item: max(map(len, item.splitlines())))
            code_section = next(s for s in sections if code in s["text"])
            table = max(tables, key=lambda item: max(map(len, item["text"].splitlines())))
            table_section = next(s for s in sections if table["text"] in s["text"])
            report["mobile"] = [
                check_mobile(page, base, code_section, code, args.artifacts, "wikibooks-mobile-code.png"),
                check_mobile(page, base, table_section, table["text"], args.artifacts, "wikibooks-mobile-table.png"),
            ]
            page.set_viewport_size({"width": 390, "height": 844})
            check_source(page, book)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator(".source-details").screenshot(path=str(args.artifacts / "wikibooks-credits-mobile.png"))
            download_response = page.request.get(base + book["artifact_url"])
            assert download_response.status == 200 and sha(download_response.body()) == ARTIFACT_SHA256
            with page.expect_download() as downloaded:
                page.locator("#reader-source").get_by_role(
                    "link", name="Скачать зафиксированный файл", exact=True
                ).click()
            download = downloaded.value
            path = download.path()
            assert path is not None and sha(Path(path).read_bytes()) == ARTIFACT_SHA256
            report["download"] = {
                "artifact_sha256": ARTIFACT_SHA256,
                "api_bytes": len(download_response.body()),
                "ui_filename": download.suggested_filename,
                "api_and_actual_browser_download_verified": True,
            }
            report["checks"].append("mobile_code_tables_partial_credits_and_pinned_artifact_download")
            assert not errors and not writes, {"errors": errors, "writes": writes}
            report["stats_snapshot"] = page.request.get(base + "/api/stats").json()
            report["result"] = "passed"
        except BaseException as error:
            report["result"] = "failed"
            report["error"] = f"{type(error).__name__}: {error}"
            page.screenshot(path=str(args.artifacts / "failure-viewport.png"))
            raise
        finally:
            report.update(
                elapsed_seconds=round(time.monotonic() - started, 3),
                browser_errors=errors,
                browser_write_requests=writes,
            )
            (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
