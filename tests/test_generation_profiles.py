"""Reviewed profile selection stays offline and cannot redirect inference downloads."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from book_rag import local_generation as generation
from book_rag.generation_profiles import DEFAULT_PROFILE, PROFILES, generation_profile


def test_default_artifact_remains_identical_and_registry_is_immutable() -> None:
    profile = generation_profile()
    assert profile.key == DEFAULT_PROFILE
    assert profile.sha256 == "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
    assert profile.bytes == 1_117_320_736
    with pytest.raises(FrozenInstanceError):
        profile.__setattr__("repository", "unreviewed")
    with pytest.raises(TypeError):
        cast(Any, PROFILES)["unreviewed"] = profile
    with pytest.raises(ValueError):
        generation_profile("https://example.org/arbitrary.gguf")


def test_converted_candidate_retains_base_author_and_actual_conversion_provenance() -> None:
    profile = generation_profile("qwen3-4b-instruct-2507")
    assert profile.repository.startswith("unsloth/")
    assert profile.upstream_repository == "Qwen/Qwen3-4B-Instruct-2507"
    assert profile.reviewed_upstream_revision in profile.license_url
    assert profile.bytes == 2_497_281_120 and profile.license == "Apache-2.0"
    assert "not an official Qwen GGUF release" in profile.conversion


def test_selected_profile_uses_separate_cache_and_pinned_hash_without_loading_native_runtime() -> None:
    code = """
import json,sys
from book_rag import local_generation as g
print(json.dumps({'repository':g.MODEL_REPOSITORY,'sha256':g.MODEL_SHA256,
 'directory':str(g.DEFAULT_MODEL_DIRECTORY),'native_loaded':'llama_cpp' in sys.modules}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=os.environ | {"BOOKRAG_GENERATION_PROFILE": "qwen3-4b-instruct-2507"},
    )
    data = json.loads(result.stdout)
    assert data["repository"] == "unsloth/Qwen3-4B-Instruct-2507-GGUF"
    assert data["sha256"] == "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
    assert data["directory"].endswith("/qwen3-4b-instruct-2507") and not data["native_loaded"]


def test_bad_optional_profile_keeps_search_api_importable_and_generation_fails_lazily(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
from book_rag.api import create_app
from book_rag.local_generation import LocalGenerator,ModelUnavailable,capabilities
app=create_app(Path(sys.argv[1]))
assert app.state.library.stats()['books']==0
assert capabilities()['profile_supported'] is False
try:LocalGenerator()
except ModelUnavailable:pass
else:raise AssertionError('Unknown profile must not silently select a different model')
"""
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "library")],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=os.environ | {"BOOKRAG_GENERATION_PROFILE": "unreviewed-profile"},
    )


def test_native_worker_uses_parent_pins_even_when_its_module_defaults_differ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"GGUF" + b"original CC0 invented model fixture"
    path = tmp_path / "fixture.gguf"
    path.write_bytes(data)
    settings = {
        "model_path": str(path),
        "model_stamp": generation._model_stamp(path, expected_bytes=len(data)),
        "model_bytes": len(data),
        "model_sha256": hashlib.sha256(data).hexdigest(),
        "context_tokens": 512,
        "n_threads": 1,
        "gpu_layers": 0,
    }
    monkeypatch.setattr(generation, "MODEL_BYTES", len(data) + 1)
    monkeypatch.setattr(generation, "MODEL_SHA256", "0" * 64)
    loaded: list[str] = []

    def load(**kwargs: Any) -> Any:
        loaded.append(kwargs["model_path"])
        return SimpleNamespace(metadata={"tokenizer.chat_template": "<|im_start|><|im_end|>"}, close=lambda: None)

    monkeypatch.setitem(
        sys.modules,
        "llama_cpp",
        SimpleNamespace(
            __version__=generation.RUNTIME_VERSION,
            Llama=load,
            llama_supports_gpu_offload=lambda: False,
        ),
    )
    sent: list[Any] = []
    channel = SimpleNamespace(recv=lambda: None, send=sent.append, close=lambda: None)
    generation._worker_main(cast(generation.Connection, channel), settings)
    assert loaded == [str(path)] and not sent
