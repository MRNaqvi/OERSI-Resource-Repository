import json
from pathlib import Path

from src.extractor import Extractor
from tests.jsonld_fixtures import LocalAmbContextLoader, resource_fixture


class FakeClient:
    api_url = "https://oersi.org/api/search/oer_data/_search"

    def __init__(self):
        self.calls = []

    def search(self, query, size):
        self.calls.append((query, size))
        if query == {"match_all": {}}:
            entries = [
                resource_fixture("https://example.edu/course", "https://w3id.org/kim/hcrt/course"),
                resource_fixture("https://example.edu/video", "https://w3id.org/kim/hcrt/video"),
                resource_fixture("https://example.edu/unknown", "https://w3id.org/kim/hcrt/other"),
            ]
        else:
            entries = []
        return {"hits": {"hits": [{"_source": record} for record in entries[:size]]}}


def test_extraction_saves_source_jsonld_normalized_data_and_summary(tmp_path: Path):
    client = FakeClient()
    report = Extractor(
        client,
        tmp_path,
        limit=10,
        sample_size=3,
        target_per_category=1,
        context_loader=LocalAmbContextLoader(),
    ).run()

    assert report["fetched_records"] == 3
    assert report["successfully_classified_courses"] == 1
    assert report["successfully_classified_learning_materials"] == 1
    assert report["unclassified_records"] == 1
    assert len(report["raw_response_files"]) == 1
    assert report["jsonld_validation"]["validated_documents"] == 3
    assert report["jsonld_validation"]["invalid_jsonld_documents"] == 0
    assert report["jsonld_validation"]["context_resolution_failures"] == 0

    for category, source_id in (
        ("courses", "https://example.edu/course"),
        ("learning_materials", "https://example.edu/video"),
        ("unclassified", "https://example.edu/unknown"),
    ):
        jsonld_files = list((tmp_path / category).glob("*.jsonld"))
        normalized_files = list((tmp_path / "normalized" / category).glob("*.json"))
        assert len(jsonld_files) == 1
        assert len(normalized_files) == 1
        source_document = json.loads(jsonld_files[0].read_text(encoding="utf-8"))
        normalized = json.loads(normalized_files[0].read_text(encoding="utf-8"))
        assert source_document["id"] == source_id
        assert "@context" in source_document
        assert "source_metadata" not in source_document
        assert normalized["oersi_id"] == source_id
        assert normalized["classification"]["container"] == category
        assert "@context" not in normalized

    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["extraction_error_count"] == 0
