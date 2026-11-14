"""Transactional corpus storage, lexical retrieval and verifiable evidence.

Offsets index Python Unicode codepoints in the canonical section text, not the
original ebook byte stream. Original artifact hashes remain separately recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CHUNKER_VERSION = "paragraph-sentence-v1-1200-2000"

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, language TEXT NOT NULL,
 work_id TEXT NOT NULL, edition TEXT NOT NULL, source_url TEXT NOT NULL, rights TEXT NOT NULL,
 rights_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'discovered',
 artifact_sha256 TEXT, content_sha256 TEXT, local_path TEXT, error TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS books_work ON books(work_id);
CREATE INDEX IF NOT EXISTS books_content ON books(content_sha256);
CREATE TABLE IF NOT EXISTS index_versions (
 book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE, fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_metadata (
 book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, UNIQUE(book_id,ordinal)
);
CREATE TABLE IF NOT EXISTS section_roles (
 section_id INTEGER PRIMARY KEY REFERENCES sections(id) ON DELETE CASCADE,
 section_author TEXT NOT NULL, section_role TEXT NOT NULL, attribution_basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_deferrals (
 host TEXT PRIMARY KEY, retry_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
 start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL,
 UNIQUE(section_id,start,end)
);
CREATE INDEX IF NOT EXISTS passages_book ON passages(book_id);
CREATE INDEX IF NOT EXISTS passages_section ON passages(section_id);
CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
 text, title, author, section_title, content='', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS saved (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL,
 query TEXT NOT NULL DEFAULT '', passage_id INTEGER REFERENCES passages(id) ON DELETE SET NULL,
 collection TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 snapshot_text TEXT NOT NULL DEFAULT '', snapshot_citation TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS saved_provenance (
 item_id INTEGER PRIMARY KEY REFERENCES saved(id) ON DELETE CASCADE, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_spans(text: str, target: int = 1200, maximum: int = 2000) -> list[tuple[int, int]]:
    """Partition at paragraphs/sentences, retaining exact nonoverlapping spans."""
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        end = min(start + maximum, len(text))
        if end < len(text):
            paragraphs = [m.start() + start for m in re.finditer(r"\n\s*\n", text[start:end])]
            boundaries = [p for p in paragraphs if p >= start + target // 2]
            if boundaries:
                end = min(boundaries, key=lambda p: abs(p - start - target))
            else:
                sentences = list(re.finditer(r"[.!?。！？](?:\s|$)", text[start:end]))
                viable = [start + m.start() + 1 for m in sentences if m.start() >= target // 2]
                if viable:
                    end = min(viable, key=lambda p: abs(p - start - target))
                else:
                    space = text.rfind(" ", start + target // 2, end)
                    if space > start:
                        end = space
        while end > start and text[end - 1].isspace():
            end -= 1
        spans.append((start, end))
        start = end
    return spans


class Library:
    """A process-safe local SQLite library; each operation owns its connection."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "library.sqlite3"
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def register(self, metadata: dict[str, Any]) -> str:
        """Persist discovery before download so interrupted jobs remain visible."""
        book_id = str(metadata.get("id") or digest(json.dumps(metadata, sort_keys=True))[:20])
        fields = ("title", "author", "language", "work_id", "edition", "source_url", "rights", "rights_url")
        values = [str(metadata.get(k, "") or "") for k in fields]
        values[3] = values[3] or digest(unicodedata.normalize("NFKC", values[0] + values[1]).casefold())[:20]
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO books (id,title,author,language,work_id,edition,source_url,rights,rights_url) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (book_id, *values),
            )
            conn.execute(
                "INSERT OR IGNORE INTO book_metadata(book_id,metadata) VALUES (?,?)",
                (book_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
            )
        return book_id

    def stage(self, book_id: str, stage: str, detail: str = "", local_path: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE books SET status=?,error=?,local_path=COALESCE(?,local_path) WHERE id=?",
                (stage, detail if stage in ("failed", "deferred") else None, local_path, book_id),
            )
            conn.execute("INSERT INTO events(book_id,stage,detail) VALUES (?,?,?)", (book_id, stage, detail))

    def import_book(self, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        """Index atomically, deduplicate normalized editions, resume safely after failure."""
        from book_rag.attribution import section_attribution
        from book_rag.parsers import PARSER_FINGERPRINT, parse_book

        indexer_version = digest(
            PARSER_FINGERPRINT
            + CHUNKER_VERSION
            + "explicit-contributors-v1"
            + json.dumps(metadata.get("section_attributions", []), ensure_ascii=False, sort_keys=True)
        )

        book_id = self.register(metadata)
        fields = ("title", "author", "language", "work_id", "edition", "source_url", "rights", "rights_url")
        try:
            with path.open("rb") as artifact:
                artifact_hash = hashlib.file_digest(artifact, "sha256").hexdigest()
            with self.connect() as conn:
                old = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
                version = conn.execute("SELECT fingerprint FROM index_versions WHERE book_id=?", (book_id,)).fetchone()
                changed = any(str(metadata[k] or "") != old[k] for k in fields if k in metadata) if old else True
                if (
                    old
                    and old["status"] == "indexed"
                    and old["artifact_sha256"] == artifact_hash
                    and not changed
                    and version
                    and version[0] == indexer_version
                ):
                    conn.execute(
                        "INSERT INTO book_metadata(book_id,metadata) VALUES (?,?) "
                        "ON CONFLICT(book_id) DO UPDATE SET metadata=excluded.metadata",
                        (book_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
                    )
                    return {"id": book_id, "status": "indexed", "skipped": True}
            self.stage(book_id, "downloaded", local_path=str(path.resolve()))
            parsed = parse_book(path)
            if not parsed.sections or not any(s.text.strip() for s in parsed.sections):
                raise ValueError("No extractable text; scanned PDFs require OCR before import")
            self.stage(book_id, "processed")
            content_hash = digest("\n\n".join(s.text for s in parsed.sections))
            with self.connect() as conn:
                duplicate = conn.execute(
                    "SELECT id FROM books WHERE content_sha256=? AND status='indexed' AND id!=?",
                    (content_hash, book_id),
                ).fetchone()
                self._remove_index(conn, book_id)
                title = metadata.get("title") or parsed.title or path.stem
                author = metadata.get("author") or parsed.author or "Unknown"
                language = metadata.get("language") or parsed.language or "und"
                effective = {key: str(metadata[key] or "") if key in metadata else old[key] for key in fields}
                effective.update(title=title, author=author, language=language)
                conn.execute(
                    "UPDATE books SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                    (*(effective[key] for key in fields), book_id),
                )
                conn.execute(
                    "INSERT INTO book_metadata(book_id,metadata) VALUES (?,?) "
                    "ON CONFLICT(book_id) DO UPDATE SET metadata=excluded.metadata",
                    (book_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
                )
                if duplicate:
                    conn.execute(
                        "UPDATE books SET status='duplicate',content_sha256=?,artifact_sha256=?,error=? WHERE id=?",
                        (content_hash, artifact_hash, f"duplicate_of:{duplicate['id']}", book_id),
                    )
                    return {"id": book_id, "status": "duplicate", "duplicate_of": duplicate["id"]}
                for ordinal, section in enumerate(parsed.sections):
                    cursor = conn.execute(
                        "INSERT INTO sections(book_id,ordinal,title,text) VALUES (?,?,?,?)",
                        (book_id, ordinal, section.title, section.text),
                    )
                    section_id = cursor.lastrowid
                    attribution = section_attribution(section.title, metadata | {"author": author})
                    conn.execute(
                        "INSERT INTO section_roles(section_id,section_author,section_role,attribution_basis) "
                        "VALUES (?,?,?,?)",
                        (section_id, attribution["author"], attribution["role"], attribution["attribution_basis"]),
                    )
                    indexed_author = (
                        author if attribution["author"] == author else author + "; " + attribution["author"]
                    )
                    for start, end in chunk_spans(section.text):
                        text = section.text[start:end]
                        cursor = conn.execute(
                            "INSERT INTO passages(book_id,section_id,start,end,text,sha256) VALUES (?,?,?,?,?,?)",
                            (book_id, section_id, start, end, text, digest(text)),
                        )
                        conn.execute(
                            "INSERT INTO passage_fts(rowid,text,title,author,section_title) VALUES (?,?,?,?,?)",
                            (cursor.lastrowid, text, title, indexed_author, section.title),
                        )
                conn.execute(
                    "UPDATE books SET title=?,author=?,language=?,artifact_sha256=?,content_sha256=?,"
                    "status='indexed',error=NULL,indexed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (title, author, language, artifact_hash, content_hash, book_id),
                )
                conn.execute(
                    "INSERT INTO index_versions(book_id,fingerprint) VALUES (?,?) "
                    "ON CONFLICT(book_id) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (book_id, indexer_version),
                )
                conn.execute("INSERT INTO events(book_id,stage) VALUES (?,'indexed')", (book_id,))
            return {"id": book_id, "status": "indexed", "skipped": False}
        except Exception as exc:
            # Existing index remains searchable only once a successful import restores indexed state.
            self.stage(book_id, "failed", str(exc)[:1000])
            raise

    def _remove_index(self, conn: sqlite3.Connection, book_id: str) -> None:
        rows = conn.execute(
            "SELECT p.*,b.title,b.author,s.title section_title,a.section_author FROM passages p "
            "JOIN books b ON p.book_id=b.id JOIN sections s ON p.section_id=s.id "
            "LEFT JOIN section_roles a ON a.section_id=s.id WHERE p.book_id=?",
            (book_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO passage_fts(passage_fts,rowid,text,title,author,section_title) "
                "VALUES ('delete',?,?,?,?,?)",
                (
                    row["id"],
                    row["text"],
                    row["title"],
                    row["author"] + "; " + row["section_author"]
                    if row["section_author"] and row["section_author"] != row["author"]
                    else row["author"],
                    row["section_title"],
                ),
            )
        conn.execute("DELETE FROM sections WHERE book_id=?", (book_id,))

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM books GROUP BY status")}
            return {
                "books": counts.get("indexed", 0),
                "works": conn.execute("SELECT COUNT(DISTINCT work_id) FROM books WHERE status='indexed'").fetchone()[0],
                "passages": conn.execute(
                    "SELECT COUNT(*) FROM passages p JOIN books b ON b.id=p.book_id WHERE b.status='indexed'"
                ).fetchone()[0],
                "status_counts": counts,
                "work_count_is_provisional": bool(
                    conn.execute(
                        "SELECT COUNT(*) FROM books b LEFT JOIN book_metadata m ON m.book_id=b.id "
                        "WHERE b.status='indexed' AND "
                        "COALESCE(json_extract(m.metadata,'$.work_identity_status'),'unspecified')"
                        "!='curated'"
                    ).fetchone()[0]
                ),
                "curated_work_groups": conn.execute(
                    "SELECT COUNT(DISTINCT b.work_id) FROM books b JOIN book_metadata m ON m.book_id=b.id "
                    "WHERE b.status='indexed' AND json_extract(m.metadata,'$.work_identity_status')='curated'"
                ).fetchone()[0],
                "periodical_editions": conn.execute(
                    "SELECT COUNT(*) FROM books b JOIN book_metadata m ON m.book_id=b.id "
                    "WHERE b.status='indexed' AND json_extract(m.metadata,'$.work_type')='periodical_issue'"
                ).fetchone()[0],
                "languages": [
                    dict(r)
                    for r in conn.execute(
                        "SELECT language,COUNT(*) count FROM books WHERE status='indexed' GROUP BY language"
                    )
                ],
            }

    def books(self, language: str | None = None, q: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT b.*, (SELECT COUNT(*) FROM sections s WHERE s.book_id=b.id) sections,"
                    "(SELECT COUNT(*) FROM passages p WHERE p.book_id=b.id) passages FROM books b "
                    "WHERE (? IS NULL OR language=?) AND (?='' OR instr(casefold(title),casefold(?))>0 "
                    "OR instr(casefold(author),casefold(?))>0) ORDER BY title",
                    (language, language, q, q, q),
                )
            ]

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if not row:
                return None
            book = dict(row)
            metadata = conn.execute("SELECT metadata FROM book_metadata WHERE book_id=?", (book_id,)).fetchone()
            book["source_metadata"] = json.loads(metadata[0]) if metadata else {}
            book["sections"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT s.id,s.ordinal,s.title,s.text,a.section_author,a.section_role,a.attribution_basis "
                    "FROM sections s LEFT JOIN section_roles a ON a.section_id=s.id WHERE s.book_id=? ORDER BY ordinal",
                    (book_id,),
                )
            ]
            return book

    @staticmethod
    def _result(row: sqlite3.Row) -> dict[str, Any]:
        r = dict(row)
        section_quote = r.pop("section_quote", None)
        if section_quote is None:
            section_quote = r.pop("section_text")[r["start"] : r["end"]]
        valid = (
            0 <= r["start"] < r["end"]
            and r["end"] - r["start"] == len(r["text"])
            and section_quote == r["text"]
            and digest(r["text"]) == r["sha256"]
        )
        r.pop("sha256")
        r["verified"] = valid
        r["citation"] = (
            f"{r['author']}. {r['title']}. {r['edition']}. {r['section_title']}, chars {r['start']}–{r['end']}."
        )
        if r.get("section_author") and r["section_author"] != r["author"]:
            r["citation"] = f"{r['section_author']} ({r.get('section_role', 'contributor')}). В: " + r["citation"]
        from urllib.parse import urlencode

        r["reader_url"] = "/?" + urlencode({"book": r["book_id"], "section": r["section_id"], "passage": r["id"]})
        return r

    def passage(self, passage_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
                "b.rights_url,b.artifact_sha256,b.content_sha256,"
                "s.title section_title,s.text section_text,a.section_author,a.section_role,a.attribution_basis,"
                "0.0 score FROM passages p "
                "JOIN books b ON p.book_id=b.id JOIN sections s ON p.section_id=s.id "
                "LEFT JOIN section_roles a ON a.section_id=s.id WHERE p.id=?",
                (passage_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._result(row)
            result["context"] = row["section_text"][max(0, row["start"] - 800) : row["end"] + 800]
            return result

    def search(
        self,
        query: str,
        mode: str = "lexical",
        book_id: str | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        limit = max(1, min(limit, 50))
        if mode not in ("baseline", "lexical", "hybrid", "semantic", "reranked"):
            raise ValueError("Unknown search mode")
        if mode == "reranked":
            from book_rag.reranking import rerank

            candidates = self.search(query, "hybrid", book_id, language, 50)
            ranked = rerank(query, candidates["results"], self.data_dir / "models", limit=limit)
            if ranked["available"]:
                return {
                    "query": query,
                    "mode": "reranked",
                    "results": ranked["results"],
                    "warnings": candidates["warnings"] + ranked["warnings"],
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "model": candidates.get("model"),
                    "reranker": ranked["model"],
                    "reranker_revision": ranked["revision"],
                }
            candidates["results"] = candidates["results"][:limit]
            candidates["warnings"].extend(ranked["warnings"])
            return candidates
        if mode in ("hybrid", "semantic"):
            from book_rag.semantic import semantic_search

            return semantic_search(self, query, mode, book_id, language, limit)
        tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", query), re.UNICODE)[:40]
        from book_rag.retrieval import prepare_query, rerank_results

        plan = prepare_query(query)
        expression = plan.expression if mode == "lexical" else " OR ".join('"' + t + '"' for t in tokens)
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        if mode == "lexical" and not expression and plan.reason:
            warnings.append(plan.reason)
        if expression:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
                    "b.rights_url,b.artifact_sha256,b.content_sha256,"
                    "s.title section_title,substr(s.text,p.start+1,p.end-p.start) section_quote,"
                    "a.section_author,a.section_role,a.attribution_basis,"
                    "bm25(passage_fts,1.0,2.0,1.5,1.2) score "
                    "FROM passage_fts JOIN passages p ON p.id=passage_fts.rowid JOIN books b ON b.id=p.book_id "
                    "JOIN sections s ON s.id=p.section_id LEFT JOIN section_roles a ON a.section_id=s.id "
                    "WHERE passage_fts MATCH ? AND b.status='indexed' "
                    "AND (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) ORDER BY score,p.id LIMIT ?",
                    (expression, book_id, book_id, language, language, max(200, limit) if mode == "lexical" else limit),
                ).fetchall()
                results = [self._result(row) for row in rows]
                if any(not r["verified"] for r in results):
                    warnings.append("Corrupt citation suppressed; rebuild the affected book")
                    results = [r for r in results if r["verified"]]
                if mode == "lexical":
                    results = rerank_results(plan, results, limit=limit)
        return {
            "query": query,
            "mode": mode,
            "results": results,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "warnings": warnings,
        }

    def research(self, query: str, book_ids: list[str] | None = None, limit: int = 8) -> dict[str, Any]:
        """Produce a transparent extractive research board, never fabricated prose."""
        if book_ids:
            candidates = []
            for book_id in dict.fromkeys(book_ids[:10]):
                candidates.extend(self.search(query, book_id=book_id, limit=max(1, limit // len(book_ids)))["results"])
            results = candidates[:limit]
        else:
            candidates = self.search(query, limit=50)["results"]
            results = []
            counts: dict[str, int] = {}
            for r in candidates:
                if counts.get(r["book_id"], 0) < 2:
                    results.append(r)
                    counts[r["book_id"]] = counts.get(r["book_id"], 0) + 1
                if len(results) >= limit:
                    break
        from book_rag.retrieval import has_research_support, prepare_query

        plan = prepare_query(query)
        results = [r for r in results if has_research_support(plan, r)]
        return {
            "query": query,
            "results": results,
            "abstained": not results,
            "method": "extractive",
            "answer": "Найдены проверенные фрагменты для исследования. Сопоставьте их в контексте глав."
            if results
            else "В текущей библиотеке не найдено подтверждающих фрагментов. Попробуйте другой запрос.",
        }

    def saved(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM saved ORDER BY id DESC")]
        for row in rows:
            row["text"] = row.pop("snapshot_text")
            row["citation"] = row.pop("snapshot_citation")
            with self.connect() as conn:
                provenance = conn.execute(
                    "SELECT metadata FROM saved_provenance WHERE item_id=?", (row["id"],)
                ).fetchone()
            row["provenance"] = json.loads(provenance[0]) if provenance else {}
            if row["passage_id"] is not None:
                current = self.passage(row["passage_id"])
                if (
                    current
                    and current["verified"]
                    and current["text"] == row["text"]
                    and current["citation"] == row["citation"]
                ):
                    row["reader_url"] = current["reader_url"]
        return rows

    def save(self, item: dict[str, Any]) -> dict[str, Any]:
        kind = item.get("kind")
        if kind not in ("passage", "query"):
            raise ValueError("kind must be passage or query")
        passage = self.passage(item["passage_id"]) if kind == "passage" and item.get("passage_id") else None
        if kind == "passage" and (not passage or not passage["verified"]):
            raise ValueError("A verified passage is required")
        if kind == "query" and not str(item.get("query", "")).strip():
            raise ValueError("A nonempty query is required")
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO saved(kind,title,query,passage_id,collection,note,"
                "snapshot_text,snapshot_citation) VALUES (?,?,?,?,?,?,?,?)",
                (
                    kind,
                    str(item.get("title", ""))[:500],
                    str(item.get("query", ""))[:2000],
                    passage["id"] if passage else None,
                    str(item.get("collection", ""))[:200],
                    str(item.get("note", ""))[:10000],
                    passage["text"] if passage else "",
                    passage["citation"] if passage else "",
                ),
            )
            new_id = cursor.lastrowid
            if passage:
                fields = (
                    "book_id",
                    "title",
                    "author",
                    "edition",
                    "source_url",
                    "rights",
                    "rights_url",
                    "section_id",
                    "section_title",
                    "section_author",
                    "section_role",
                    "attribution_basis",
                    "start",
                    "end",
                    "artifact_sha256",
                    "content_sha256",
                )
                provenance = {key: passage.get(key) for key in fields}
                conn.execute(
                    "INSERT INTO saved_provenance(item_id,metadata) VALUES (?,?)",
                    (new_id, json.dumps(provenance, ensure_ascii=False)),
                )
        return next(r for r in self.saved() if r["id"] == new_id)

    def delete_saved(self, item_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute("DELETE FROM saved WHERE id=?", (item_id,)).rowcount > 0

    def defer_source(self, url: str, retry_at: float) -> None:
        from urllib.parse import urlparse

        with self.connect() as conn:
            conn.execute(
                "INSERT INTO source_deferrals(host,retry_at) VALUES (?,?) "
                "ON CONFLICT(host) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)",
                (urlparse(url).netloc.casefold(), retry_at),
            )

    def source_retry_at(self, url: str) -> float | None:
        from urllib.parse import urlparse

        with self.connect() as conn:
            row = conn.execute(
                "SELECT retry_at FROM source_deferrals WHERE host=?", (urlparse(url).netloc.casefold(),)
            ).fetchone()
        return float(row[0]) if row and row[0] > time.time() else None
