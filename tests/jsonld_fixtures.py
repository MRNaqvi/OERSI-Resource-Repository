"""Offline JSON-LD fixture data and controlled AMB context loader."""

from __future__ import annotations

from typing import Any

from src.jsonld_validation import OFFICIAL_AMB_CONTEXT_URL

AMB_CONTEXT_DOCUMENT = {
    "@context": {
        "id": "@id",
        "type": "@type",
        "@vocab": "http://schema.org/",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "prefLabel": {"@id": "skos:prefLabel", "@container": "@language"},
    }
}


def resource_fixture(
    resource_id: str = "https://example.edu/resource/course-1",
    resource_type: str = "https://w3id.org/kim/hcrt/course",
) -> dict[str, Any]:
    return {
        "@context": [OFFICIAL_AMB_CONTEXT_URL, {"@language": "en"}],
        "id": resource_id,
        "type": ["LearningResource"],
        "name": "A multilingual resource",
        "description": "A fixture for JSON-LD expansion.",
        "learningResourceType": [
            {
                "id": resource_type,
                "prefLabel": {"en": "Course", "de": "Kurs", "fr": "Cours"},
            }
        ],
        "about": [
            {
                "id": "https://example.edu/subject/biology",
                "prefLabel": {"en": "Biology", "de": "Biologie"},
            }
        ],
        "creator": [{"id": "https://example.edu/person/ada", "name": "Ada Author", "type": "Person"}],
        "publisher": [{"name": "Example University", "type": "Organization"}],
        "license": {"id": "https://creativecommons.org/licenses/by/4.0/"},
        "inLanguage": ["en", "de"],
        "mainEntityOfPage": [{"id": "https://example.edu/course-1", "type": "WebPage"}],
        "sourceOrganization": [{"name": "Example Faculty", "type": "Organization"}],
        "datePublished": "2025-01-02",
        "dateModified": "2025-03-04",
        "customSourceProperty": {"keep": True, "values": [1, 2, 3]},
    }


class LocalAmbContextLoader:
    """Return a fixed copy of the official context without network requests."""

    def __init__(self) -> None:
        self.failures: list[dict[str, str]] = []
        self.calls: list[str] = []

    def __call__(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(url)
        if url != OFFICIAL_AMB_CONTEXT_URL:
            raise ValueError(f"Unexpected context URL: {url}")
        return {
            "contextUrl": None,
            "documentUrl": url,
            "document": AMB_CONTEXT_DOCUMENT,
        }


class FailingAmbContextLoader:
    """Controlled stand-in for an unreachable remote JSON-LD context."""

    def __init__(self) -> None:
        self.failures: list[dict[str, str]] = []

    def __call__(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        failure = {"url": url, "kind": "network_or_http", "message": "offline fixture"}
        self.failures.append(failure)
        raise OSError("offline fixture")


class FakeContextResponse:
    def __init__(self, document: Any, link_header: str | None = None) -> None:
        self._document = document
        self.headers = {"Link": link_header} if link_header is not None else {}
        self.url = OFFICIAL_AMB_CONTEXT_URL

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._document


class FakeContextSession:
    def __init__(self, response: FakeContextResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeContextResponse:
        self.calls.append((url, kwargs))
        return self.response
