"""Runner preparation/profile/gate tests have no model or accelerator work."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

from book_rag.embedding_profiles import PROFILES, profile_for_model, profile_from_metadata

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/build_e5_base_experiment.py"))


def gate(directory: Path, *, complete: bool = True, exercised: bool = True) -> None:
    directory.mkdir()
    dataset = "a" * 64
    (directory / "state.json").write_text(
        json.dumps(
            {
                "status": "complete" if complete else "running",
                "validated": complete,
                "dataset_sha256": dataset,
            }
        )
    )
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "semantic_candidate_exercised": exercised,
                "dataset_sha256": dataset,
            }
        )
    )


def test_independent_owned_copy_exact_bytes_resume_and_no_clobber(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "library.sqlite3"
    original.write_bytes(b"Owned copy fixture; generic capture helper only, no SQLite/model run.")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    target = tmp_path / "owned"
    receipt = RUNNER["prepare_copy"](source, target, digest)
    assert receipt["source_database_sha256"] == digest == receipt["copied_database_sha256"]
    assert receipt["source_identity"][:2] != receipt["copied_identity"][:2]
    assert RUNNER["prepare_copy"](source, target, digest) == json.loads((target / RUNNER["MARKER"]).read_text())
    (target / "library.sqlite3").write_bytes(b"Changed only the owned copy")
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="ownership/profile/database"):
        RUNNER["prepare_copy"](source, target, digest)
    unrelated = tmp_path / "unowned"
    unrelated.mkdir()
    with pytest.raises(ValueError, match="unowned target"):
        RUNNER["prepare_copy"](source, unrelated, digest)
    with pytest.raises(ValueError, match="distinct owned"):
        RUNNER["prepare_copy"](source, source / "nested", digest)


@pytest.mark.parametrize("complete,exercised", [(False, False), (False, True), (True, False)])
def test_unvalidated_or_fallback_gate_refuses_before_runtime_import(
    tmp_path: Path, complete: bool, exercised: bool
) -> None:
    directory = tmp_path / "gate"
    gate(directory, complete=complete, exercised=exercised)
    with patch("importlib.import_module", side_effect=AssertionError("No model runtime before valid gate")):
        with pytest.raises(ValueError, match="Gate pending"):
            RUNNER["build"](tmp_path / "absent-source", tmp_path / "target", directory, tmp_path / "cache")
    assert not (tmp_path / "target").exists()


def test_completed_gate_binds_same_dataset_and_file_receipts(tmp_path: Path) -> None:
    directory = tmp_path / "gate"
    gate(directory)
    receipt = RUNNER["validated_gate"](directory)
    assert receipt["validated"] and receipt["semantic_candidate_exercised"]
    assert receipt["state_sha256"] == hashlib.sha256((directory / "state.json").read_bytes()).hexdigest()
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "semantic_candidate_exercised": True,
                "dataset_sha256": "b" * 64,
            }
        )
    )
    with pytest.raises(ValueError, match="Gate pending"):
        RUNNER["validated_gate"](directory)


def test_process_local_profile_restores_registry_env_and_keeps_stored_query_contract(tmp_path: Path) -> None:
    profile = RUNNER["PROFILE"]
    previous = PROFILES.get(profile.name)
    previous_offline = os.environ.get("HF_HUB_OFFLINE")
    with RUNNER["experimental_profile"](tmp_path):
        assert profile_for_model(RUNNER["MODEL"], profile.name) == profile
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == "0.65"
        assert os.environ["PYTORCH_MPS_LOW_WATERMARK_RATIO"] == "0.5"
        stored = profile.metadata(RUNNER["REVISION"])
    assert PROFILES.get(profile.name) == previous
    assert os.environ.get("HF_HUB_OFFLINE") == previous_offline
    parsed = profile_from_metadata(
        {
            "model": RUNNER["MODEL"],
            "model_revision": RUNNER["REVISION"],
            "embedding_profile": stored,
        }
    )
    assert parsed.query_input("Что ищет читатель?") == "query: Что ищет читатель?"
    assert parsed.document_prefix == "passage: " and parsed.max_tokens == 512


def test_telemetry_ignores_other_profile_checkpoints(tmp_path: Path) -> None:
    vectors = tmp_path / "vectors"
    vectors.mkdir()
    (vectors / "active.json").write_text(json.dumps({"vectors": 999, "embedding_profile": {"name": "unrelated"}}))
    assert not RUNNER["checkpoint_progress"](tmp_path)["ready"]
    stage = vectors / "staging-owned"
    stage.mkdir()
    (stage / "checkpoint.json").write_text(
        json.dumps(
            {
                "completed_vectors": 24,
                "descriptor": {"embedding_profile": RUNNER["PROFILE"].metadata(RUNNER["REVISION"]), "vectors": 100},
            }
        )
    )
    progress = RUNNER["checkpoint_progress"](tmp_path)
    assert progress["committed_vectors"] == 24 and progress["planned_vectors"] == 100 and not progress["ready"]
