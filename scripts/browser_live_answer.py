"""Run two known development questions through the real browser and local model.

Read-only with respect to books/notes. Raw responses/screenshots stay outside Git.
This is a narrow regression smoke, not a representative quality measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path)
    arguments = parser.parse_args()
    if arguments.artifacts is None:
        arguments.artifacts = Path("artifacts") / ("live-answer-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    parsed = urlsplit(arguments.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Select an existing local development server explicitly")
    if arguments.artifacts.exists() and any(arguments.artifacts.iterdir()):
        parser.error("Use a new artifact directory to preserve earlier successes and failures")
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "closed_final_used": False,
        "quality_benchmark": False,
        "cases": [],
    }
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(arguments.base_url + "/?panel=answer&scope=pg11-full", wait_until="networkidle")
        expect(page.locator("#run-answer")).to_be_enabled(timeout=30_000)
        for name, question, status in [
            ("watch", "What does the White Rabbit take out of its waistcoat-pocket?", "answered"),
            ("wrong-topic", "What does Alice explain about quantum encryption?", "abstained"),
        ]:
            page.locator("#answer-query").fill(question)
            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/ask", timeout=100_000
            ) as pending:
                page.locator("#run-answer").click()
            response = pending.value
            assert response.ok, response.text()
            result = response.json()
            raw = arguments.artifacts / f"{name}.response.json"
            raw.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            observed = {
                "name": name,
                "expected_status": status,
                "observed_status": result["status"],
                "elapsed_ms": result["elapsed_ms"],
                "source_count": len(result["sources"]),
                "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            }
            report["cases"].append(observed)
            (arguments.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            assert result["status"] == status, observed
            if status == "answered":
                assert "watch" in " ".join(claim["text"] for claim in result["claims"]).lower()
                expect(page.locator(".answer-claim").first).to_be_visible()
                support = result["claims"][0]["supporting_quotes"][0]
                expect(page.locator(".answer-support-quote").first).to_have_text(support["quote"])
                source = next(item for item in result["sources"] if item["source_id"] == support["source_id"])
                assert source["verified"] and support["quote"] in source["text"]
                page.screenshot(path=str(arguments.artifacts / "watch.png"), full_page=True)
                page.locator(".answer-source-chip").first.click()
                page.locator("#answer-source-" + support["source_id"]).get_by_role(
                    "button", name="Открыть в контексте"
                ).click()
                expect(page.locator("#found-passage")).to_have_text(source["text"])
                page.screenshot(path=str(arguments.artifacts / "reader.png"), full_page=True)
                observed["reader_exact"] = True
                page.locator("#reader-back").click()
                expect(page.locator("#answer-panel")).to_be_visible()
            else:
                expect(page.locator(".answer-claim")).to_have_count(0)
                expect(page.locator(".answer-sources article").first).to_be_visible()
                page.screenshot(path=str(arguments.artifacts / "abstention.png"), full_page=True)
            print(json.dumps(observed), flush=True)
        browser.close()
    report["javascript_errors"] = errors
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["passed"] = not errors
    (arguments.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert not errors, errors


if __name__ == "__main__":
    main()
