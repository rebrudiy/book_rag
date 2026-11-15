"""Build real, complete-edition compact vectors without copying the live database.

Explicit local IDs and a measured compatible codebook are required. No activation,
new model downloads, generated answers or closed evaluation labels are involved.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import resource
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np

from book_rag.encoder_ipc import EncoderService, rpc
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import build_incremental
from book_rag.semantic_batches import RESERVE_BYTES, verify_batch_source
from book_rag.semantic_pilot import ReadOnlyLibrary, _publish_json, manifest_windows


def _build_worker(control: Connection, encoder: Connection, settings: dict[str, Any]) -> None:
    os.environ["BOOKRAG_IVFPQ_WORKER"] = "1"
    try:
        source = ReadOnlyLibrary(Path(settings["source"]))
        receipt = settings["manifest"]
        verify_batch_source(source, receipt)

        def encode(records: list[Any]) -> np.ndarray:
            vectors = np.empty((len(records), 384), dtype=np.float32)
            for start in range(0, len(records), 32):
                texts = [record.text for record in records[start : start + 32]]
                vectors[start : start + len(texts)] = rpc(encoder, {"operation": "encode", "texts": texts})["vectors"]
            return vectors

        result = build_incremental(
            source,
            manifest_windows(Path(settings["manifest_path"])),
            encode,
            Path(settings["output"]),
            Path(settings["codec"]),
            settings["codec_sha256"],
            settings["model_space"],
            receipt["rows_by_book"],
            chunk_rows=256,
            shard_rows=8192,
            stop_after_chunks=settings.get("stop_after_chunks"),
        )
        verify_batch_source(source, receipt)
        if result["complete"]:
            ready = json.loads((Path(result["directory"]) / "ready.json").read_text())
            if ready["input_chain"] != receipt["input_chain"]:
                raise ValueError("Completed compact vectors differ from the exact canonical input stream")
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        control.send(
            {
                "ok": True,
                "result": result,
                "peak_rss_bytes": int(rss if sys.platform == "darwin" else rss * 1024),
                "torch_imported": "torch" in sys.modules,
            }
        )
    except BaseException as error:
        control.send({"ok": False, "error": type(error).__name__ + ": " + str(error)[:1500]})
    finally:
        control.close()


def _resources(output: Path, pids: list[int | None], max_output_bytes: int) -> dict[str, int]:
    size = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    free = shutil.disk_usage(output).free
    if size > max_output_bytes:
        raise RuntimeError("Owned batch output exceeded its disk budget")
    if free < RESERVE_BYTES:
        raise RuntimeError("Free disk space reached the 4.5GiB reserve")
    for pid in pids:
        if pid is None:
            continue
        response = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False)
        if int(response.stdout.strip() or "0") * 1024 > 2 * 1024**3:
            raise RuntimeError("Semantic batch worker exceeded 2GiB RSS")
    return {"owned_output_bytes": size, "free_disk_bytes": free}


def run_phase(
    service: EncoderService,
    settings: dict[str, Any],
    output: Path,
    max_output_bytes: int,
    phase_timeout: float = 3600,
) -> dict[str, Any]:
    if isinstance(phase_timeout, bool) or not math.isfinite(phase_timeout) or not 1 <= phase_timeout <= 86400:
        raise ValueError("Phase timeout must be finite and within1–86400seconds")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_build_worker, args=(child, service.connection, settings))
    process.start()
    child.close()
    began = time.monotonic()
    try:
        while not parent.poll(0.5):
            if not process.is_alive():
                raise RuntimeError("Isolated compact-index worker exited without a report")
            if time.monotonic() - began > phase_timeout:
                raise TimeoutError("Compact-index phase exceeded its wall-clock deadline")
            _resources(output, [process.pid, service.process.pid], max_output_bytes)
        result = parent.recv()
        process.join(timeout=3)
        if result.get("ok") is not True:
            raise RuntimeError(result.get("error", "Invalid compact-index response"))
        if result["torch_imported"]:
            raise RuntimeError("Torch entered the isolated native index worker")
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        parent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/full"))
    parser.add_argument("--book-ids-from", type=Path, required=True, help="JSON list of explicit indexed edition IDs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("data/models"))
    parser.add_argument("--codec-report", type=Path, default=Path("data/bench/ivfpq-frozen1308-sample-v2/report.json"))
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    parser.add_argument("--max-windows", type=int, default=200_000)
    parser.add_argument("--max-output-mib", type=int, default=700)
    parser.add_argument("--phase-timeout", type=float, default=3600, help="Finite native build deadline in seconds")
    parser.add_argument("--stop-after-chunks", type=int, help="Intentionally checkpoint an unfinished build")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    source = ReadOnlyLibrary(args.source)
    if output.is_relative_to(source.data_dir):
        parser.error("Use a separate owned output directory outside the live library")
    if not 16 <= args.max_output_mib <= 1024:
        parser.error("Output budget must be within16–1024MiB")
    if not math.isfinite(args.phase_timeout) or not 1 <= args.phase_timeout <= 86400:
        parser.error("Phase timeout must be within1–86400seconds")
    if (output / "report.json").exists():
        parser.error("Completed batch already exists; preserve it and select another output")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error("Use --resume explicitly for an interrupted/prepared batch")
    output.mkdir(parents=True, exist_ok=True)
    books = json.loads(args.book_ids_from.read_text())
    report_sha = file_sha256(args.codec_report)
    codec_report = json.loads(args.codec_report.read_text())
    codec = Path(codec_report["result"]["build"]["directory"]) / "codec.faiss"
    codec_sha = file_sha256(codec)
    if codec_sha != codec_report["result"]["build"]["shards"][0]["codec_sha256"]:
        raise ValueError("Measured codebook bytes differ from the verified report")
    max_output_bytes = args.max_output_mib * 1024**2
    _resources(output, [], max_output_bytes)
    service: EncoderService | None = None
    report: dict[str, Any] = {
        "kind": "actual-complete-edition-semantic-batch",
        "started_at": datetime.now(UTC).isoformat(),
        "source": str(source.data_dir),
        "codec_report_sha256": report_sha,
        "codec_sha256": codec_sha,
        "activated": False,
        "closed_final_used": False,
    }
    try:
        service = EncoderService(args.cache, args.device)
        space = service.metadata["model_space"]
        if any(
            space[key] != codec_report["source"][report_key]
            for key, report_key in (
                ("model", "model"),
                ("revision", "model_revision"),
                ("profile", "embedding_profile"),
                ("strategy", "strategy"),
            )
        ):
            raise ValueError("Measured codebook belongs to a different model/input space")
        manifest_path = output / "windows.sqlite3"
        manifest = rpc(
            service.connection,
            {
                "operation": "prepare_batch",
                "source": str(source.data_dir),
                "output": str(manifest_path),
                "book_ids": books,
                "max_windows": args.max_windows,
                "max_manifest_bytes": min(512 * 1024**2, max_output_bytes - 8 * 1024**2),
            },
            120,
        )
        report["manifest"] = {key: manifest[key] for key in ("rows", "rows_by_book", "input_chain", "database_sha256")}
        if args.prepare_only:
            report["status"] = "prepared"
        else:
            checkpoints = list((output / "compressed").glob("incremental-*/checkpoint.json"))
            if len(checkpoints) > 1:
                raise ValueError("More than one existing source/model generation requires explicit review")
            durable_before = json.loads(checkpoints[0].read_text())["completed_rows"] if checkpoints else 0
            before = rpc(service.connection, {"operation": "stats"})
            result = run_phase(
                service,
                {
                    "source": str(source.data_dir),
                    "manifest": manifest,
                    "manifest_path": str(manifest_path),
                    "output": str(output / "compressed"),
                    "codec": str(codec),
                    "codec_sha256": codec_sha,
                    "model_space": space,
                    "stop_after_chunks": args.stop_after_chunks,
                },
                output,
                max_output_bytes,
                args.phase_timeout,
            )
            after = rpc(service.connection, {"operation": "stats"})
            report["native"] = result
            encoded = after["rows_encoded"] - before["rows_encoded"]
            expected = result["result"]["durable_rows"] - durable_before
            if encoded != expected:
                raise ValueError("Resumed batch re-encoded durable rows or skipped new inputs")
            report["encoded_rows_this_run"] = encoded
            report["durable_rows_before"] = durable_before
            report["status"] = "complete" if result["result"]["complete"] else "checkpointed"
        verify_batch_source(source, json.loads(manifest_path.with_suffix(".sqlite3.receipt.json").read_text()))
        report["source_unchanged"] = True
    except BaseException as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = type(error).__name__ + ": " + str(error)[:1500]
        raise
    finally:
        if service is not None:
            if report["status"] in {"failed", "interrupted"}:
                # A terminated native consumer may leave an encode response on
                # the pipe. Do not mistake it for a shutdown/weight-check receipt.
                service.abort()
                report["encoder_aborted_after_failure"] = True
            else:
                try:
                    closed = service.close()
                    if not isinstance(closed, dict) or closed.get("model_files_unchanged") is not True:
                        raise RuntimeError("Missing final encoder model-file verification")
                    report["encoder"] = closed
                except Exception as error:
                    report["encoder_close_error"] = str(error)[:1000]
                    report["status"] = "failed"
                    service.abort()
        report["completed_at"] = datetime.now(UTC).isoformat()
        try:
            report["resources"] = _resources(output, [], max_output_bytes)
        except Exception as error:
            report["resource_error"] = str(error)
            report["status"] = "failed"
        name = (
            "report.json"
            if report["status"] == "complete"
            else "run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        )
        _publish_json(output / name, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if report["status"] == "failed" and "error" not in report:
            raise RuntimeError("Batch final model/resource verification failed; inspect the saved report")


if __name__ == "__main__":
    main()
