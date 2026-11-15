"""Actual GET-only scoped-batch browser journeys on the running local app.

These named books/queries are new OPEN probes, not heldout/final quality cases.
No answers, query planner, acquisition, saved-note writes or server changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

from book_rag.experimental_ivfpq import file_sha256
from book_rag.semantic_batches import batch_source_state
from book_rag.semantic_pilot import ReadOnlyLibrary

PROBES = [
    {
        "id": "open-kant-ru-to-de",
        "book_id": "pg30821-full",
        "query_language": "ru",
        "source_language": "de",
        "query": "Почему человек остаётся несовершеннолетним и не решается пользоваться собственным рассудком?",
    },
    {
        "id": "open-kafka-judgment-de",
        "book_id": "pg21593-full",
        "query_language": "de",
        "source_language": "de",
        "query": "Ein Vater verurteilt seinen Sohn zum Tod durch Ertrinken.",
    },
    {
        "id": "open-voltaire-journey-fr",
        "book_id": "pg4718-full",
        "query_language": "fr",
        "source_language": "fr",
        "query": "Un voyageur rencontre la cruauté et le fanatisme dans les différents pays qu'il visite.",
    },
]


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def artifact_state(source: ReadOnlyLibrary, book_ids: list[str]) -> dict[str, dict[str, Any]]:
    with source.connect() as connection:
        rows = connection.execute(
            "SELECT id,local_path,artifact_sha256 FROM books WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(book_ids),),
        ).fetchall()
    result = {}
    for row in rows:
        path = Path(row["local_path"])
        actual = file_sha256(path)
        assert actual == row["artifact_sha256"], "Original acquired artifact hash is not verified"
        result[row["id"]] = {"sha256": actual, "bytes": path.stat().st_size}
    assert set(result) == set(book_ids)
    return result


def exact_reader(page: Any, expected: dict[str, Any]) -> dict[str, Any]:
    mark = page.locator("#found-passage")
    expect(mark).to_have_text(expected["text"], timeout=30_000)
    assert mark.text_content() == expected["text"], "Reader changed literal quote characters"
    text = page.locator("#reader-text").text_content()
    assert text and text[expected["start"] : expected["end"]] == expected["text"]
    expect(page.locator("#reader-citation")).to_contain_text(expected["citation"])
    assert parse_qs(urlsplit(page.url).query)["book"] == [expected["book_id"]]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    return {
        "book_id": expected["book_id"],
        "passage_id": expected["id"],
        "section_id": expected["section_id"],
        "start": expected["start"],
        "end": expected["end"],
        "text_sha256": sha_text(expected["text"]),
        "citation_sha256": sha_text(expected["citation"]),
        "exact_highlight": True,
        "exact_unicode_section_slice": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--source", type=Path, default=Path("data/full"))
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--public-receipt", type=Path)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if urlsplit(base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Select the running local application")
    output = args.artifacts or Path("artifacts") / (
        "live-semantic-batch-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    if args.public_receipt and args.public_receipt.exists():
        parser.error("Preserve prior receipts; select a new public receipt path")
    output.mkdir(parents=True, exist_ok=False)
    source = ReadOnlyLibrary(args.source)
    ids = [case["book_id"] for case in PROBES]
    before = batch_source_state(source, ids)
    original_artifacts = artifact_state(source, ids)
    errors: list[str] = []
    writes: list[dict[str, str]] = []
    asks: list[str] = []
    searches: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "kind": "actual-live-explicit-book-semantic-batch-browser-probes",
        "started_at": datetime.now(UTC).isoformat(),
        "base_url": base,
        "probe_status": "new_open_probes_not_final_quality",
        "closed_final_used": False,
        "real_api": True,
        "http_response_mocking": False,
        "get_only": True,
        "answer_generation_requested": False,
        "query_planning_requested": False,
        "cases": [],
        "latency_is_not_a_paired_benchmark": True,
        "latency_caveat": "Root may run bulk MiniLM and a separate local4B evaluation concurrently.",
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.set_default_timeout(30_000)

            def protect(route: Any) -> None:
                request = route.request
                if request.method not in {"GET", "HEAD"}:
                    writes.append({"method": request.method, "path": urlsplit(request.url).path})
                    route.abort()
                else:
                    route.continue_()

            # This blocks writes only. Every GET uses the actual local API response.
            context.route("**/*", protect)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def observed(request: Any) -> None:
                endpoint = urlsplit(request.url)
                if endpoint.path == "/api/ask":
                    asks.append(request.method)
                if endpoint.path == "/api/search":
                    query = parse_qs(endpoint.query)
                    searches.append(
                        {
                            "q": query.get("q"),
                            "book_id": query.get("book_id"),
                            "mode": query.get("mode"),
                            "method": request.method,
                        }
                    )

            page.on("request", observed)
            for case in PROBES:
                scope = case["book_id"]
                page.goto(
                    base + "/?" + urlencode({"panel": "answer", "scope": scope}),
                    wait_until="networkidle",
                    timeout=60_000,
                )
                expect(page.locator("#run-answer")).to_be_enabled()
                expect(page.locator("#book-filter")).to_have_value(scope)
                page.locator("#fragments-tab").click()
                page.locator('[data-mode="semantic"]').click()
                page.locator("#query").fill(case["query"])
                began = time.perf_counter()
                with page.expect_response(
                    lambda response, selected_scope=scope: (
                        urlsplit(response.url).path == "/api/search"
                        and parse_qs(urlsplit(response.url).query).get("book_id") == [selected_scope]
                        and parse_qs(urlsplit(response.url).query).get("mode") == ["semantic"]
                    ),
                    timeout=90_000,
                ) as pending:
                    page.locator('#search-form [type="submit"]').click()
                response = pending.value
                assert response.ok, f"Search failed:HTTP{response.status}"
                data = response.json()
                wall_ms = (time.perf_counter() - began) * 1000
                assert data["retrieval_strategy"] == "explicit-book-batch-IVFPQ-RRF-v1", data
                assert data["mode"] == "semantic" and data["query"] == case["query"]
                coverage = data["semantic_coverage"]
                assert coverage["scope"] == "book-batch-overlay" and coverage["active"] is True
                assert coverage["scope_book_id"] == scope and coverage["requested_book_covered"] is True
                assert data["results"]
                assert all(
                    row["book_id"] == scope
                    and row["language"] == case["source_language"]
                    and row["verified"] is True
                    and row["semantic_score_approximate"] is True
                    for row in data["results"]
                )
                cards = page.locator("#fragments-panel .passage-card")
                expect(cards).to_have_count(len(data["results"]))
                badge = page.locator("#semantic-coverage")
                expect(badge).to_be_visible()
                expect(badge).to_contain_text("для выбранной книги")
                expect(badge).to_contain_text("приближённо")
                assert "из 1 изданий" not in (badge.text_content() or "")
                expected = data["results"][0]
                assert cards.first.locator("blockquote").text_content() == expected["text"]
                assert cards.first.locator(".passage-citation").text_content() == expected["citation"]
                current = page.request.get(base + f"/api/passages/{expected['id']}")
                assert current.ok
                canonical = current.json()
                assert canonical["verified"] and canonical["text"] == expected["text"]
                assert canonical["citation"] == expected["citation"]
                book_response = page.request.get(base + f"/api/books/{scope}")
                assert book_response.ok
                book = book_response.json()
                chapter = next(section for section in book["sections"] if section["id"] == expected["section_id"])
                assert chapter["text"][expected["start"] : expected["end"]] == expected["text"]
                (output / (case["id"] + ".response.json")).write_text(json.dumps(data, ensure_ascii=False, indent=2))
                page.screenshot(path=str(output / (case["id"] + "-search.png")))
                cards.first.get_by_role("button", name="Открыть в контексте").click()
                reader = exact_reader(page, expected)
                page.screenshot(path=str(output / (case["id"] + "-reader.png")))
                page.locator("#reader-back").click()
                expect(page.locator("#search-view")).to_be_visible()
                expect(page.locator("#book-filter")).to_have_value(scope)
                expect(page.locator("#query")).to_have_value(case["query"])
                expect(page.locator("#fragments-panel .passage-card")).to_have_count(len(data["results"]))
                assert page.locator("#fragments-panel blockquote").first.text_content() == expected["text"]
                item = dict(case) | {
                    "title": book["title"],
                    "author": book["author"],
                    "result_count": len(data["results"]),
                    "retrieval_strategy": data["retrieval_strategy"],
                    "coverage": coverage,
                    "server_elapsed_ms": data["elapsed_ms"],
                    "browser_search_wall_ms": wall_ms,
                    "reader": reader,
                    "back_preserved_scope": True,
                    "honest_approximate_scope_badge": True,
                }
                if case is PROBES[-1]:
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.screenshot(path=str(output / "mobile-scoped-search.png"))
                    page.locator("#fragments-panel .passage-card").first.get_by_role(
                        "button", name="Открыть в контексте"
                    ).click()
                    exact_reader(page, expected)
                    page.screenshot(path=str(output / "mobile-exact-reader.png"))
                    page.reload(wait_until="networkidle", timeout=60_000)
                    exact_reader(page, expected)
                    item["mobile_reader_exact"] = True
                    item["mobile_reader_reload_exact"] = True
                report["cases"].append(item)
                assert not writes and not asks, (writes, asks)
            browser.close()
        after = batch_source_state(source, ids)
        artifacts_after = artifact_state(source, ids)
        assert after == before and artifacts_after == original_artifacts, "Selected original bytes changed"
        assert not errors and not writes and not asks
        report.update(
            passed=True,
            selected_original_source_unchanged=True,
            original_artifacts_unchanged=True,
            original_artifact_hashes=original_artifacts,
            source_state_sha256=sha_text(json.dumps(before, sort_keys=True)),
        )
    except Exception as error:
        report.update(passed=False, failure=str(error))
        raise
    finally:
        report.update(
            javascript_errors=errors,
            non_get_attempts=writes,
            ask_requests=asks,
            semantic_search_requests=searches,
            completed_at=datetime.now(UTC).isoformat(),
        )
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.public_receipt:
            public = {key: value for key, value in report.items() if key not in {"failure"}}
            public["raw_report"] = {"path": str(output / "report.json"), "sha256": file_sha256(output / "report.json")}
            args.public_receipt.parent.mkdir(parents=True, exist_ok=True)
            args.public_receipt.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "passed": report.get("passed"),
                    "cases": len(report["cases"]),
                    "output": str(output),
                    "javascript_errors": len(errors),
                    "non_get_attempts": len(writes),
                    "ask_requests": len(asks),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
