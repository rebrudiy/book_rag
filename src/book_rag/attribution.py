"""Conservative contributor attribution from explicit, source-audited rules.

A heading such as Introduction, Preface or Notes does not establish authorship.
Default sections retain the book's author as catalog context but are explicitly
unclassified. Only a matching metadata rule verifies a section contributor.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

EXPLICIT_ROLES = {"main_text", "editorial"}


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def section_attribution(title: str, metadata: dict[str, Any]) -> dict[str, str]:
    """Resolve one section using explicit ``metadata.section_attributions``.

    Each rule has nonempty ``title_contains``, ``author`` and a ``role`` of
    ``main_text`` or ``editorial``. Matching is Unicode-normalized, casefolded
    and whitespace-insensitive. Conflicting applicable rules return an
    unclassified result instead of guessing between contributors. Malformed
    rules raise ValueError so invalid source metadata cannot silently become
    verified attribution.
    """
    book_author = str(metadata.get("author") or "Unknown")
    default = {"role": "unclassified", "author": book_author, "attribution_basis": "book_metadata_unclassified"}
    rules = metadata.get("section_attributions", [])
    if not isinstance(rules, list):
        raise ValueError("section_attributions must be a list of explicit contributor rules")
    normalized_title = _normalized(title)
    matches: list[tuple[int, str, str, str]] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"section_attributions[{index}] must be an object")
        for field in ("title_contains", "author", "role"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise ValueError(f"section_attributions[{index}].{field} must be a nonempty string")
        expected_artifact = rule.get("source_artifact_sha256")
        if expected_artifact is not None:
            if not isinstance(expected_artifact, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_artifact):
                raise ValueError(f"section_attributions[{index}].source_artifact_sha256 must be a SHA-256 digest")
        if rule["role"] not in EXPLICIT_ROLES:
            raise ValueError(f"section_attributions[{index}].role must be main_text or editorial")
        translator = rule.get("translator", "")
        if "translator" in rule and (not isinstance(translator, str) or not translator.strip()):
            raise ValueError(f"section_attributions[{index}].translator must be a nonempty string")
        required_ordinal = rule.get("section_ordinal")
        if required_ordinal is not None and (type(required_ordinal) is not int or required_ordinal < 0):
            raise ValueError(f"section_attributions[{index}].section_ordinal must be a nonnegative integer")
        required_text = rule.get("text_contains")
        if required_text is not None and (not isinstance(required_text, str) or not required_text.strip()):
            raise ValueError(f"section_attributions[{index}].text_contains must be nonempty text")
        if required_ordinal is not None and metadata.get("_section_ordinal") != required_ordinal:
            continue
        if required_text is not None and _normalized(required_text) not in _normalized(
            str(metadata.get("_section_text", ""))
        ):
            continue
        if expected_artifact is not None and metadata.get("_artifact_sha256") != expected_artifact:
            continue
        if _normalized(rule["title_contains"]) in normalized_title:
            matches.append((index, rule["author"].strip(), rule["role"], translator.strip()))
    if not matches:
        return default
    identities = {(_normalized(author), role) for _, author, role, _ in matches}
    translators = {_normalized(translator) for _, _, _, translator in matches if translator}
    if len(identities) > 1 or len(translators) > 1:
        return {**default, "attribution_basis": "conflicting_explicit_section_attributions"}
    indices = ",".join(str(index) for index, _, _, _ in matches)
    _, author, role, _ = matches[0]
    result = {"role": role, "author": author, "attribution_basis": f"explicit_section_attributions[{indices}]"}
    if translators:
        result["translator"] = next(translator for _, _, _, translator in matches if translator)
    return result
