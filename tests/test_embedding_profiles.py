"""Embedding input contracts must stay pinned and independent of runtime env."""

from dataclasses import replace
from typing import Any

import pytest

from book_rag.embedding_profiles import (
    E5_SMALL_MODEL,
    E5_SMALL_PROFILE,
    E5_SMALL_REVISION,
    MINILM_MODEL,
    MINILM_PROFILE,
    PROFILES,
    profile_for_model,
    profile_from_metadata,
)


def e5_metadata() -> dict[str, Any]:
    return {
        "model": E5_SMALL_MODEL,
        "model_revision": E5_SMALL_REVISION,
        "embedding_profile": E5_SMALL_PROFILE.metadata(E5_SMALL_REVISION),
    }


def test_registered_profiles_pin_trained_input_contracts() -> None:
    assert profile_for_model(MINILM_MODEL) == MINILM_PROFILE
    profile = profile_for_model(E5_SMALL_MODEL)
    assert profile == E5_SMALL_PROFILE
    assert profile.max_tokens == 512
    assert profile.revision == E5_SMALL_REVISION
    assert profile.document_prefix == "passage: "
    assert profile.query_input("  Где начинается река? ") == "query:   Где начинается река? "
    assert MINILM_PROFILE.query_input("unchanged query") == "unchanged query"


def test_explicit_profile_rejects_wrong_model_and_unknown_profile() -> None:
    with pytest.raises(ValueError, match="requires model"):
        profile_for_model(MINILM_MODEL, "multilingual-e5-small")
    with pytest.raises(ValueError, match="Unknown"):
        profile_for_model(E5_SMALL_MODEL, "unregistered")
    custom = profile_for_model("my/custom-model")
    assert custom.query_prefix == custom.document_prefix == ""
    assert custom.revision is None


def test_stored_metadata_controls_query_even_after_registry_or_env_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = e5_metadata()
    monkeypatch.setenv("BOOKRAG_EMBEDDING_PROFILE", "minilm-v2")
    monkeypatch.setitem(PROFILES, "multilingual-e5-small", replace(E5_SMALL_PROFILE, query_prefix="future query: "))
    assert profile_from_metadata(metadata).query_input("question") == "query: question"


def test_legacy_profile_is_unprefixed_without_guessing_from_model_name() -> None:
    assert profile_from_metadata({"model": MINILM_MODEL}).query_input("query") == "query"
    assert profile_from_metadata({"model": E5_SMALL_MODEL}).query_input("query") == "query"


@pytest.mark.parametrize(
    "change",
    [
        {"model": MINILM_MODEL},
        {"revision": "wrong"},
        {"query_prefix": 42},
        {"max_tokens": True},
        {"max_tokens": 20},
        {"document_prefix": None},
    ],
)
def test_invalid_stored_profile_is_rejected(change: dict[str, Any]) -> None:
    metadata = e5_metadata()
    metadata["embedding_profile"].update(change)
    with pytest.raises(ValueError):
        profile_from_metadata(metadata)
