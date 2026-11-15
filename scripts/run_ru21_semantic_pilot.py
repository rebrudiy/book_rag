"""Actual owned RU21: offline Torch IPC -> compressed FAISS, never production.

Requires an idle accelerator; all originals use read-only adapters. Source
selection, current attribution and stable passage/section IDs are preserved.
Only the three public RU-original RAG dev cases and own CC0 examples are queried.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import resource
import shutil
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from book_rag.encoder_ipc import EncoderService, rpc
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import build_incremental, query_incremental
from book_rag.library import Library
from book_rag.russian_retrieval import scoped_russian_fallback
from book_rag.semantic_pilot import ReadOnlyLibrary, create_owned_subset, manifest_windows, verify_owned_subset

ROOT = Path(__file__).resolve().parents[1]
DEV_IDS = ("ru-chameleon-owner", "ru-lefty-warning", "ru-land-grave")
RESERVE = int(4.5 * 1024**3)
MAX_DISK = 250 * 1024**2
MAX_RSS = 2 * 1024**3


def public_cases() -> list[dict[str, Any]]:
    fixture = json.loads((ROOT / "evals/rag-dev-v1.json").read_text())
    selected = [case for case in fixture["cases"] if case["id"] in DEV_IDS]
    if len(selected) != 3:
        raise ValueError("Expected exactly three declared public RU-original dev anchors")
    return selected


def cc0_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "cc0-blue-chest",
            "question": "Где смотритель Ладан хранит серебряный ключ от синего сундука?",
            "book_ids": ["cc0-island"],
            "quote": "Серебряный ключ от синего сундука смотритель Ладан хранит в зелёной чашке.",
            "expected_answerability": True,
        },
        {
            "id": "cc0-observatory",
            "question": "Что наблюдательница Мирта записывает после каждого сигнала маяка?",
            "book_ids": ["cc0-observatory"],
            "quote": "После каждого сигнала маяка Мирта записывает число медных колец.",
            "expected_answerability": True,
        },
        {
            "id": "cc0-wrong-book",
            "question": "Где смотритель Ладан хранит серебряный ключ от синего сундука?",
            "book_ids": ["cc0-observatory"],
            "quote": None,
            "expected_answerability": False,
            "scope_rationale": (
                "Own complete CC0 observatory text contains no Ladan/chest/key event; wrong-book scope only."
            ),
        },
    ]


def create_cc0(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    library = Library(directory)
    texts = {
        "cc0-island": "Архив острова Сивер\n\n"
        "Серебряный ключ от синего сундука смотритель Ладан хранит в зелёной чашке. "
        "Медный ключ от красного сундука лежит в деревянной шкатулке.\n\n"
        "Капитан перепутал сундуки, но Ладан сверился с записью и вернул оба ключа на свои места.",
        "cc0-observatory": "Обсерватория бухты Нер\n\nПосле каждого сигнала маяка Мирта записывает число медных колец. "
        "Утром она сравнивает записи с журналом приливов.\n\n"
        "Зелёная чашка здесь используется для питьевой воды; посетители не оставляют в ней предметов.",
    }
    for identity, text in texts.items():
        path = directory / (identity + ".txt")
        path.write_text(text)
        library.import_book(
            path,
            {
                "id": identity,
                "title": text.splitlines()[0],
                "author": "BookRAG CC0 fixture",
                "language": "ru",
                "rights": "Own CC0-1.0 test text",
                "coverage": "full",
                "source_url": "https://example.invalid/bookrag-cc0/" + identity,
                "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            },
        )


def anchor_rank(candidates: list[dict[str, Any]], case: dict[str, Any], library: ReadOnlyLibrary) -> int | None:
    for position, result in enumerate(candidates, 1):
        if case.get("quote") is not None and case["quote"] in result["text"]:
            return position
        for anchor in case.get("evidence", []):
            if result["book_id"] != anchor["book_id"] or result["section_id"] is None:
                continue
            with library.connect() as conn:
                ordinal = conn.execute("SELECT ordinal FROM sections WHERE id=?", (result["section_id"],)).fetchone()[0]
            if (
                ordinal == anchor["section_ordinal"]
                and result["start"] <= anchor["quote_start"]
                and result["end"] >= anchor["quote_end"]
                and anchor["quote"] in result["text"]
            ):
                return position
    return None


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "id",
            "book_id",
            "title",
            "author",
            "edition",
            "section_id",
            "section_title",
            "section_author",
            "section_role",
            "section_translator",
            "start",
            "end",
            "citation",
            "verified",
            "reader_url",
        )
    }


def _faiss_phase(control: Connection, encoder: Connection, settings: dict[str, Any]) -> None:
    os.environ["BOOKRAG_IVFPQ_WORKER"] = "1"
    try:
        source = ReadOnlyLibrary(Path(settings["source"]))
        receipt = settings["manifest"]
        space = settings["model_space"]
        before = rpc(encoder, {"operation": "stats"})

        def encode(records: list[Any]) -> np.ndarray:
            vectors = np.empty((len(records), 384), dtype=np.float32)
            for start in range(0, len(records), 32):
                batch = records[start : start + 32]
                value = rpc(encoder, {"operation": "encode", "texts": [record.text for record in batch]})
                vectors[start : start + len(batch)] = value["vectors"]
            return vectors

        result = build_incremental(
            source,
            manifest_windows(Path(settings["manifest_path"])),
            encode,
            Path(settings["output"]),
            Path(settings["codec"]),
            settings["codec_sha256"],
            space,
            receipt["rows_by_book"],
            chunk_rows=256,
            shard_rows=2048,
            stop_after_chunks=settings.get("stop_after_chunks"),
        )
        encoded_after_build = rpc(encoder, {"operation": "stats"})
        reports = []
        if result["complete"]:
            directory = Path(result["directory"])
            ready = json.loads((directory / "ready.json").read_text())
            if ready["input_chain"] != receipt["input_chain"]:
                raise ValueError("Compressed stream differs from exact producer input manifest")
            for case in settings["cases"]:
                query = rpc(encoder, {"operation": "encode", "texts": [case["question"]]})["vectors"]
                search = query_incremental(
                    source, directory, query, space, book_id=case["book_ids"][0], language="ru", limit=100, nprobe=32
                )
                verified = source.passages([hit["passage_id"] for hit in search["results"]])
                if any(hit.get("verified") is not True for hit in verified):
                    raise ValueError("ANN result failed exact canonical source verification")
                eligible = [
                    hit
                    for hit in verified
                    if not (
                        hit.get("section_role") == "editorial"
                        and str(hit.get("section_author")).casefold() == "bookrag"
                    )
                ]
                with patch("book_rag.russian_retrieval.scoped_russian_fallback", return_value={"used": False}):
                    lexical = source.search(case["question"], "lexical", case["book_ids"][0], "ru", 40)
                morph = scoped_russian_fallback(
                    source, case["question"], case["book_ids"][0], "ru", prior_results=lexical["results"], limit=40
                )
                morphology = morph["results"] if morph["used"] else lexical["results"]
                reports.append(
                    {
                        "id": case["id"],
                        "question": case["question"],
                        "book_ids": case["book_ids"],
                        "expected_answerability": case["expected_answerability"],
                        "semantic_anchor_rank": anchor_rank(eligible, case, source),
                        "lexical_anchor_rank": anchor_rank(lexical["results"], case, source),
                        "morph_anchor_rank": anchor_rank(morphology, case, source),
                        "semantic_top": [compact(hit) for hit in eligible[:10]],
                        "lexical_count": len(lexical["results"]),
                        "morph_count": len(morphology),
                        "morph_used": morph["used"],
                        "morph_reason": morph["reason"],
                        "all_semantic_sources_exact": True,
                        "score_is_not_answerability": True,
                        "coverage": {key: search[key] for key in ("covered_books", "indexed_books", "coverage_kind")},
                    }
                )
        if "torch" in sys.modules:
            raise ValueError("Torch entered FAISS-only child")
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        control.send(
            {
                "ok": True,
                "result": result,
                "encoded_rows_this_build": encoded_after_build["rows_encoded"] - before["rows_encoded"],
                "cases": reports,
                "torch_imported": False,
                "peak_rss_bytes": int(rss if sys.platform == "darwin" else rss * 1024),
            }
        )
    except BaseException as error:
        control.send({"ok": False, "error": type(error).__name__ + ": " + str(error)[:1500]})
    finally:
        control.close()


def run_phase(service: EncoderService, settings: dict[str, Any], output: Path) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_faiss_phase, args=(child, service.connection, settings))
    process.start()
    child.close()
    started = time.monotonic()
    try:
        while not parent.poll(0.2):
            if not process.is_alive():
                raise RuntimeError("FAISS-only pilot child exited without a report")
            if time.monotonic() - started > 600:
                raise TimeoutError("Bounded RU21 phase exceeded600s")
            check_limits(output, [process.pid, service.process.pid])
        result = parent.recv()
        process.join(timeout=3)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Invalid child response"))
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        parent.close()


def check_limits(output: Path, pids: list[int | None]) -> None:
    import subprocess

    rss = 0
    for pid in pids:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False)
        rss += int(result.stdout.strip() or "0") * 1024
    if rss > MAX_RSS:
        raise RuntimeError("Combined encoder/FAISS RSS exceeded2GiB")
    if shutil.disk_usage(output).free < RESERVE:
        raise RuntimeError("Free disk crossed4.5GiB reserve")
    if sum(path.stat().st_size for path in output.rglob("*") if path.is_file()) > MAX_DISK:
        raise RuntimeError("Owned RU21 disk budget exceeded250MiB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/full"))
    parser.add_argument("--book-ids-from", type=Path, help="JSON list of exactly21 explicit source edition IDs")
    parser.add_argument("--output", type=Path, default=Path("data/semantic-pilots/ru21-v1"))
    parser.add_argument("--cache", type=Path, default=Path("data/models"))
    parser.add_argument("--codec-report", type=Path, default=Path("data/bench/ivfpq-frozen1308-sample-v2/report.json"))
    parser.add_argument("--device", choices=("cpu", "mps"), default="mps")
    parser.add_argument("--resume", action="store_true", help="Explicitly reuse an interrupted owned pilot")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists():
        parser.error("Completed pilot report already exists; choose a new output")
    canonical = output / "canonical"
    if canonical.exists():
        if not args.resume:
            parser.error("Interrupted owned pilot exists; use --resume or choose a new output")
        snapshot = verify_owned_subset(canonical)
    else:
        selected = json.loads(args.book_ids_from.read_text()) if args.book_ids_from else None
        if selected is not None and (
            not isinstance(selected, list) or len(selected) != 21 or any(not isinstance(item, str) for item in selected)
        ):
            parser.error("--book-ids-from must contain a JSON list of exactly21 edition IDs")
        snapshot = create_owned_subset(args.source, canonical, selected)
    if len(snapshot["book_ids"]) != 21:
        parser.error("Actual pilot expects exactly21 currently indexed RU editions")
    codec_report = json.loads(args.codec_report.read_text())
    codec = Path(codec_report["result"]["build"]["directory"]) / "codec.faiss"
    codec_sha = file_sha256(codec)
    if codec_sha != codec_report["result"]["build"]["shards"][0]["codec_sha256"]:
        raise ValueError("Frozen trained codec differs from its measured receipt")
    check_limits(output, [])
    service = EncoderService(args.cache, args.device)
    try:
        space = service.metadata["model_space"]
        if (
            space["model"] != codec_report["source"]["model"]
            or space["revision"] != codec_report["source"]["model_revision"]
            or space["profile"] != codec_report["source"]["embedding_profile"]
            or space["strategy"] != codec_report["source"]["strategy"]
        ):
            raise ValueError("Frozen codebook belongs to another embedding model/input space")
        manifest_path = output / "windows.sqlite3"
        manifest = rpc(
            service.connection, {"operation": "prepare", "source": str(canonical), "output": str(manifest_path)}, 60
        )
        settings = {
            "source": str(canonical),
            "manifest": manifest,
            "manifest_path": str(manifest_path),
            "output": str(output / "compressed"),
            "codec": str(codec),
            "codec_sha256": codec_sha,
            "model_space": space,
            "cases": public_cases(),
        }
        checkpoints = list((output / "compressed").glob("incremental-*/checkpoint.json"))
        if len(checkpoints) > 1:
            raise ValueError("Multiple source/model generations require a new output or explicit review")
        old_durable = json.loads(checkpoints[0].read_text())["completed_rows"] if checkpoints else 0
        first = run_phase(service, settings | {"stop_after_chunks": 2}, output)
        second = run_phase(service, settings, output)
        if old_durable + first["encoded_rows_this_build"] + second["encoded_rows_this_build"] != manifest["rows"]:
            raise ValueError("Resume re-encoded durable canonical windows")
        if not second["result"]["complete"]:
            raise ValueError("RU21 pilot did not seal all source windows")
        cc0 = output / "cc0"
        if not cc0.exists():
            create_cc0(cc0)
        cc0_manifest_path = output / "cc0-windows.sqlite3"
        cc0_manifest = rpc(
            service.connection, {"operation": "prepare", "source": str(cc0), "output": str(cc0_manifest_path)}, 30
        )
        cc0_result = run_phase(
            service,
            settings
            | {
                "source": str(cc0),
                "manifest": cc0_manifest,
                "manifest_path": str(cc0_manifest_path),
                "output": str(output / "cc0-compressed"),
                "cases": cc0_cases(),
            },
            output,
        )
        encoder = service.close()
        verify_owned_subset(canonical)
        if file_sha256(codec) != codec_sha:
            raise ValueError("Original training codec changed")
        check_limits(output, [])
        report = {
            "schema_version": 1,
            "kind": "actual-owned-RU21-semantic-pilot",
            "pilot_coverage_books": 21,
            "production_promoted": False,
            "full_corpus_build": False,
            "original_library_constructor_called": False,
            "snapshot": snapshot,
            "manifest": manifest,
            "first_phase": first,
            "resumed_phase": second,
            "previously_durable_windows": old_durable,
            "cc0_manifest": cc0_manifest,
            "cc0_phase": cc0_result,
            "encoder": encoder,
            "codec_source_report_sha256": file_sha256(args.codec_report),
            "codec_sha256": codec_sha,
            "original_selected_sources_unchanged": True,
            "reader_validation": "pending-read-only-browser-probe",
            "limitations": [
                "Three public development cases and original CC0 fixtures are not a final evaluation.",
                "PQ scores do not prove entailment or calibrate no-answer behavior; no LLM is used.",
                "Pilot covers21 owned RU editions, not the growing full library.",
            ],
        }
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "report": str(output / "report.json"),
                    "books": 21,
                    "windows": manifest["rows"],
                    "source_windows_encoded_once": True,
                    "encoder_rows_total": encoder["rows_encoded"],
                }
            )
        )
    finally:
        service.abort()


if __name__ == "__main__":
    main()
