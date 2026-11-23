"""JSON-LD 1.1 validation using PyLD and a cacheable context loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests
from pyld import jsonld

OFFICIAL_AMB_CONTEXT_URL = "https://w3id.org/kim/amb/context.jsonld"


class JsonLdContextLoader:
    """Fetch JSON-LD contexts with a timeout, response cache, and error record."""

    def __init__(self, timeout: float = 15.0, session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self._cache: dict[str, dict[str, Any]] = {}
        self.failures: list[dict[str, str]] = []

    def __call__(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if url in self._cache:
            return self._cache[url]
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            self.failures.append({"url": url, "kind": "network_or_http", "message": str(exc)})
            raise
        try:
            document = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            self.failures.append({"url": url, "kind": "invalid_context_document", "message": str(exc)})
            raise
        if not isinstance(document, (dict, list)):
            message = "Remote JSON-LD context must be a JSON object or array"
            self.failures.append({"url": url, "kind": "invalid_context_document", "message": message})
            raise ValueError(message)

        loaded = {
            "contextUrl": response.headers.get("Link"),
            "documentUrl": response.url,
            "document": document,
        }
        self._cache[url] = loaded
        self._cache[response.url] = loaded
        return loaded


def _context_contains(context: Any, context_url: str) -> bool:
    if context == context_url:
        return True
    if isinstance(context, list):
        return any(_context_contains(item, context_url) for item in context)
    if isinstance(context, dict):
        return any(_context_contains(value, context_url) for value in context.values())
    return False


def _record_expansion_error(
    result: dict[str, Any], exc: Exception, loader: Any, failure_count_before: int
) -> dict[str, Any]:
    failures = getattr(loader, "failures", [])[failure_count_before:]
    network_failures = [failure for failure in failures if failure.get("kind") == "network_or_http"]
    if network_failures:
        result["status"] = "context_resolution_failure"
        result["context_resolution_errors"] = network_failures
        return result

    invalid_context_failures = [
        failure for failure in failures if failure.get("kind") == "invalid_context_document"
    ]
    result["errors"].append(f"JSON-LD expansion failed: {type(exc).__name__}: {exc}")
    result["errors"].extend(
        f"Invalid remote context {failure.get('url')}: {failure.get('message')}"
        for failure in invalid_context_failures
    )
    return result


def validate_jsonld_document(
    document: Any,
    expected_id: str | None = None,
    document_loader: Any | None = None,
) -> dict[str, Any]:
    """Parse/expand a JSON-LD document without modifying its source object.

    Network/context-loader failures are reported separately from malformed or
    semantically invalid JSON-LD. The expanded form is returned only as a
    validation result; it is never written over the source document.
    """
    result: dict[str, Any] = {
        "valid": False,
        "status": "invalid_jsonld",
        "errors": [],
        "context_resolution_errors": [],
    }
    if not isinstance(document, dict):
        result["errors"].append("JSON-LD document root must be an object")
        return result
    if "@context" not in document:
        result["errors"].append("Missing @context")
        return result
    if not _context_contains(document.get("@context"), OFFICIAL_AMB_CONTEXT_URL):
        result["errors"].append(f"Expected official AMB context {OFFICIAL_AMB_CONTEXT_URL} was not referenced")
        return result

    raw_id = document.get("id", document.get("@id"))
    raw_types = document.get("type", document.get("@type"))
    if not isinstance(raw_id, str) or not raw_id.strip():
        result["errors"].append("JSON-LD resource must have a non-empty id/@id")
        return result
    if expected_id is not None and raw_id != expected_id:
        result["errors"].append("JSON-LD resource identifier does not match the OERSI source identifier")
        return result
    if not isinstance(raw_types, (str, list)) or not raw_types:
        result["errors"].append("JSON-LD resource must have a non-empty type/@type")
        return result
    source_types = [raw_types] if isinstance(raw_types, str) else raw_types
    if not all(isinstance(value, str) and value.strip() for value in source_types):
        result["errors"].append("JSON-LD resource type values must be non-empty strings")
        return result

    loader = document_loader or JsonLdContextLoader()
    failure_count_before = len(getattr(loader, "failures", []))
    try:
        expanded = jsonld.expand(
            document,
            options={"documentLoader": loader, "processingMode": "json-ld-1.1"},
        )
    except Exception as exc:  # PyLD wraps remote loader and JSON-LD errors in JsonLdError.
        return _record_expansion_error(result, exc, loader, failure_count_before)

    if not expanded or not isinstance(expanded[0], dict):
        result["errors"].append("JSON-LD expansion produced no resource node")
        return result

    expanded_root = expanded[0]
    expanded_id = expanded_root.get("@id")
    expanded_types = expanded_root.get("@type", [])
    type_term = "type" if "type" in document else "@type"
    try:
        type_probe = jsonld.expand(
            {"@context": document["@context"], type_term: source_types},
            options={"documentLoader": loader, "processingMode": "json-ld-1.1"},
        )
        expected_expanded_types = type_probe[0].get("@type", []) if type_probe else []
    except Exception as exc:
        return _record_expansion_error(result, exc, loader, failure_count_before)
    if expanded_id != raw_id:
        result["errors"].append(
            f"Expanded identifier {expanded_id!r} does not match source identifier {raw_id!r}"
        )
    if not isinstance(expanded_types, list) or not expanded_types:
        result["errors"].append("JSON-LD expansion did not produce an @type for the resource")
    elif sorted(expanded_types) != sorted(expected_expanded_types):
        result["errors"].append(
            "Expanded resource @type does not match the type values expanded through the document context"
        )

    result.update(
        {
            "valid": not result["errors"],
            "status": "valid" if not result["errors"] else "invalid_jsonld",
            "expanded_id": expanded_id,
            "expanded_types": expanded_types,
            "expected_expanded_types": expected_expanded_types,
            "expanded_property_count": len(expanded_root),
        }
    )
    return result


def validate_jsonld_file(
    path: Path,
    expected_id: str | None = None,
    document_loader: Any | None = None,
) -> dict[str, Any]:
    """Read a serialized JSON-LD file, report parse errors, then expand it."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "status": "invalid_jsonld",
            "errors": [f"Could not parse JSON-LD JSON file: {type(exc).__name__}: {exc}"],
            "context_resolution_errors": [],
        }
    return validate_jsonld_document(document, expected_id, document_loader)
