"""Reproducible, resumable book-library commands."""

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from book_rag.config import data_dir
from book_rag.library import Library

app = typer.Typer(no_args_is_help=True, help="Book Atlas — local, verifiable book research")


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def ingest_manifest(manifest: Path, limit: int | None = None) -> dict[str, Any]:
    from book_rag.sources import download_source, load_manifest

    library = Library(data_dir())
    entries = load_manifest(manifest)
    report: list[dict[str, Any]] = []
    start = time.perf_counter()
    for entry in entries:
        library.register(entry)
    for entry in entries[:limit]:
        book_id = library.register(entry)
        try:
            if entry.get("local_path"):
                path = Path(entry["local_path"])
                if not path.is_absolute():
                    path = manifest.parent / path
            else:
                path = download_source(entry, data_dir() / "downloads")
            result = library.import_book(path, entry)
            report.append(result)
        except Exception as exc:
            library.stage(book_id, "failed", str(exc)[:1000])
            report.append({"id": book_id, "status": "failed", "error": str(exc)})
        typer.echo(json.dumps(report[-1], ensure_ascii=False), err=True)
    return {"entries": report, "elapsed_seconds": round(time.perf_counter() - start, 3), "stats": library.stats()}


@app.command()
def demo() -> None:
    """Import the checked-in, licensed demo excerpts (no network)."""
    manifest = Path(__file__).resolve().parents[2] / "corpus" / "demo" / "manifest.json"
    if not manifest.exists():
        raise typer.BadParameter("Run from a source checkout containing corpus/demo/manifest.json")
    emit(ingest_manifest(manifest))


@app.command()
def ingest(manifest: Path, limit: Annotated[int | None, typer.Option(min=1)] = None) -> None:
    """Discover a source catalog, then resume downloads and indexing."""
    emit(ingest_manifest(manifest, limit))


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
