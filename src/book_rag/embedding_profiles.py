"""Versioned embedding input contracts; retrieval scores are not probabilities.

Profiles describe trained model inputs, not query-specific tuning. Index metadata
owns the query contract after activation, independently of current environment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

MINILM_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MINILM_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
E5_SMALL_MODEL = "intfloat/multilingual-e5-small"
E5_SMALL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """A pinned model's asymmetric retrieval prefixes and token-window contract."""

    name: str
    model: str
    revision: str | None = None
    query_prefix: str = ""
    document_prefix: str = ""
    max_tokens: int | None = None
    strategy: str = "token-windows-v2"

    def metadata(self, resolved_revision: str) -> dict[str, Any]:
        """Freeze the actual revision with all input fields in the index identity."""
        return asdict(self) | {"revision": resolved_revision}

    def query_input(self, text: str) -> str:
        return self.query_prefix + text


MINILM_PROFILE = EmbeddingProfile("minilm-v2", MINILM_MODEL, MINILM_REVISION)
E5_SMALL_PROFILE = EmbeddingProfile(
    "multilingual-e5-small",
    E5_SMALL_MODEL,
    E5_SMALL_REVISION,
    query_prefix="query: ",
    document_prefix="passage: ",
    max_tokens=512,
    strategy="token-windows-e5-v1",
)
PROFILES = {profile.name: profile for profile in (MINILM_PROFILE, E5_SMALL_PROFILE)}


def profile_for_model(model: str, explicit_profile: str | None = None) -> EmbeddingProfile:
    """Choose a registered contract, rejecting incompatible explicit combinations."""
    if explicit_profile:
        if explicit_profile not in PROFILES:
            raise ValueError(f"Unknown embedding profile: {explicit_profile}")
        profile = PROFILES[explicit_profile]
        if profile.model != model:
            raise ValueError(f"Embedding profile {profile.name} requires model {profile.model}")
        return profile
    for profile in PROFILES.values():
        if profile.model == model:
            return profile
    return EmbeddingProfile("unprefixed-v2", model)


def profile_from_metadata(metadata: dict[str, Any]) -> EmbeddingProfile:
    """Use frozen prefixes, treating pre-profile active indexes as unprefixed."""
    value = metadata.get("embedding_profile")
    if value is None:
        return EmbeddingProfile("legacy-unprefixed-v2", metadata["model"], metadata.get("model_revision"))
    if not isinstance(value, dict):
        raise ValueError("Invalid embedding profile metadata")
    for field in ("name", "model", "revision", "query_prefix", "document_prefix", "strategy"):
        if not isinstance(value.get(field), str):
            raise ValueError(f"Invalid embedding profile field: {field}")
    maximum = value.get("max_tokens")
    if maximum is not None and (type(maximum) is not int or maximum < 32):
        raise ValueError("Invalid embedding profile token limit")
    if value["model"] != metadata["model"] or value["revision"] != metadata.get("model_revision"):
        raise ValueError("Embedding profile model/revision differs from the active index")
    return EmbeddingProfile(
        name=value["name"],
        model=value["model"],
        revision=value["revision"],
        query_prefix=value["query_prefix"],
        document_prefix=value["document_prefix"],
        max_tokens=maximum,
        strategy=value["strategy"],
    )
