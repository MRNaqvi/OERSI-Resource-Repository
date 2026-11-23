"""Safe normalization helpers for OERSI JSON-LD metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote


def canonical_json(value: Any) -> str:
    """Serialize JSON with sorted object keys and compact separators."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def metadata_hash(value: Any) -> str:
    """Hash JSON content without expanding or canonicalizing its RDF meaning."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _localized_text(value: Any, languages: tuple[str, ...] = ("en", "de")) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for language in languages:
            candidate = value.get(language)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _as_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _labels(value: Any, languages: tuple[str, ...] = ("en", "de")) -> list[str]:
    labels: list[str] = []
    for item in _as_items(value):
        if isinstance(item, Mapping):
            label = _localized_text(item.get("prefLabel") or item.get("name"), languages)
            if label is None:
                label = _localized_text(item.get("id"), languages)
        else:
            label = _localized_text(item, languages)
        if label and label not in labels:
            labels.append(label)
    return labels


def _people(value: Any) -> list[str]:
    people: list[str] = []
    for item in _as_items(value):
        name = _localized_text(item.get("name")) if isinstance(item, Mapping) else _localized_text(item)
        if name and name not in people:
            people.append(name)
    return people


def _first_url(metadata: Mapping[str, Any]) -> str | None:
    entity_pages = _as_items(metadata.get("mainEntityOfPage"))
    for page in entity_pages:
        if isinstance(page, Mapping):
            url = page.get("id") or page.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    url = metadata.get("url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def _source_providers(metadata: Mapping[str, Any]) -> list[str]:
    providers: list[str] = []
    pages = _as_items(metadata.get("mainEntityOfPage"))
    candidates: list[Any] = [metadata.get("provider")]
    candidates.extend(page.get("provider") for page in pages if isinstance(page, Mapping))
    for provider in candidates:
        for item in _as_items(provider):
            name = _localized_text(item.get("name")) if isinstance(item, Mapping) else _localized_text(item)
            if name and name not in providers:
                providers.append(name)
    return providers


def stable_resource_id(oersi_id: str) -> str:
    """Derive a deterministic filesystem-safe ID from the OERSI identifier."""
    digest = hashlib.sha256(oersi_id.encode("utf-8")).hexdigest()[:20]
    return f"oersi-{digest}"


def normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Extract application fields without copying or mutating source metadata."""
    oersi_id = metadata.get("id")
    if not isinstance(oersi_id, str) or not oersi_id.strip():
        raise ValueError("OERSI metadata has no usable 'id' identifier")
    oersi_id = oersi_id.strip()

    resource_types = []
    for item in _as_items(metadata.get("learningResourceType")):
        if isinstance(item, Mapping):
            resource_types.append(
                {
                    "id": item.get("id") if isinstance(item.get("id"), str) else None,
                    "label": _localized_text(item.get("prefLabel")),
                }
            )
        elif isinstance(item, str):
            resource_types.append({"id": item, "label": None})

    license_value = metadata.get("license")
    license_id: str | None = None
    if isinstance(license_value, Mapping):
        raw_license = license_value.get("id") or license_value.get("name")
        license_id = raw_license.strip() if isinstance(raw_license, str) and raw_license.strip() else None
    elif isinstance(license_value, str):
        license_id = license_value.strip() or None

    return {
        "stable_id": stable_resource_id(oersi_id),
        "oersi_id": oersi_id,
        "title": _localized_text(metadata.get("name")),
        "description": _localized_text(metadata.get("description")),
        "resource_type": resource_types,
        "keywords": _labels(metadata.get("keywords")),
        "subject": _labels(metadata.get("about")),
        "creator": _people(metadata.get("creator") or metadata.get("author")),
        "publisher": _people(metadata.get("publisher")),
        "language": _labels(metadata.get("inLanguage")),
        "license": license_id,
        "source_url": _first_url(metadata),
        "publication_date": _localized_text(metadata.get("datePublished")),
        "modification_date": _localized_text(metadata.get("dateModified")),
        "source_provider": _source_providers(metadata),
    }


def filename_for(stable_id: str) -> str:
    """Return a safe JSON filename for a stable resource identifier."""
    return f"{quote(stable_id, safe='-_.')}.json"
