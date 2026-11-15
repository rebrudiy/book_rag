"""Exercise book descriptions, source-language filters and reading on a live local server.

Uses real catalogue embeddings and canonical reader responses. No generation calls,
book mutations or saved notes. This is an open development smoke, not a quality score.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path)
    arguments = parser.parse_args()
    parsed = urlsplit(arguments.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Select an existing local server")
    output = arguments.artifacts or Path("artifacts") / (
        "book-discovery-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    if output.exists() and any(output.iterdir()):
        parser.error("Use a new artifact directory to preserve previous observations")
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    mutations: list[str] = []
    report: dict = {
        "started_at": datetime.now(UTC).isoformat(),
        "real_metadata_index": True,
        "closed_final_used": False,
        "quality_benchmark": False,
        "cases": [],
    }

    def save() -> None:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: mutations.append(request.url) if request.method != "GET" else None)
            page.goto(arguments.base_url + "/", wait_until="networkidle")
            page.locator("#open-book-discovery").click()
            expect(page.locator("#library-search-mode")).to_have_value("description")
            expect(page.locator("#library-books")).to_contain_text("Какую книгу вы хотите найти?")
            expect(page.locator("#library-status")).to_be_disabled()
            report["cases"].append({"name": "homepage_description_entry", "passed": True})

            for name, query, target in [
                ("alice", "a girl follows a rabbit into a strange underground world", "pg11-full"),
                ("darwin", "происхождение видов и естественный отбор", "pg1228-full"),
            ]:
                with page.expect_response(
                    lambda response: urlsplit(response.url).path == "/api/discover/books", timeout=60_000
                ) as pending:
                    page.locator("#library-query").fill(query)
                response = pending.value
                assert response.ok, response.text()
                result = response.json()
                (output / f"{name}.response.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                assert result["kind"] == "metadata_semantic" and result["status"] == "ready", result
                books = result["books"]
                assert target in [book["id"] for book in books], [book["id"] for book in books]
                card = page.locator(f'#library-books [data-book-id="{target}"]')
                expect(card).to_be_visible(timeout=30_000)
                expect(page.locator("#book-discovery-context")).to_contain_text("метаданным")
                expect(page.locator("#library-books .verified-label")).to_have_count(0)
                page.screenshot(path=str(output / f"{name}.png"), full_page=True)
                report["cases"].append(
                    {
                        "name": name,
                        "query": query,
                        "target": target,
                        "rank": [book["id"] for book in books].index(target) + 1,
                        "coverage": result["coverage"],
                        "passed": True,
                    }
                )
                save()
                if name == "alice":
                    card.get_by_role("button", name="Читать").click()
                    expect(page.locator("#reader-sidebar-title")).to_contain_text("Alice")
                    expect(page.locator("#reader-text")).not_to_be_empty()
                    page.screenshot(path=str(output / "alice-reader.png"), full_page=True)
                    page.locator("#reader-back").click()
                    expect(page.locator("#library-search-mode")).to_have_value("description")
                    expect(page.locator("#library-query")).to_have_value(query)
                    expect(page.locator(f'#library-books [data-book-id="{target}"]')).to_be_visible()
                    report["cases"].append({"name": "description_reader_return", "passed": True})

            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/discover/books", timeout=60_000
            ) as pending:
                page.locator("#library-language").select_option("en")
            filtered = pending.value.json()
            assert filtered["books"] and all(book["language"] == "en" for book in filtered["books"])
            expect(page.locator("#library-count")).to_contain_text("Ближайшие совпадения")
            expect(page.locator('#library-books [data-book-id="pg1228-full"]')).to_be_visible()
            deep_link = page.url
            assert "book_mode=description" in deep_link and "language=en" in deep_link
            page.reload(wait_until="networkidle")
            expect(page.locator("#library-query")).to_have_value("происхождение видов и естественный отбор")
            expect(page.locator("#library-language")).to_have_value("en")
            expect(page.locator('#library-books [data-book-id="pg1228-full"]')).to_be_visible(timeout=30_000)
            report["cases"].append({"name": "source_language_and_reload", "url": deep_link, "passed": True})

            for width in [390, 320]:
                page.set_viewport_size({"width": width, "height": 844})
                page.goto(
                    arguments.base_url
                    + "/?"
                    + urlencode(
                        {
                            "view": "library",
                            "book_mode": "description",
                            "book_q": "естественный отбор",
                            "language": "en",
                        }
                    ),
                    wait_until="networkidle",
                )
                expect(page.locator("#library-books .book-card").first).to_be_visible(timeout=30_000)
                dimensions = page.evaluate("({scroll: document.documentElement.scrollWidth, inner: innerWidth})")
                assert dimensions["scroll"] <= dimensions["inner"], dimensions
                page.screenshot(path=str(output / f"mobile-{width}.png"), full_page=True)
                report["cases"].append({"name": f"mobile_{width}", "passed": True, **dimensions})

            assert not errors, errors
            assert not mutations, mutations
            browser.close()
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = str(error)
        raise
    finally:
        report["javascript_errors"] = errors
        report["non_get_requests"] = mutations
        report["completed_at"] = datetime.now(UTC).isoformat()
        save()
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
