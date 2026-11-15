"""Local HTTP API and browser interface."""

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from book_rag.answers import AnswerGenerator
from book_rag.config import data_dir
from book_rag.library import Library


class SavedItem(BaseModel):
    kind: Literal["passage", "query"]
    title: str = Field(default="", max_length=500)
    query: str = Field(default="", max_length=2000)
    passage_id: int | None = None
    collection: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=10000)


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    book_ids: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=8, ge=1, le=30)


class AnswerRequest(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=1, max_length=2000)
    book_ids: list[str] = Field(default_factory=list, max_length=10)
    language: str | None = Field(default=None, max_length=20)
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    limit: int = Field(default=8, ge=1, le=12)

    @field_validator("book_ids")
    @classmethod
    def valid_book_ids(cls, identities: list[str]) -> list[str]:
        if any(not identity.strip() or identity != identity.strip() or len(identity) > 200 for identity in identities):
            raise ValueError("Book IDs must be nonempty and bounded, without surrounding whitespace")
        return identities


class SavedItemUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=10000)
    collection: str | None = Field(default=None, max_length=200)


def create_app(directory: Path | None = None, *, answer_generator: AnswerGenerator | None = None) -> FastAPI:
    library = Library(directory or data_dir())
    app = FastAPI(title="Book Atlas", version="0.2.0")
    app.state.library = library
    static = Path(__file__).parent / "static"

    @app.get("/api/artifacts/{sha256}")
    def artifact(sha256: str) -> FileResponse:
        from book_rag.artifacts import locate_artifact

        try:
            path = locate_artifact(library.data_dir, sha256)
        except ValueError as exc:
            raise HTTPException(400, "Invalid or corrupt artifact digest") from exc
        if path is None:
            raise HTTPException(404, "Archived original not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return library.stats()

    @app.get("/api/explore")
    def explore() -> dict[str, Any]:
        from book_rag.explore import build_map

        return build_map(library)

    @app.get("/api/books")
    def books(
        language: str | None = None,
        q: str = Query(default="", max_length=500),
        status: Literal["indexed", "discovered", "downloaded", "processed", "failed", "duplicate", "deferred"]
        | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        ingestion_status: Literal["indexed", "discovered", "downloaded", "processed", "failed", "duplicate", "deferred"]
        | None = None,
    ) -> dict[str, Any]:
        return library.book_page(language, q, status, offset, limit, ingestion_status)

    @app.get("/api/books/{book_id}")
    def book(book_id: str) -> dict[str, Any]:
        result = library.get_book(book_id)
        if result is None:
            raise HTTPException(404, "Book not found")
        return result

    @app.get("/api/passages/{passage_id}")
    def passage(passage_id: int) -> dict[str, Any]:
        result = library.passage(passage_id)
        if result is None:
            raise HTTPException(404, "Passage not found")
        return result

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1, max_length=2000),
        mode: Literal["baseline", "lexical", "hybrid", "semantic", "reranked"] = "lexical",
        book_id: str | None = None,
        language: str | None = None,
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, Any]:
        if not q.strip():
            raise HTTPException(400, "Search query must contain text")
        try:
            return library.search(q, mode, book_id, language, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/research")
    def research(request: ResearchRequest) -> dict[str, Any]:
        if not request.query.strip():
            raise HTTPException(400, "Research query must contain text")
        try:
            return library.research(request.query, request.book_ids, request.limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/ask")
    def answer(request: AnswerRequest) -> dict[str, Any]:
        from book_rag.answers import ask

        if not request.query.strip():
            raise HTTPException(400, "Question must contain text")
        return ask(
            library,
            request.query,
            book_ids=request.book_ids,
            language=request.language,
            mode=request.mode,
            limit=request.limit,
            generator=answer_generator,
        )

    @app.get("/api/saved")
    def saved() -> dict[str, Any]:
        return {"items": library.saved()}

    @app.get("/api/saved/{item_id}/context")
    def saved_context(item_id: int, ordinal: int | None = Query(default=None, ge=0)) -> dict[str, Any]:
        try:
            result = library.saved_context(item_id, ordinal)
        except (FileNotFoundError, IndexError) as exc:
            raise HTTPException(404, "Сохранённая глава недоступна; снимок цитаты сохранён.") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "Сохранённый контекст не прошёл проверку целостности.") from exc
        if result is None:
            raise HTTPException(404, "Сохранённый фрагмент не найден.")
        return result

    @app.get("/api/saved/export")
    def export_saved(collection: str | None = Query(default=None, max_length=200)) -> Response:
        from book_rag.exporting import export_collection

        return Response(
            export_collection(library.saved(), collection),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=book-atlas-collection.md"},
        )

    @app.post("/api/saved", status_code=201)
    def save(item: SavedItem) -> dict[str, Any]:
        try:
            return library.save(item.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/saved/{item_id}")
    def delete_saved(item_id: int) -> dict[str, bool]:
        if not library.delete_saved(item_id):
            raise HTTPException(404, "Saved item not found")
        return {"deleted": True}

    @app.patch("/api/saved/{item_id}")
    def update_saved(item_id: int, item: SavedItemUpdate) -> dict[str, Any]:
        try:
            result = library.update_saved(item_id, item.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "Saved item not found")
        return result

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
