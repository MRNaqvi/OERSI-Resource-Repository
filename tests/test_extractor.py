import json
from pathlib import Path

from src.extractor import Extractor


class FakeClient:
    api_url = "https://oersi.org/api/search/oer_data/_search"

    def __init__(self):
        self.calls = []

    def search(self, query, size):
        self.calls.append((query, size))
        if query == {"match_all": {}}:
            entries = [
                {"id": "https://example.edu/course", "learningResourceType": [{"id": "https://w3id.org/kim/hcrt/course"}]},
                {"id": "https://example.edu/video", "learningResourceType": [{"id": "https://w3id.org/kim/hcrt/video"}]},
                {"id": "https://example.edu/unknown", "learningResourceType": [{"id": "https://w3id.org/kim/hcrt/other"}]},
            ]
        else:
            entries = []
        return {"hits": {"hits": [{"_source": record} for record in entries[:size]]}}


def test_extraction_saves_raw_categorized_and_summary_files(tmp_path: Path):
    client = FakeClient()
    report = Extractor(client, tmp_path, limit=10, sample_size=3, target_per_category=1).run()

    assert report["fetched_records"] == 3
    assert report["successfully_classified_courses"] == 1
    assert report["successfully_classified_learning_materials"] == 1
    assert report["unclassified_records"] == 1
    assert len(report["raw_response_files"]) == 1
    assert list((tmp_path / "courses").glob("*.json"))
    assert list((tmp_path / "learning_materials").glob("*.json"))
    assert list((tmp_path / "unclassified").glob("*.json"))
    assert json.loads((tmp_path / "summary.json").read_text())["extraction_error_count"] == 0
