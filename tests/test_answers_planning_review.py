"""Independent owned CC0 review regressions; no model or sealed fixture access."""

from pathlib import Path
from typing import Any

from test_answers_planning import META, TEXT, PlannedFake

from book_rag.answers import ask
from book_rag.library import Library
from book_rag.local_generation import ContextLimitExceeded


def test_opt_in_without_language_filter_rejects_language_identity_change_during_writer(tmp_path: Path) -> None:
    library = Library(tmp_path / "owned")
    text = tmp_path / "cc0.txt"
    text.write_text(TEXT)
    library.import_book(text, META)

    def change() -> None:
        with library.connect() as conn:
            conn.execute("UPDATE books SET language='ru' WHERE id=?", (META["id"],))

    fake = PlannedFake(on_writer=change)
    response = ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert response["status"] == "sources_changed"
    assert not response["sources"] and not response["claims"]
    assert [call["kind"] for call in fake.calls] == ["plan", "writer"]


def test_final_evidence_scope_metadata_matches_sources_after_context_reduction(tmp_path: Path) -> None:
    library = Library(tmp_path / "owned")
    for identity, text in (
        (META["id"], TEXT),
        ("second", "A second amber bookmark is placed beside the blue archive."),
    ):
        path = tmp_path / (identity + ".txt")
        path.write_text(text)
        library.import_book(path, META | {"id": identity, "title": identity})

    class SmallContext(PlannedFake):
        def generate(self, messages: Any, limits: Any = None, *, response_schema: Any = None) -> dict[str, Any]:
            import json

            payload = json.loads(messages[-1]["content"])
            if "sources" in payload and payload.get("task") != "verify_claims" and len(payload["sources"]) > 1:
                raise ContextLimitExceeded("Owned two-book context fixture")
            return super().generate(messages, limits, response_schema=response_schema)

    result = ask(
        library,
        "amber bookmark",
        book_ids=[META["id"], "second"],
        mode="lexical",
        generator=SmallContext(),
        plan_retrieval=True,
    )
    assert result["status"] == "answered" and len(result["sources"]) == 1
    assert result["retrieval"]["evidence_book_ids"] == list(
        dict.fromkeys(source["book_id"] for source in result["sources"])
    )
