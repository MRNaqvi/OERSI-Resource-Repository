from __future__ import annotations

import copy
import json
from pathlib import Path

from pyld import jsonld

from src.jsonld_validation import OFFICIAL_AMB_CONTEXT_URL, validate_jsonld_document, validate_jsonld_file
from src.jsonld_validation import JsonLdContextLoader
from src.metadata import normalize_metadata
from tests.jsonld_fixtures import (
    AMB_CONTEXT_DOCUMENT,
    FailingAmbContextLoader,
    FakeContextResponse,
    FakeContextSession,
    LocalAmbContextLoader,
    resource_fixture,
)


def test_original_context_and_properties_expand_with_correct_identity_and_type():
    source = resource_fixture()
    original = copy.deepcopy(source)
    loader = LocalAmbContextLoader()

    result = validate_jsonld_document(source, expected_id=source["id"], document_loader=loader)

    assert result["valid"] is True
    assert result["status"] == "valid"
    assert result["expanded_id"] == "https://example.edu/resource/course-1"
    assert result["expanded_types"] == ["http://schema.org/LearningResource"]
    assert source == original
    assert loader.calls
    assert set(loader.calls) == {OFFICIAL_AMB_CONTEXT_URL}


def test_expansion_preserves_nested_objects_and_multilingual_labels():
    result = validate_jsonld_document(
        resource_fixture(), document_loader=LocalAmbContextLoader()
    )

    assert result["valid"] is True
    expanded = jsonld.expand(
        resource_fixture(),
        options={"documentLoader": LocalAmbContextLoader(), "processingMode": "json-ld-1.1"},
    )[0]
    about = expanded["http://schema.org/about"][0]
    assert about["@id"] == "https://example.edu/subject/biology"
    labels = about["http://www.w3.org/2004/02/skos/core#prefLabel"]
    assert {label["@language"] for label in labels} == {"en", "de"}
    learning_type = expanded["http://schema.org/learningResourceType"][0]
    assert learning_type["@id"] == "https://w3id.org/kim/hcrt/course"


def test_missing_context_is_reported_as_invalid_jsonld():
    source = resource_fixture()
    del source["@context"]

    result = validate_jsonld_document(source, document_loader=LocalAmbContextLoader())

    assert result["valid"] is False
    assert result["status"] == "invalid_jsonld"
    assert "Missing @context" in result["errors"]
    assert result["context_resolution_errors"] == []


def test_context_resolution_failure_is_separate_from_invalid_jsonld():
    result = validate_jsonld_document(
        resource_fixture(), document_loader=FailingAmbContextLoader()
    )

    assert result["valid"] is False
    assert result["status"] == "context_resolution_failure"
    assert result["context_resolution_errors"][0]["kind"] == "network_or_http"
    assert result["errors"] == []


def test_invalid_remote_context_is_reported_as_invalid_jsonld():
    invalid_context = {"@context": {"name": {"@id": 12}}}

    class InvalidContextLoader:
        failures = []

        def __call__(self, url, options=None):
            return {"contextUrl": None, "documentUrl": url, "document": invalid_context}

    result = validate_jsonld_document(
        resource_fixture(), document_loader=InvalidContextLoader()
    )

    assert result["valid"] is False
    assert result["status"] == "invalid_jsonld"
    assert result["errors"]
    assert result["context_resolution_errors"] == []


def test_serialized_source_jsonld_is_structurally_identical_and_root_is_original(tmp_path: Path):
    source = resource_fixture()
    original = copy.deepcopy(source)
    path = tmp_path / "resource.jsonld"
    path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")

    parsed = json.loads(path.read_text(encoding="utf-8"))
    validation = validate_jsonld_file(
        path, expected_id=source["id"], document_loader=LocalAmbContextLoader()
    )

    assert parsed == original
    assert parsed["@context"] == original["@context"]
    assert "source_metadata" not in parsed
    assert "stable_id" not in parsed
    assert "classification" not in parsed
    assert validation["valid"] is True
    assert "@context" in AMB_CONTEXT_DOCUMENT


def test_normalized_values_do_not_include_source_jsonld():
    source = resource_fixture()
    original = copy.deepcopy(source)

    normalized = normalize_metadata(source)

    assert source == original
    assert normalized["oersi_id"] == source["id"]
    assert normalized["title"] == source["name"]
    assert "@context" not in normalized
    assert "source_metadata" not in normalized
    assert all(not key.startswith("@") for key in normalized)


def test_identifier_mismatch_is_reported():
    source = resource_fixture()

    result = validate_jsonld_document(
        source,
        expected_id="https://example.edu/a-different-id",
        document_loader=LocalAmbContextLoader(),
    )

    assert result["valid"] is False
    assert result["status"] == "invalid_jsonld"
    assert any("does not match" in error for error in result["errors"])


def test_invalid_json_serialization_is_reported(tmp_path: Path):
    path = tmp_path / "broken.jsonld"
    path.write_text('{"@context": ', encoding="utf-8")

    result = validate_jsonld_file(path, document_loader=LocalAmbContextLoader())

    assert result["valid"] is False
    assert result["status"] == "invalid_jsonld"
    assert "JSONDecodeError" in result["errors"][0]


def test_context_loader_returns_context_document_with_null_context_url():
    session = FakeContextSession(FakeContextResponse(AMB_CONTEXT_DOCUMENT))
    loader = JsonLdContextLoader(session=session)

    loaded = loader(OFFICIAL_AMB_CONTEXT_URL)

    assert loaded["contextUrl"] is None
    assert loaded["document"] == AMB_CONTEXT_DOCUMENT
    assert session.calls[0][1]["timeout"] == loader.timeout


def test_context_loader_does_not_misuse_raw_link_header_as_context_url():
    link_header = '<https://example.edu/license>; rel="license", <https://example.edu/next>; rel="next"'
    session = FakeContextSession(FakeContextResponse(AMB_CONTEXT_DOCUMENT, link_header))
    loader = JsonLdContextLoader(session=session)

    loaded = loader(OFFICIAL_AMB_CONTEXT_URL)

    assert loaded["contextUrl"] is None
    assert loaded["contextUrl"] != link_header
    assert loaded["document"] == AMB_CONTEXT_DOCUMENT
