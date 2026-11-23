"""Phase 1 orchestration: fetch, classify, normalize, and save metadata only."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.classification import COURSE_TYPE_ID, LEARNING_MATERIAL_TYPE_IDS, classify_resource
from src.metadata import filename_for, normalize_metadata
from src.jsonld_validation import JsonLdContextLoader, validate_jsonld_file
from src.oersi_client import OERSIClient


class Extractor:
    def __init__(
        self,
        client: OERSIClient,
        output_dir: Path,
        limit: int = 100,
        sample_size: int = 20,
        target_per_category: int = 10,
        context_loader: Any | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if sample_size < 1:
            raise ValueError("sample_size must be at least 1")
        if target_per_category < 0:
            raise ValueError("target_per_category cannot be negative")
        self.client = client
        self.output_dir = output_dir
        self.limit = limit
        self.sample_size = sample_size
        self.target_per_category = target_per_category
        self.context_loader = context_loader or JsonLdContextLoader()
        self.errors: list[dict[str, str]] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.api_hits_fetched = 0
        self.raw_files: list[str] = []
        self._run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    def _save_raw(self, label: str, payload: dict[str, Any]) -> None:
        raw_dir = self.output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{self._run_stamp}_{label}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.raw_files.append(str(path.relative_to(self.output_dir.parent)))

    def _ingest(self, payload: dict[str, Any], label: str) -> None:
        hits = payload.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            self.errors.append({"stage": "response", "message": f"{label}: hits.hits is not a list"})
            return
        self.api_hits_fetched += len(hits)
        for hit in hits:
            if not isinstance(hit, dict):
                self.errors.append({"stage": "record", "message": f"{label}: hit is not an object"})
                continue
            metadata = hit.get("_source")
            if not isinstance(metadata, dict):
                self.errors.append({"stage": "record", "message": f"{label}: hit has no _source object"})
                continue
            identifier = metadata.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                self.errors.append({"stage": "record", "message": f"{label}: record has no source id"})
                continue
            identifier = identifier.strip()
            if identifier in self.records:
                continue
            try:
                normalized = normalize_metadata(metadata)
                container, reason = classify_resource(metadata)
                self.records[identifier] = {
                    "source": metadata,
                    "normalized": {
                        **normalized,
                        "classification": {"container": container, "reason": reason},
                    },
                }
            except (TypeError, ValueError, RecursionError) as exc:
                self.errors.append(
                    {"stage": "record", "resource_id": identifier, "message": str(exc)}
                )

    def _search(self, label: str, query: dict[str, Any], size: int) -> None:
        if size <= 0:
            return
        try:
            payload = self.client.search(query, size=size)
            self._save_raw(label, payload)
            self._ingest(payload, label)
        except Exception as exc:  # Network, HTTP, JSON, and persistence failures are reported.
            self.errors.append({"stage": "request", "message": f"{label}: {type(exc).__name__}: {exc}"})

    def _category_count(self, container: str) -> int:
        return sum(
            record["normalized"]["classification"]["container"] == container
            for record in self.records.values()
        )

    def _top_up(self, container: str, query: dict[str, Any], label: str) -> None:
        remaining_budget = self.limit - len(self.records)
        deficit = self.target_per_category - self._category_count(container)
        request_size = min(remaining_budget, max(0, deficit))
        if request_size <= 0:
            return
        excluded = list(self.records)
        if excluded:
            query = {
                "bool": {
                    "must": [query],
                    "must_not": [{"terms": {"id": excluded}}],
                }
            }
        self._search(label, query, request_size)

    def run(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._search("initial_sample", {"match_all": {}}, min(self.sample_size, self.limit))

        self._top_up(
            "courses",
            {"term": {"learningResourceType.id": COURSE_TYPE_ID}},
            "top_up_courses",
        )
        material_query = {
            "terms": {"learningResourceType.id": sorted(LEARNING_MATERIAL_TYPE_IDS)}
        }
        self._top_up("learning_materials", material_query, "top_up_learning_materials")

        validation_results: list[dict[str, Any]] = []
        for record in self.records.values():
            source = record["source"]
            app_metadata = record["normalized"]
            container = app_metadata["classification"]["container"]
            directory = {
                "courses": "courses",
                "learning_materials": "learning_materials",
                "unclassified": "unclassified",
            }[container]
            stable_id = app_metadata["stable_id"]
            destination = self.output_dir / directory / f"{filename_for(stable_id)}ld"
            normalized_destination = (
                self.output_dir / "normalized" / directory / filename_for(stable_id)
            )
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                validation = validate_jsonld_file(
                    destination,
                    expected_id=source.get("id") if isinstance(source.get("id"), str) else None,
                    document_loader=self.context_loader,
                )
                validation_results.append(
                    {
                        "stable_id": stable_id,
                        "container": container,
                        "path": str(destination.relative_to(self.output_dir.parent)),
                        **validation,
                    }
                )
                if validation["status"] != "valid":
                    self.errors.append(
                        {
                            "stage": "jsonld_validation",
                            "resource_id": app_metadata["oersi_id"],
                            "status": validation["status"],
                            "message": "; ".join(validation["errors"])
                            or "; ".join(
                                error.get("message", "Context resolution failed")
                                for error in validation["context_resolution_errors"]
                            ),
                            "path": str(destination.relative_to(self.output_dir.parent)),
                        }
                    )
            except OSError as exc:
                self.errors.append(
                    {"stage": "write_jsonld", "resource_id": app_metadata["oersi_id"], "message": str(exc)}
                )
            try:
                normalized_destination.parent.mkdir(parents=True, exist_ok=True)
                normalized_destination.write_text(
                    json.dumps(app_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                self.errors.append(
                    {"stage": "write_normalized", "resource_id": app_metadata["oersi_id"], "message": str(exc)}
                )

        counts = {
            "courses": self._category_count("courses"),
            "learning_materials": self._category_count("learning_materials"),
            "unclassified": self._category_count("unclassified"),
        }
        report = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "api_endpoint": self.client.api_url,
            "fetched_records": self.api_hits_fetched,
            "unique_records": len(self.records),
            "successfully_classified_courses": counts["courses"],
            "successfully_classified_learning_materials": counts["learning_materials"],
            "unclassified_records": counts["unclassified"],
            "extraction_error_count": len(self.errors),
            "extraction_errors": self.errors,
            "raw_response_files": self.raw_files,
            "jsonld_validation": {
                "validated_documents": sum(item["status"] == "valid" for item in validation_results),
                "invalid_jsonld_documents": sum(item["status"] == "invalid_jsonld" for item in validation_results),
                "context_resolution_failures": sum(
                    item["status"] == "context_resolution_failure" for item in validation_results
                ),
                "results": validation_results,
            },
            "classification_rule": (
                f"Exact {COURSE_TYPE_ID} IDs are courses. Recognized concrete OERSI HCRT material IDs "
                "are learning materials; absent, unknown, portal, web_page, and broad 'other' types "
                "remain unclassified."
            ),
            "resources": [
                {
                    "stable_id": record["normalized"]["stable_id"],
                    "oersi_id": record["normalized"]["oersi_id"],
                    "container": record["normalized"]["classification"]["container"],
                }
                for record in self.records.values()
            ],
        }
        report_path = self.output_dir / "summary.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
