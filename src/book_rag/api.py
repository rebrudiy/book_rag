"""Local HTTP API and browser interface."""

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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


def create_app(directory: Path | None = None) -> FastAPI:
    library = Library(directory or data_dir())
    app = FastAPI(title="Book Atlas", version="0.1.0")
    app.state.library = library
    static = Path(__file__).parent / "static"

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return library.stats()

    @app.get("/api/books")
    def books(language: str | None = None, q: str = Query(default="", max_length=500)) -> dict[str, Any]:
        return {"books": library.books(language, q)}

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
        mode: Literal["lexical", "hybrid", "semantic"] = "lexical",
        book_id: str | None = None,
        language: str | None = None,
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, Any]:
        return library.search(q, mode, book_id, language, limit)

    @app.post("/api/research")
    def research(request: ResearchRequest) -> dict[str, Any]:
        return library.research(request.query, request.book_ids, request.limit)

    @app.get("/api/saved")
    def saved() -> dict[str, Any]:
        return {"items": library.saved()}

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

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
