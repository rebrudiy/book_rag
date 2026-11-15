"""Transactional corpus storage, lexical retrieval and verifiable evidence.

Offsets index Python Unicode codepoints in the canonical section text, not the
original ebook byte stream. Original artifact hashes remain separately recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
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
CREATE TABLE IF NOT EXISTS source_pipeline (
 book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
 stage TEXT NOT NULL, local_path TEXT, error TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
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
 section_author TEXT NOT NULL, section_role TEXT NOT NULL, attribution_basis TEXT NOT NULL,
 section_translator TEXT NOT NULL DEFAULT ''
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
            conn.execute("BEGIN IMMEDIATE")
            if "section_translator" not in {row["name"] for row in conn.execute("PRAGMA table_info(section_roles)")}:
                conn.execute("ALTER TABLE section_roles ADD COLUMN section_translator TEXT NOT NULL DEFAULT ''")
            # Legacy failed rows are intentionally not inferred to be searchable.
            conn.execute(
                "INSERT OR IGNORE INTO source_pipeline(book_id,stage,local_path,error,updated_at) "
                "SELECT id,status,local_path,error,COALESCE(indexed_at,created_at) FROM books"
            )
            from book_rag.index_coverage import ensure_epoch_schema

            ensure_epoch_schema(conn)

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
            conn.execute(
                "INSERT OR IGNORE INTO source_pipeline(book_id,stage,local_path,error) "
                "SELECT id,status,local_path,error FROM books WHERE id=?",
                (book_id,),
            )
        return book_id

    @staticmethod
    def _pipeline_stage(
        conn: sqlite3.Connection, book_id: str, stage: str, error: str | None, local_path: str | None
    ) -> None:
        conn.execute(
            "INSERT INTO source_pipeline(book_id,stage,error,local_path) "
            "SELECT id,?,?,? FROM books WHERE id=? "
            "ON CONFLICT(book_id) DO UPDATE SET stage=excluded.stage,error=excluded.error,"
            "local_path=COALESCE(excluded.local_path,source_pipeline.local_path),updated_at=CURRENT_TIMESTAMP",
            (stage, error, local_path, book_id),
        )

    def stage(self, book_id: str, stage: str, detail: str = "", local_path: str | None = None) -> None:
        """Track the latest attempt without withdrawing a successfully indexed edition."""
        with self.connect() as conn:
            error = detail if stage in ("failed", "deferred") else None
            self._pipeline_stage(conn, book_id, stage, error, local_path)
            conn.execute(
                "UPDATE books SET status=?,error=?,local_path=COALESCE(?,local_path) WHERE id=? AND status!='indexed'",
                (stage, error, local_path, book_id),
            )
            conn.execute("INSERT INTO events(book_id,stage,detail) VALUES (?,?,?)", (book_id, stage, detail))

    def import_book(self, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        """Index atomically, deduplicate normalized editions, resume safely after failure."""
        from book_rag.artifacts import archive_artifact
        from book_rag.attribution import section_attribution
        from book_rag.parsers import PARSER_FINGERPRINT, parse_book

        book_id = str(metadata["id"]) if metadata.get("id") else None
        fields = ("title", "author", "language", "work_id", "edition", "source_url", "rights", "rights_url")
        attempt_path = str(path.absolute())
        try:
            book_id = self.register(metadata)
            self.stage(book_id, "downloaded", local_path=attempt_path)
            indexer_version = digest(
                PARSER_FINGERPRINT
                + CHUNKER_VERSION
                + (
                    "explicit-contributors-v2-translators"
                    if isinstance(metadata.get("section_attributions"), list)
                    and any(
                        isinstance(rule, dict) and "translator" in rule for rule in metadata["section_attributions"]
                    )
                    else "explicit-contributors-v1"
                )
                + json.dumps(metadata.get("section_attributions", []), ensure_ascii=False, sort_keys=True)
            )
            original_filename = str(metadata.get("original_filename") or path.name)
            metadata = metadata | {"original_filename": original_filename}
            archived = archive_artifact(path, self.data_dir)
            artifact_hash = archived["sha256"]
            path = archived["path"]
            attempt_path = str(path.resolve())
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
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
                    conn.execute("UPDATE books SET local_path=? WHERE id=?", (str(path.resolve()), book_id))
                    self._pipeline_stage(conn, book_id, "indexed", None, attempt_path)
                    conn.execute("INSERT INTO events(book_id,stage) VALUES (?,'indexed')", (book_id,))
                    return {"id": book_id, "status": "indexed", "skipped": True}
            parsed = parse_book(path, fallback_title=Path(original_filename).stem)
            if not parsed.sections or not any(s.text.strip() for s in parsed.sections):
                raise ValueError("No extractable text; scanned PDFs require OCR before import")
            self.stage(book_id, "processed", local_path=attempt_path)
            content_hash = digest("\n\n".join(s.text for s in parsed.sections))
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
                duplicate = conn.execute(
                    "SELECT id FROM books WHERE content_sha256=? AND status='indexed' AND id!=?",
                    (content_hash, book_id),
                ).fetchone()
                self._remove_index(conn, book_id)
                title = metadata.get("title") or parsed.title or path.stem
                author = metadata.get("author") or parsed.author or "Unknown"
                language = metadata.get("language") or parsed.language or "und"
                effective = {key: str(metadata[key] or "") if key in metadata else current[key] for key in fields}
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
                        "UPDATE books SET status='duplicate',content_sha256=?,artifact_sha256=?,local_path=?,error=? "
                        "WHERE id=?",
                        (content_hash, artifact_hash, attempt_path, f"duplicate_of:{duplicate['id']}", book_id),
                    )
                    self._pipeline_stage(conn, book_id, "duplicate", None, attempt_path)
                    conn.execute(
                        "INSERT INTO events(book_id,stage,detail) VALUES (?,'duplicate',?)",
                        (book_id, f"duplicate_of:{duplicate['id']}"),
                    )
                    return {"id": book_id, "status": "duplicate", "duplicate_of": duplicate["id"]}
                for ordinal, section in enumerate(parsed.sections):
                    cursor = conn.execute(
                        "INSERT INTO sections(book_id,ordinal,title,text) VALUES (?,?,?,?)",
                        (book_id, ordinal, section.title, section.text),
                    )
                    section_id = cursor.lastrowid
                    attribution = section_attribution(
                        section.title,
                        metadata
                        | {
                            "author": author,
                            "_artifact_sha256": artifact_hash,
                            "_section_text": section.text,
                            "_section_ordinal": ordinal,
                        },
                    )
                    conn.execute(
                        "INSERT INTO section_roles(section_id,section_author,section_role,attribution_basis,"
                        "section_translator) VALUES (?,?,?,?,?)",
                        (
                            section_id,
                            attribution["author"],
                            attribution["role"],
                            attribution["attribution_basis"],
                            attribution.get("translator", ""),
                        ),
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
                    "UPDATE books SET title=?,author=?,language=?,artifact_sha256=?,content_sha256=?,local_path=?,"
                    "status='indexed',error=NULL,indexed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (title, author, language, artifact_hash, content_hash, attempt_path, book_id),
                )
                conn.execute(
                    "INSERT INTO index_versions(book_id,fingerprint) VALUES (?,?) "
                    "ON CONFLICT(book_id) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (book_id, indexer_version),
                )
                self._pipeline_stage(conn, book_id, "indexed", None, attempt_path)
                conn.execute("INSERT INTO events(book_id,stage) VALUES (?,'indexed')", (book_id,))
            return {"id": book_id, "status": "indexed", "skipped": False}
        except Exception as exc:
            if book_id is not None:
                self.stage(book_id, "failed", str(exc)[:1000], local_path=attempt_path)
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
            conn.execute("BEGIN")
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM books GROUP BY status")}
            return {
                "books": counts.get("indexed", 0),
                "searchable_books": counts.get("indexed", 0),
                "works": conn.execute("SELECT COUNT(DISTINCT work_id) FROM books WHERE status='indexed'").fetchone()[0],
                "passages": conn.execute(
                    "SELECT COUNT(*) FROM passages p JOIN books b ON b.id=p.book_id WHERE b.status='indexed'"
                ).fetchone()[0],
                "status_counts": counts,
                "ingestion_status_counts": {
                    r["stage"]: r["n"]
                    for r in conn.execute(
                        "SELECT COALESCE(i.stage,b.status) stage,COUNT(*) n FROM books b "
                        "LEFT JOIN source_pipeline i ON i.book_id=b.id GROUP BY COALESCE(i.stage,b.status)"
                    )
                },
                "coverage_counts": {
                    r["coverage"]: r["n"]
                    for r in conn.execute(
                        "SELECT COALESCE(json_extract(m.metadata,'$.coverage'),'unspecified') coverage,COUNT(*) n "
                        "FROM books b LEFT JOIN book_metadata m ON m.book_id=b.id WHERE b.status='indexed' "
                        "GROUP BY COALESCE(json_extract(m.metadata,'$.coverage'),'unspecified')"
                    )
                },
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
                "confirmed_full_works_lower_bound": conn.execute(
                    "SELECT COUNT(DISTINCT b.work_id) FROM books b JOIN book_metadata m ON m.book_id=b.id "
                    "WHERE b.status='indexed' AND json_extract(m.metadata,'$.work_identity_status')='curated' "
                    "AND json_extract(m.metadata,'$.coverage')='full' "
                    "AND COALESCE(json_extract(m.metadata,'$.work_type'),'')!='periodical_issue'"
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
        """Return complete metadata for local evaluation/export workflows."""
        return self.book_page(language=language, q=q, limit=None)["books"]

    def book_page(
        self,
        language: str | None = None,
        q: str = "",
        status: str | None = None,
        offset: int = 0,
        limit: int | None = 100,
        ingestion_status: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a bounded catalog page and consistent total without loading all books."""
        if offset < 0 or (limit is not None and not 1 <= limit <= 500):
            raise ValueError("Invalid catalog pagination")
        where = (
            "WHERE (? IS NULL OR b.language=?) AND (?='' OR instr(casefold(b.title),casefold(?))>0 "
            "OR instr(casefold(b.author),casefold(?))>0) AND (? IS NULL OR b.status=?)"
            " AND (? IS NULL OR COALESCE(i.stage,b.status)=?)"
        )
        params = (language, language, q, q, q, status, status, ingestion_status, ingestion_status)
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute(
                "SELECT COUNT(*) FROM books b LEFT JOIN source_pipeline i ON i.book_id=b.id " + where, params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT b.*, (SELECT COUNT(*) FROM sections s WHERE s.book_id=b.id) sections,"
                "(SELECT COUNT(*) FROM passages p WHERE p.book_id=b.id) passages,"
                "json_extract(m.metadata,'$.coverage') coverage,json_extract(m.metadata,'$.work_type') work_type,"
                "json_extract(m.metadata,'$.work_identity_status') work_identity_status,"
                "COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error,"
                "i.local_path ingestion_local_path,i.updated_at ingestion_updated_at "
                "FROM books b LEFT JOIN book_metadata m ON m.book_id=b.id "
                "LEFT JOIN source_pipeline i ON i.book_id=b.id " + where + " ORDER BY b.title,b.id LIMIT ? OFFSET ?",
                (*params, limit if limit is not None else -1, offset),
            ).fetchall()
        return {
            "books": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total,
        }

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT b.*,COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error,"
                "i.local_path ingestion_local_path,i.updated_at ingestion_updated_at "
                "FROM books b LEFT JOIN source_pipeline i ON i.book_id=b.id WHERE b.id=?",
                (book_id,),
            ).fetchone()
            if not row:
                return None
            book = dict(row)
            metadata = conn.execute("SELECT metadata FROM book_metadata WHERE book_id=?", (book_id,)).fetchone()
            book["source_metadata"] = json.loads(metadata[0]) if metadata else {}
            book["artifact_url"] = self.artifact_url(book.get("artifact_sha256"))
            # A shared provisional catalogue ID is not proof of work identity.
            # Offer edition navigation only for links explicitly curated on both sides.
            book["related_editions"] = []
            book["related_editions_total"] = 0
            if book["status"] == "indexed" and book["source_metadata"].get("work_identity_status") == "curated":
                related_where = (
                    "FROM books b JOIN book_metadata m ON m.book_id=b.id "
                    "WHERE b.work_id=? AND b.id!=? AND b.status='indexed' "
                    "AND json_extract(m.metadata,'$.work_identity_status')='curated'"
                )
                book["related_editions_total"] = conn.execute(
                    "SELECT COUNT(*) " + related_where, (book["work_id"], book_id)
                ).fetchone()[0]
                book["related_editions"] = [
                    dict(item)
                    for item in conn.execute(
                        "SELECT b.id,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,"
                        "json_extract(m.metadata,'$.translator') translator,"
                        "json_extract(m.metadata,'$.coverage') coverage "
                        + related_where
                        + " ORDER BY b.language,b.edition,b.id LIMIT 20",
                        (book["work_id"], book_id),
                    )
                ]
            book["sections"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT s.id,s.ordinal,s.title,s.text,a.section_author,a.section_role,a.attribution_basis,"
                    "a.section_translator "
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
        if r.get("ingestion_status") in ("failed", "deferred"):
            r["ingestion_warning"] = (
                "Последняя попытка обновления не завершена; показано последнее успешно проиндексированное издание."
            )
        r["citation"] = (
            f"{r['author']}. {r['title']}. {r['edition']}. {r['section_title']}, chars {r['start']}–{r['end']}."
        )
        if r.get("section_author") and r["section_author"] != r["author"]:
            r["citation"] = f"{r['section_author']} ({r.get('section_role', 'contributor')}). В: " + r["citation"]
        if r.get("section_translator"):
            r["citation"] = f"Перевод фрагмента: {r['section_translator']}. " + r["citation"]
        from urllib.parse import urlencode

        r["reader_url"] = "/?" + urlencode({"book": r["book_id"], "section": r["section_id"], "passage": r["id"]})
        return r

    def passages(self, passage_ids: list[int]) -> list[dict[str, Any]]:
        """Read a bounded candidate batch in input order, verifying exact source slices.

        Missing, nonsearchable or misowned passages are omitted. Other corrupt
        evidence carries verified=False for callers to suppress with a warning.
        """
        if len(passage_ids) > 500 or any(type(identity) is not int or identity < 1 for identity in passage_ids):
            raise ValueError("Expected at most 500 positive integer passage IDs")
        if not passage_ids:
            return []
        unique = list(dict.fromkeys(passage_ids))
        placeholders = ",".join("?" for _ in unique)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
                "b.rights_url,b.artifact_sha256,b.content_sha256,s.title section_title,"
                "substr(s.text,p.start+1,p.end-p.start) section_quote,"
                "a.section_author,a.section_role,a.attribution_basis,a.section_translator,0.0 score,"
                "COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error "
                "FROM passages p JOIN books b ON b.id=p.book_id "
                "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                "LEFT JOIN source_pipeline i ON i.book_id=b.id "
                "LEFT JOIN section_roles a ON a.section_id=s.id "
                f"WHERE b.status='indexed' AND p.id IN ({placeholders})",
                unique,
            ).fetchall()
        found = {row["id"]: self._result(row) for row in rows}
        return [dict(found[identity]) for identity in passage_ids if identity in found]

    def passage(self, passage_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._passage(conn, passage_id)

    def _passage(self, conn: sqlite3.Connection, passage_id: int) -> dict[str, Any] | None:
        """Read evidence inside the caller's transaction when saving a version."""
        row = conn.execute(
            "SELECT p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
            "b.rights_url,b.artifact_sha256,b.content_sha256,s.ordinal section_ordinal,"
            "s.title section_title,s.text section_text,a.section_author,a.section_role,a.attribution_basis,"
            "a.section_translator,"
            "0.0 score,COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error FROM passages p "
            "JOIN books b ON p.book_id=b.id JOIN sections s ON p.section_id=s.id AND s.book_id=p.book_id "
            "LEFT JOIN source_pipeline i ON i.book_id=b.id "
            "LEFT JOIN section_roles a ON a.section_id=s.id WHERE p.id=? AND b.status='indexed'",
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
            ranked = rerank(
                query,
                candidates["results"],
                Path(os.getenv("BOOKRAG_MODEL_CACHE", str(self.data_dir / "models"))).expanduser(),
                limit=limit,
            )
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

            return self._ingestion_warnings(semantic_search(self, query, mode, book_id, language, limit))
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
                envelope = ""
                bounds: tuple[Any, ...] = ()
                if book_id is not None:
                    # Keep the index seeks in this statement's read snapshot:
                    # ingestion may replace passage IDs between separate SELECTs.
                    # The envelope can contain other books; retain exact filters.
                    envelope = (
                        "AND passage_fts.rowid>=COALESCE((SELECT id FROM passages WHERE book_id=? "
                        "ORDER BY id LIMIT 1),0) "
                        "AND passage_fts.rowid<=COALESCE((SELECT id FROM passages WHERE book_id=? "
                        "ORDER BY id DESC LIMIT 1),-1) "
                    )
                    bounds = (book_id, book_id)
                rows = conn.execute(
                    "WITH candidates AS MATERIALIZED ("
                    "SELECT p.id,bm25(passage_fts,1.0,2.0,1.5,1.2) score "
                    "FROM passage_fts JOIN passages p ON p.id=passage_fts.rowid "
                    "JOIN books b ON b.id=p.book_id "
                    "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                    "WHERE passage_fts MATCH ? AND b.status='indexed' "
                    "AND (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) "
                    + envelope
                    + "ORDER BY score,p.id LIMIT ?) "
                    "SELECT p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
                    "b.rights_url,b.artifact_sha256,b.content_sha256,"
                    "s.title section_title,substr(s.text,p.start+1,p.end-p.start) section_quote,"
                    "a.section_author,a.section_role,a.attribution_basis,a.section_translator,"
                    "c.score,"
                    "COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error "
                    "FROM candidates c JOIN passages p ON p.id=c.id JOIN books b ON b.id=p.book_id "
                    "LEFT JOIN source_pipeline i ON i.book_id=b.id "
                    "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                    "LEFT JOIN section_roles a ON a.section_id=s.id "
                    "ORDER BY c.score,p.id",
                    (
                        expression,
                        book_id,
                        book_id,
                        language,
                        language,
                        *bounds,
                        max(200, limit) if mode == "lexical" else limit,
                    ),
                ).fetchall()
                results = [self._result(row) for row in rows]
                if any(not r["verified"] for r in results):
                    warnings.append("Corrupt citation suppressed; rebuild the affected book")
                    results = [r for r in results if r["verified"]]
                if mode == "lexical":
                    results = rerank_results(plan, results, limit=limit)
        return self._ingestion_warnings(
            {
                "query": query,
                "mode": mode,
                "results": results,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "warnings": warnings,
            }
        )

    @staticmethod
    def _ingestion_warnings(response: dict[str, Any]) -> dict[str, Any]:
        warnings = response.setdefault("warnings", [])
        for result in response.get("results", []):
            warning = result.get("ingestion_warning")
            if warning and warning not in warnings:
                warnings.append(warning)
        return response

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
            rows = conn.execute(
                "SELECT s.*,p.metadata provenance_json FROM saved s LEFT JOIN saved_provenance p ON p.item_id=s.id "
                "ORDER BY s.id DESC"
            ).fetchall()
        return [self._saved_item(dict(row)) for row in rows]

    def get_saved(self, item_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT s.*,p.metadata provenance_json FROM saved s LEFT JOIN saved_provenance p ON p.item_id=s.id "
                "WHERE s.id=?",
                (item_id,),
            ).fetchone()
        return self._saved_item(dict(row)) if row else None

    def _saved_item(self, row: dict[str, Any]) -> dict[str, Any]:
        row["text"] = row.pop("snapshot_text")
        row["citation"] = row.pop("snapshot_citation")
        row["provenance"] = json.loads(row.pop("provenance_json") or "{}")
        row["artifact_url"] = self.artifact_url(row["provenance"].get("artifact_sha256"))
        if re.fullmatch(r"cv1-[a-f0-9]{64}", str(row["provenance"].get("canonical_version_id", ""))):
            row["historical_reader_url"] = f"/?saved={row['id']}"
        if row["passage_id"] is not None:
            current = self.passage(row["passage_id"])
            if (
                current
                and current["verified"]
                and current["text"] == row["text"]
                and current["citation"] == row["citation"]
            ):
                row["reader_url"] = current["reader_url"]
        return row

    def update_saved(self, item_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        """Edit a title, note or collection while preserving frozen evidence."""
        limits = {"title": 500, "note": 10000, "collection": 200}
        if not changes or set(changes) - limits.keys():
            raise ValueError("Only title, note and collection can be edited")
        if any(not isinstance(value, str) or len(value) > limits[key] for key, value in changes.items()):
            raise ValueError("Edited fields must be strings within their length limits")
        # SQL identifiers come exclusively from the fixed whitelist above.
        fields = sorted(changes)
        with self.connect() as conn:
            result = conn.execute(
                "UPDATE saved SET " + ",".join(f"{field}=?" for field in fields) + " WHERE id=?",
                (*[changes[field] for field in fields], item_id),
            )
        return self.get_saved(item_id) if result.rowcount else None

    def save(self, item: dict[str, Any]) -> dict[str, Any]:
        kind = item.get("kind")
        if kind not in ("passage", "query"):
            raise ValueError("kind must be passage or query")
        if kind == "query" and not str(item.get("query", "")).strip():
            raise ValueError("A nonempty query is required")
        with self.connect() as conn:
            # Keep passage, edition, chapter metadata and its extraction identity
            # coherent while a concurrent ingestion process replaces a book.
            conn.execute("BEGIN IMMEDIATE")
            passage = self._passage(conn, item["passage_id"]) if kind == "passage" and item.get("passage_id") else None
            if kind == "passage" and (not passage or not passage["verified"]):
                raise ValueError("A verified passage is required")
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
            if new_id is None:
                raise RuntimeError("Saved record did not receive an identifier")
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
                    "section_ordinal",
                    "section_title",
                    "section_author",
                    "section_role",
                    "section_translator",
                    "attribution_basis",
                    "start",
                    "end",
                    "artifact_sha256",
                    "content_sha256",
                )
                provenance = {key: passage.get(key) for key in fields}
                try:
                    provenance["canonical_version_id"] = self._preserve_saved_book(conn, passage)
                except (OSError, ValueError) as exc:
                    # A full disk or unavailable historical store must not lose
                    # the user's verified excerpt and annotation.
                    provenance["historical_context_error"] = str(exc)[:500]
                conn.execute(
                    "INSERT INTO saved_provenance(item_id,metadata) VALUES (?,?)",
                    (new_id, json.dumps(provenance, ensure_ascii=False)),
                )
        saved = self.get_saved(new_id)
        if saved is None:
            raise RuntimeError("Saved record disappeared before it could be read")
        return saved

    def _preserve_saved_book(self, conn: sqlite3.Connection, passage: dict[str, Any]) -> str:
        import shutil

        from book_rag.canonical_versions import MAX_CANONICAL_BYTES, preserve_canonical
        from book_rag.parsers import ParsedBook, Section

        if shutil.disk_usage(self.data_dir).free < 4 * 1024**3 + MAX_CANONICAL_BYTES:
            raise OSError("Insufficient free disk for historical context; the quotation and note are retained")
        rows = conn.execute(
            "SELECT s.title,s.text,a.section_author,a.section_role,a.section_translator,a.attribution_basis "
            "FROM sections s LEFT JOIN section_roles a ON a.section_id=s.id WHERE s.book_id=? ORDER BY s.ordinal",
            (passage["book_id"],),
        ).fetchall()
        version = conn.execute(
            "SELECT fingerprint FROM index_versions WHERE book_id=?", (passage["book_id"],)
        ).fetchone()
        result = preserve_canonical(
            ParsedBook(
                passage["title"], passage["author"], passage["language"], [Section(r["title"], r["text"]) for r in rows]
            ),
            self.data_dir,
            artifact_sha256=passage["artifact_sha256"],
            indexer_fingerprint=version[0] if version else None,
            edition=passage["edition"],
            source_url=passage["source_url"],
            rights=passage["rights"],
            rights_url=passage["rights_url"],
            section_metadata=[
                {
                    key: r[key] or ""
                    for key in ("section_author", "section_role", "section_translator", "attribution_basis")
                }
                for r in rows
            ],
        )
        return result["version_id"]

    def saved_context(self, item_id: int, ordinal: int | None = None) -> dict[str, Any] | None:
        """Read one exact historical chapter, independently of the current index."""
        from book_rag.canonical_versions import read_canonical_manifest, read_canonical_section

        with self.connect() as conn:
            row = conn.execute(
                "SELECT s.*,p.metadata FROM saved s JOIN saved_provenance p ON p.item_id=s.id "
                "WHERE s.id=? AND s.kind='passage'",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        provenance = json.loads(row["metadata"])
        version_id = provenance.get("canonical_version_id")
        if not version_id:
            raise FileNotFoundError("Historical chapter was not archived; the saved quotation is retained")
        selected = provenance.get("section_ordinal") if ordinal is None else ordinal
        if type(selected) is not int or selected < 0:
            raise ValueError("Historical chapter ordinal is invalid")
        manifest = read_canonical_manifest(self.data_dir, version_id)
        if manifest["artifact_sha256"] != provenance.get("artifact_sha256"):
            raise ValueError("Historical source differs from saved provenance")
        chapter = read_canonical_section(self.data_dir, version_id, selected)["section"]
        section_id = f"canonical-{selected}"
        quote = None
        if selected == provenance.get("section_ordinal"):
            start, end = provenance.get("start"), provenance.get("end")
            if not (
                type(start) is int
                and type(end) is int
                and 0 <= start < end <= len(chapter["text"])
                and chapter["text"][start:end] == row["snapshot_text"]
            ):
                raise ValueError("Saved quotation differs from its historical chapter")
            quote = provenance | {
                "id": None,
                "section_id": section_id,
                "text": row["snapshot_text"],
                "citation": row["snapshot_citation"],
                "verified": True,
                "historical": True,
            }
        sections = [
            {"id": f"canonical-{s['ordinal']}", "ordinal": s["ordinal"], "title": s["title"], **s["metadata"]}
            for s in manifest["sections"]
        ]
        sections[selected] |= {"text": chapter["text"]}
        book = manifest["book"] | {
            "id": provenance["book_id"],
            "historical_saved_id": item_id,
            "canonical_version_id": version_id,
            "historical_saved_at": row["created_at"],
            "status": "archived",
            "artifact_sha256": manifest["artifact_sha256"],
            "artifact_url": self.artifact_url(manifest["artifact_sha256"]),
            "source_metadata": {"coverage": "saved_canonical_version"},
            "sections": sections,
            "related_editions": [],
        }
        return {"book": book, "section": sections[selected], "passage": quote}

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

    def artifact_url(self, sha256: str | None) -> str | None:
        """Expose an available archive locator; downloads independently verify bytes."""
        from book_rag.artifacts import SUPPORTED_SUFFIXES

        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            return None
        shard = self.data_dir / "artifacts" / sha256[:2]
        if any(
            (shard / f"{sha256}{suffix}").is_file() and not (shard / f"{sha256}{suffix}").is_symlink()
            for suffix in SUPPORTED_SUFFIXES
        ):
            return f"/api/artifacts/{sha256}"
        return None
