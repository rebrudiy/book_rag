"""Reviewed local artifacts; selection is configuration, never a request-supplied URL."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    key: str
    repository: str
    revision: str
    filename: str
    sha256: str
    bytes: int
    upstream_repository: str
    reviewed_upstream_revision: str
    license_filename: str
    license_url: str
    license_sha256: str = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
    license: str = "Apache-2.0"
    conversion: str = "official Qwen GGUF"


DEFAULT_PROFILE = "qwen2.5-1.5b"
PROFILES = MappingProxyType(
    {
        DEFAULT_PROFILE: GenerationProfile(
            key=DEFAULT_PROFILE,
            repository="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
            revision="91cad51170dc346986eccefdc2dd33a9da36ead9",
            filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
            sha256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
            bytes=1_117_320_736,
            upstream_repository="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
            reviewed_upstream_revision="91cad51170dc346986eccefdc2dd33a9da36ead9",
            license_filename="Qwen2.5-1.5B-Instruct.LICENSE",
            license_url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/"
            "91cad51170dc346986eccefdc2dd33a9da36ead9/LICENSE",
        ),
        "qwen3-4b-instruct-2507": GenerationProfile(
            key="qwen3-4b-instruct-2507",
            repository="unsloth/Qwen3-4B-Instruct-2507-GGUF",
            revision="a06e946bb6b655725eafa393f4a9745d460374c9",
            filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            sha256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
            bytes=2_497_281_120,
            upstream_repository="Qwen/Qwen3-4B-Instruct-2507",
            reviewed_upstream_revision="cdbee75f17c01a7cc42f958dc650907174af0554",
            license_filename="Qwen3-4B-Instruct-2507.LICENSE",
            license_url="https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/resolve/"
            "cdbee75f17c01a7cc42f958dc650907174af0554/LICENSE",
            conversion="Unsloth GGUF conversion of Qwen weights; not an official Qwen GGUF release",
        ),
    }
)


def generation_profile(key: str = DEFAULT_PROFILE) -> GenerationProfile:
    try:
        return PROFILES[key]
    except KeyError as error:
        raise ValueError("Unknown local generation profile") from error
