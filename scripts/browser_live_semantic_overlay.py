"""Exercise actual scoped Russian semantic search and exact readers on the main app.

Open development cases only. GET traffic, real local embeddings/native workers,
no model answers and no assertions about factual correctness of generated prose.
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
    args = parser.parse_args()
    if urlsplit(args.base_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Select the running local application")
    output = args.artifacts or Path("artifacts") / (
        "live-semantic-overlay-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output.mkdir(parents=True, exist_ok=False)
    cases = [
        case
        for case in json.loads(Path("evals/rag-dev-v1.json").read_text())["cases"]
        if case["id"]
        in {
            "ru-chameleon-owner",
            "ru-lefty-warning",
            "ru-land-grave",
        }
    ]
    errors: list[str] = []
    writes: list[str] = []
    report: dict = {"closed_final_used": False, "real_search": True, "cases": []}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: writes.append(request.url) if request.method != "GET" else None)
            for case in cases:
                scope = case["book_ids"][0]
                page.goto(
                    args.base_url + "/?" + urlencode({"panel": "answer", "scope": scope}), wait_until="networkidle"
                )
                expect(page.locator("#run-answer")).to_be_enabled()
                expect(page.locator("#book-filter")).to_have_value(scope)
                page.locator("#fragments-tab").click()
                page.locator('[data-mode="semantic"]').click()
                query = case["question"].translate(str.maketrans(dict.fromkeys('"«»“”„', " ")))
                page.locator("#query").fill(query)
                with page.expect_response(
                    lambda response: urlsplit(response.url).path == "/api/search", timeout=60_000
                ) as pending:
                    page.locator('#search-form [type="submit"]').click()
                response = pending.value
                assert response.ok, response.text()
                result = response.json()
                assert result["retrieval_strategy"] == "scoped-IVFPQ-approximate-RRF-v1", result
                assert result["semantic_coverage"]["scope"] == "ru21-scoped-overlay"
                assert result["results"] and all(item["book_id"] == scope for item in result["results"])
                assert all(item["verified"] for item in result["results"])
                anchor = case["evidence"][0]
                rank = next(index for index, item in enumerate(result["results"]) if anchor["quote"] in item["text"])
                expect(page.locator("#semantic-coverage")).to_contain_text("на русском: 21 из 21")
                cards = page.locator("#fragments-panel .passage-card")
                expect(cards).to_have_count(len(result["results"]))
                expected = result["results"][rank]
                expect(cards.nth(rank).locator("blockquote")).to_have_text(expected["text"])
                (output / (case["id"] + ".response.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2))
                page.screenshot(path=str(output / (case["id"] + "-search.png")))
                cards.nth(rank).get_by_role("button", name="Открыть в контексте").click()
                expect(page.locator("#found-passage")).to_have_text(expected["text"])
                actual = page.locator("#reader-text").text_content()
                assert actual and actual[expected["start"] : expected["end"]] == expected["text"]
                page.screenshot(path=str(output / (case["id"] + "-reader.png")))
                page.locator("#reader-back").click()
                expect(page.locator("#fragments-panel .passage-card")).to_have_count(len(result["results"]))
                report["cases"].append(
                    {
                        "id": case["id"],
                        "query": query,
                        "book_id": scope,
                        "anchor_rank": rank + 1,
                        "elapsed_ms": result["elapsed_ms"],
                        "reader_exact": True,
                        "retrieval_strategy": result["retrieval_strategy"],
                    }
                )

            page.goto(args.base_url + "/?panel=answer", wait_until="networkidle")
            page.locator("#fragments-tab").click()
            page.locator("#language-filter").select_option("ru")
            page.locator('[data-mode="semantic"]').click()
            page.locator("#query").fill("умирающий мастер предупреждает царя о чистке оружия")
            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/search", timeout=60_000
            ) as pending:
                page.locator('#search-form [type="submit"]').click()
            language_result = pending.value.json()
            assert language_result["semantic_coverage"]["scope_searchable_books"] == 21
            assert language_result["results"] and all(item["language"] == "ru" for item in language_result["results"])
            expect(page.locator("#semantic-coverage")).to_contain_text("на русском: 21 из 21")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / "ru-language-mobile.png"), full_page=True)
            report["cases"].append({"id": "russian-language-scope-mobile", "passed": True})
            assert not errors and not writes, (errors, writes)
            browser.close()
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = str(error)
        raise
    finally:
        report["javascript_errors"] = errors
        report["non_get_requests"] = writes
        report["completed_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
