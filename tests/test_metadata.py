import pytest

from src.metadata import normalize_metadata, stable_resource_id


def test_normalizes_nested_oersi_fields_and_preserves_source():
    source = {
        "id": "https://example.edu/resource/123",
        "name": "A resource",
        "description": "Description",
        "learningResourceType": [
            {"id": "https://w3id.org/kim/hcrt/video", "prefLabel": {"en": "Video", "de": "Video"}}
        ],
        "keywords": ["biology", "open"],
        "about": [{"id": "https://example.edu/biology", "prefLabel": {"en": "Biology"}}],
        "creator": [{"name": "Ada Author", "type": "Person"}],
        "publisher": [{"name": "Example University", "type": "Organization"}],
        "inLanguage": ["en"],
        "license": {"id": "https://creativecommons.org/licenses/by/4.0/"},
        "mainEntityOfPage": [{"id": "https://example.edu/watch/123", "provider": {"name": "Example"}}],
        "datePublished": "2025-01-02",
        "dateModified": "2025-02-03",
    }

    result = normalize_metadata(source)

    assert result["oersi_id"] == source["id"]
    assert result["title"] == "A resource"
    assert result["resource_type"] == [{"id": "https://w3id.org/kim/hcrt/video", "label": "Video"}]
    assert result["subject"] == ["Biology"]
    assert result["creator"] == ["Ada Author"]
    assert result["publisher"] == ["Example University"]
    assert result["license"] == source["license"]["id"]
    assert result["source_url"] == "https://example.edu/watch/123"
    assert result["source_provider"] == ["Example"]
    assert result["publication_date"] == "2025-01-02"
    assert result["modification_date"] == "2025-02-03"
    assert result["source_metadata"] == source


def test_missing_optional_fields_are_safe():
    result = normalize_metadata({"id": "https://example.edu/minimal"})

    assert result["title"] is None
    assert result["description"] is None
    assert result["keywords"] == []
    assert result["source_provider"] == []


def test_author_is_used_when_creator_is_absent():
    result = normalize_metadata(
        {"id": "https://example.edu/authored", "author": [{"name": "A. Writer"}]}
    )

    assert result["creator"] == ["A. Writer"]


def test_stable_id_uses_identifier_not_title():
    assert stable_resource_id("https://example.edu/id") == stable_resource_id("https://example.edu/id")
    assert stable_resource_id("https://example.edu/id") != stable_resource_id("https://example.edu/other")


def test_identifier_is_required():
    with pytest.raises(ValueError, match="no usable 'id'"):
        normalize_metadata({"name": "No identifier"})
