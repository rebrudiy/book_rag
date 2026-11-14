"""Reproducible, resumable book-library commands."""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from book_rag.config import data_dir
from book_rag.library import Library

app = typer.Typer(no_args_is_help=True, help="Book Atlas — local, verifiable book research")


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def ingest_manifest(
    manifest: Path, limit: int | None = None, download_delay: float = 2.0, min_free_gib: float = 4.0
) -> dict[str, Any]:
    from book_rag.sources import DownloadDeferred, cached_source, download_source, load_manifest

    library = Library(data_dir())
    entries = load_manifest(manifest)
    report: list[dict[str, Any]] = []
    start = time.perf_counter()
    downloaded_at = 0.0
    stopped_reason = None
    for entry in entries:
        library.register(entry)
    for entry in entries[:limit]:
        book_id = library.register(entry)
        try:
            if shutil.disk_usage(data_dir()).free < min_free_gib * 1024**3:
                stopped_reason = f"Free disk is below {min_free_gib} GiB; queued books remain discovered"
                break
            if entry.get("local_path"):
                path = Path(entry["local_path"])
                if not path.is_absolute():
                    path = manifest.parent / path
            else:
                # Follow Gutenberg's published two-second harvesting example by default.
                path = cached_source(entry, data_dir() / "downloads")
                if path is None:
                    pending = library.source_retry_at(entry["download_url"])
                    if pending is not None:
                        stopped_reason = f"Provider retry deferral still active until epoch {pending}"
                        library.stage(book_id, "deferred", stopped_reason)
                        report.append({"id": book_id, "status": "deferred", "retry_at": pending})
                        typer.echo(json.dumps(report[-1]), err=True)
                        break
                    wait = max(0.0, download_delay - (time.monotonic() - downloaded_at))
                    if wait:
                        time.sleep(wait)
                    path = download_source(entry, data_dir() / "downloads")
                    downloaded_at = time.monotonic()
            result = library.import_book(path, entry)
            report.append(result)
        except DownloadDeferred as exc:
            retry_at = exc.retry_at.timestamp() if exc.retry_at is not None else time.time() + exc.retry_after
            library.defer_source(entry["download_url"], retry_at)
            library.defer_source(str(exc.request.url), retry_at)
            stopped_reason = str(exc)
            library.stage(book_id, "deferred", stopped_reason)
            report.append({"id": book_id, "status": "deferred", "retry_at": retry_at, "error": str(exc)})
            typer.echo(json.dumps(report[-1]), err=True)
            break
        except Exception as exc:
            library.stage(book_id, "failed", str(exc)[:1000])
            report.append({"id": book_id, "status": "failed", "error": str(exc)})
        typer.echo(json.dumps(report[-1], ensure_ascii=False), err=True)
    return {
        "entries": report,
        "elapsed_seconds": round(time.perf_counter() - start, 3),
        "stopped_reason": stopped_reason,
        "stats": library.stats(),
    }


@app.command()
def demo() -> None:
    """Import the checked-in, licensed demo excerpts (no network)."""
    manifest = Path(__file__).resolve().parents[2] / "corpus" / "demo" / "manifest.json"
    if not manifest.exists():
        raise typer.BadParameter("Run from a source checkout containing corpus/demo/manifest.json")
    emit(ingest_manifest(manifest))


@app.command()
def ingest(
    manifest: Path,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    download_delay: Annotated[float, typer.Option(min=0)] = 2.0,
    min_free_gib: Annotated[float, typer.Option(min=1)] = 4.0,
) -> None:
    """Discover a source catalog, then resume downloads and indexing."""
    emit(ingest_manifest(manifest, limit, download_delay, min_free_gib))


@app.command("import")
def import_file(path: Path, metadata: Annotated[Path, typer.Option()]) -> None:
    """Import a permitted local file with explicit provenance JSON."""
    emit(Library(data_dir()).import_book(path, json.loads(metadata.read_text())))


@app.command()
def stats() -> None:
    emit(Library(data_dir()).stats())


@app.command()
def search(query: str, mode: str = "lexical", limit: int = 10, book: str | None = None) -> None:
    emit(Library(data_dir()).search(query, mode=mode, book_id=book, limit=limit))


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Start the local application; loopback only by default."""
    import uvicorn

    from book_rag.api import create_app

    uvicorn.run(
        create_app(),
        host=host or os.getenv("BOOKRAG_HOST", "127.0.0.1"),
        port=port or int(os.getenv("BOOKRAG_PORT", "8000")),
    )


@app.command()
def evaluate(
    dataset: Path = Path("evals/dev.json"), mode: str = "lexical", output: Path = Path("experiments/latest.json")
) -> None:
    """Measure retrieval and evidence integrity, storing a reproducible report."""
    from book_rag.evaluation import evaluate as run_evaluation

    result = run_evaluation(Library(data_dir()), dataset, mode=mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    emit(result)


@app.command()
def embed(model: str | None = None, batch_size: int = 32) -> None:
    """Build a local multilingual vector index (requires semantic extra)."""
    from book_rag.semantic import build_index

    emit(build_index(Library(data_dir()), model_name=model, batch_size=batch_size))


@app.command()
def discover(
    catalog: Path,
    output: Path = Path("data/catalog/queue.json"),
    limit: int = 200,
    language: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Queue rights-verified books from a local official Gutenberg RDF catalog."""
    from book_rag.catalog import discover_gutenberg

    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    emit(discover_gutenberg(catalog, output, limit=limit, languages=language))


@app.command("export")
def export_saved(output: Path = Path("data/exports/collection.md"), collection: str | None = None) -> None:
    """Export saved evidence/notes as Markdown with frozen source provenance."""
    from book_rag.exporting import export_collection

    markdown = export_collection(Library(data_dir()).saved(), collection)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    emit({"output": str(output), "collection": collection, "bytes": output.stat().st_size})


@app.command()
def snapshot(output: Path, manifest: Path | None = None, include_saved: bool = True) -> None:
    """Freeze a consistent library backup or a manifest-filtered evaluation subset."""
    from book_rag.snapshots import create_snapshot
    from book_rag.sources import load_manifest

    book_ids = [entry["id"] for entry in load_manifest(manifest)] if manifest else None
    emit(create_snapshot(Library(data_dir()), output, book_ids, include_saved))


@app.command("acquire-modern")
def acquire_modern(output: Path = Path("data/modern/d2l"), index: bool = False) -> None:
    """Acquire pinned CC BY-SA D2L chapters with exact code/math and full provenance."""
    from book_rag.modern_sources import acquire_d2l

    result = acquire_d2l(output)
    emit({key: str(value) if isinstance(value, Path) else value for key, value in result.items()})
    if index:
        emit(ingest_manifest(result["manifest"]))


@app.command()
def verify(artifacts: bool = False, output: Path | None = None) -> None:
    """Audit every canonical citation and optionally original artifact hashes."""
    from book_rag.integrity import audit_library

    report = audit_library(Library(data_dir()), artifacts=artifacts)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    emit(report)
    if not report["valid"]:
        raise typer.Exit(1)


@app.command("plan-balanced")
def plan_balanced(
    catalog: Path,
    output: Path = Path("data/catalog/balanced.json"),
    per_language: Annotated[int, typer.Option(min=1)] = 100,
    language: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Plan a rights-checked language/subject-balanced queue from offline metadata."""
    from book_rag.corpus_planning import plan_balanced_corpus

    emit(plan_balanced_corpus(catalog, output, per_language, language))


@app.command("acquire-russian")
def acquire_russian(
    output: Path = Path("data/russian/wikisource"),
    limit: Annotated[int, typer.Option(min=1, max=5)] = 5,
    index: bool = False,
) -> None:
    """Acquire five verified Russian originals with captured-revision provenance."""
    from book_rag.russian_sources import acquire_russian as acquire

    result = acquire(output, limit=limit)
    emit({key: str(value) if isinstance(value, Path) else value for key, value in result.items()})
    if index:
        emit(ingest_manifest(result["manifest"]))
