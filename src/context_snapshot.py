"""Capture inspectable JSON-LD context snapshots and reload approved pins offline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from pyld.iri_resolver import resolve

MANIFEST = "manifest.json"
FORMAT = "oersi-jsonld-contexts-v1"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _require_http_url(url: Any) -> str:
    if isinstance(url, str):
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"} and parts.hostname
            and parts.username is None and parts.password is None
            and not any(char.isspace() for char in url)
        ):
            return url
    raise ValueError("Context URLs must be absolute HTTP(S) URLs without embedded credentials")


def _read_context(contents: bytes, url: str) -> dict[str, Any]:
    try:
        document = json.loads(contents)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid JSON in context document for {url}") from exc
    if not isinstance(document, dict) or "@context" not in document:
        raise ValueError(f"Context document for {url} must be a JSON object with @context")
    return document


def _resolve_imports(context: Any, base: str) -> None:
    if isinstance(context, list):
        for item in context:
            _resolve_imports(item, base)
    elif isinstance(context, dict):
        # PyLD resolves nested @context URLs against documentUrl, but processes
        # @import using the source base. Resolve imports on the in-memory copy.
        if isinstance(context.get("@import"), str):
            context["@import"] = resolve(context["@import"], base)
        for definition in context.values():
            if isinstance(definition, dict) and "@context" in definition:
                _resolve_imports(definition["@context"], base)


@dataclass(frozen=True)
class ContextDocument:
    url: str
    document_url: str
    contents: bytes

    def remote_document(self) -> dict[str, Any]:
        document = _read_context(self.contents, self.url)
        _resolve_imports(document["@context"], self.document_url)
        return {
            "document": document,
            "documentUrl": self.document_url,
            # Keep the existing ingestion convention of using the JSON body's context.
            "contextUrl": None,
        }


class _PinnedLoader:
    def __init__(self, documents: tuple[ContextDocument, ...]) -> None:
        self._documents = {document.url: document for document in documents}
        self.failures: list[dict[str, str]] = []

    def __call__(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if url not in self._documents:
            message = f"No pinned JSON-LD context for {url!r} in the approved snapshot"
            self.failures.append({"kind": "missing_pinned_context", "url": url, "message": message})
            raise ValueError(message)
        return self._documents[url].remote_document()


@dataclass(frozen=True)
class ContextSnapshot:
    sha256: str
    documents: tuple[ContextDocument, ...]

    def loader(self) -> _PinnedLoader:
        return _PinnedLoader(self.documents)


class ContextCapture:
    """Network loader for explicit capture only. A URL is fetched once per capture."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.documents: dict[str, ContextDocument] = {}
        self.failures: list[dict[str, str]] = []

    def __call__(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if url not in self.documents:
            _require_http_url(url)
            try:
                response = self.session.get(url, timeout=15, headers=(options or {}).get("headers"))
                response.raise_for_status()
            except requests.RequestException as exc:
                self.failures.append({"kind": "network_or_http", "url": url, "message": str(exc)})
                raise
            final_url = _require_http_url(response.url)
            document = ContextDocument(url, final_url, response.content)
            remote = document.remote_document()
            self.documents[url] = document
            return remote
        return self.documents[url].remote_document()


def write_context_snapshot(directory: Path | str, documents: Iterable[ContextDocument]) -> dict[str, Any]:
    """Publish a new candidate from supplied context bytes, retaining originals."""
    root = Path(directory)
    entries = []
    blobs = {}
    known_urls = set()
    for document in sorted(documents, key=lambda item: item.url):
        _require_http_url(document.url)
        _require_http_url(document.document_url)
        _read_context(document.contents, document.url)
        if document.url in known_urls:
            raise ValueError(f"Duplicate context URL {document.url!r}")
        known_urls.add(document.url)
        sha256 = _digest(document.contents)
        blobs[sha256] = document.contents
        entries.append({"url": document.url, "document_url": document.document_url, "sha256": sha256})
    manifest = {
        "format": FORMAT,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "contexts": entries,
    }
    body = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    root.mkdir(parents=True, exist_ok=False)
    (root / "documents").mkdir()
    for sha256, contents in blobs.items():
        with (root / "documents" / f"{sha256}.jsonld").open("xb") as target:
            target.write(contents)
    # A candidate is complete only once its manifest exists.
    with (root / MANIFEST).open("xb") as target:
        target.write(body)
    return {**manifest, "manifest_sha256": _digest(body)}


def _read_snapshot(
    directory: Path | str, expected_sha256: str | None = None,
) -> tuple[dict[str, Any], ContextSnapshot]:
    root = Path(directory)
    try:
        raw_manifest = (root / MANIFEST).read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read context snapshot manifest at {root / MANIFEST}: {exc}") from exc
    fingerprint = _digest(raw_manifest)
    if expected_sha256 is not None and fingerprint != expected_sha256:
        raise ValueError(f"Context snapshot manifest SHA-256 mismatch: expected {expected_sha256}, got {fingerprint}")
    manifest = json.loads(raw_manifest)
    if (
        not isinstance(manifest, dict) or manifest.get("format") != FORMAT
        or not isinstance(manifest.get("contexts"), list)
    ):
        raise ValueError(f"Invalid or unsupported context snapshot manifest at {root / MANIFEST}")

    documents = []
    known_urls = set()
    for entry in manifest["contexts"]:
        if not isinstance(entry, dict) or not _is_sha256(entry.get("sha256")):
            raise ValueError("Invalid context document entry in snapshot manifest")
        url = _require_http_url(entry.get("url"))
        final_url = _require_http_url(entry.get("document_url"))
        if url in known_urls:
            raise ValueError(f"Duplicate context URL {url!r} in snapshot manifest")
        known_urls.add(url)
        path = root / "documents" / f"{entry['sha256']}.jsonld"
        try:
            contents = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read pinned JSON-LD context for {url!r}: {exc}") from exc
        if _digest(contents) != entry["sha256"]:
            raise ValueError(f"Pinned JSON-LD context SHA-256 mismatch for {url!r}")
        _read_context(contents, url)
        documents.append(ContextDocument(url, final_url, contents))
    return {**manifest, "manifest_sha256": fingerprint}, ContextSnapshot(fingerprint, tuple(documents))


def inspect_context_snapshot(directory: Path | str) -> dict[str, Any]:
    """Check every local file and report the candidate fingerprint for review."""
    manifest, _ = _read_snapshot(directory)
    return manifest


def load_context_snapshot(directory: Path | str, expected_sha256: str) -> ContextSnapshot:
    """Load immutable bytes only after checking an explicitly approved fingerprint."""
    if not _is_sha256(expected_sha256):
        raise ValueError("An approved context snapshot SHA-256 (64 lowercase hex characters) is required")
    _, snapshot = _read_snapshot(directory, expected_sha256)
    return snapshot
