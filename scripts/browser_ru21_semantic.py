"""GET-only exact-reader checks for the isolated RU21 semantic pilot results."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

from book_rag.semantic_pilot import ReadOnlyLibrary, verify_owned_subset


def readonly_server(directory: str, connection: Any) -> None:
    import uvicorn

    from book_rag import api

    with patch.object(api, "Library", ReadOnlyLibrary):
        app = api.create_app(Path(directory))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    connection.send(sock.getsockname()[1])
    connection.close()
    uvicorn.Server(uvicorn.Config(app, log_level="error")).run(sockets=[sock])


def check(page: Any, base: str, source: ReadOnlyLibrary, record: dict[str, Any], label: str) -> dict[str, Any]:
    from playwright.sync_api import expect

    passage = source.passage(record["id"])
    assert passage and passage["verified"]
    response = page.request.get(base + f"/api/passages/{record['id']}")
    assert response.ok
    live = response.json()
    assert live["verified"] and live["text"] == passage["text"] and live["citation"] == passage["citation"]
    page.goto(base + passage["reader_url"], wait_until="networkidle")
    expect(page.locator("#found-passage")).to_have_text(passage["text"])
    section_text = page.locator("#reader-text").text_content()
    assert section_text and section_text[passage["start"] : passage["end"]] == passage["text"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    return {
        "label": label,
        "passage_id": passage["id"],
        "book_id": passage["book_id"],
        "reader_url": passage["reader_url"],
        "verified_exact_canonical_highlight": True,
    }


def main() -> None:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=Path("data/semantic-pilots/ru21-v1"))
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/ru21-semantic-reader"))
    args = parser.parse_args()
    pilot = args.pilot.resolve()
    verify_owned_subset(pilot / "canonical")
    result = json.loads((pilot / "report.json").read_text())
    args.artifacts.mkdir(parents=True, exist_ok=True)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    server = context.Process(target=readonly_server, args=(str(pilot / "cc0"), child))
    server.start()
    child.close()
    if not parent.poll(20):
        server.terminate()
        server.join(timeout=2)
        raise TimeoutError("Owned read-only CC0 reader server did not start")
    cc0_base = f"http://127.0.0.1:{parent.recv()}"
    errors: list[str] = []
    writes: list[str] = []
    records = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: writes.append(request.method) if request.method not in ("GET", "HEAD") else None,
            )
            source = ReadOnlyLibrary(pilot / "canonical")
            for case in result["resumed_phase"]["cases"]:
                candidates = case["semantic_top"]
                records.append(check(page, args.base.rstrip("/"), source, candidates[0], case["id"] + "-top1"))
                rank = case["semantic_anchor_rank"]
                if rank and rank <= len(candidates) and rank != 1:
                    records.append(
                        check(page, args.base.rstrip("/"), source, candidates[rank - 1], case["id"] + "-anchor")
                    )
            page.screenshot(path=str(args.artifacts / "ru21-actual-reader-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            case = result["resumed_phase"]["cases"][0]
            records.append(check(page, args.base.rstrip("/"), source, case["semantic_top"][0], "ru21-mobile"))
            page.screenshot(path=str(args.artifacts / "ru21-actual-reader-mobile.png"))
            cc0_source = ReadOnlyLibrary(pilot / "cc0")
            for case in result["cc0_phase"]["cases"]:
                records.append(check(page, cc0_base, cc0_source, case["semantic_top"][0], case["id"]))
            page.screenshot(path=str(args.artifacts / "cc0-actual-encoder-reader-mobile.png"))
            assert not errors and not writes, (errors, writes)
            browser.close()
        verify_owned_subset(pilot / "canonical")
        report = {
            "schema_version": 1,
            "get_only": True,
            "javascript_errors": errors,
            "non_get_requests": writes,
            "readers": records,
            "production_semantic_api_changed": False,
            "cc0_server_read_only": True,
            "original_selected_source_unchanged": True,
        }
        (pilot / "reader-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"readers": len(records), "get_only": True, "javascript_errors": 0}))
    finally:
        server.terminate()
        server.join(timeout=2)
        parent.close()


if __name__ == "__main__":
    main()
