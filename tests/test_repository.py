from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from src.classification import classify_resource
from src.metadata import normalize_metadata
from src.repository import CONTAINERS, Repository, metadata_hash, utc_now
from tests.jsonld_fixtures import LocalAmbContextLoader, resource_fixture

BASE_IRI = "https://example.org/oersi/"


def make_record(resource_id="https://example.edu/resource/1", type_id="https://w3id.org/kim/hcrt/course"):
    source = resource_fixture(resource_id, type_id)
    normalized = normalize_metadata(source)
    container, reason = classify_resource(source)
    normalized["classification"] = {"container": container, "reason": reason}
    return source, normalized


def make_repository(path: Path, base_iri: str = BASE_IRI) -> Repository:
    repository = Repository(path, base_iri=base_iri, context_loader=LocalAmbContextLoader())
    repository.initialize()
    return repository


def write_manifest(data_dir: Path, entries: list[dict[str, str]], run_at: str | None = None) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    category_counts = {"courses": 0, "learning_materials": 0, "unclassified": 0}
    for entry in entries:
        category_counts[entry["container"]] += 1
    resources = [
        {"stable_id": entry["stable_id"], "oersi_id": entry["oersi_id"], "container": entry["container"]}
        for entry in entries
    ]
    (data_dir / "summary.json").write_text(
        json.dumps(
            {
                "fetched_records": len(entries),
                "run_at": run_at or utc_now(),
                "unique_records": len(entries),
                "successfully_classified_courses": category_counts["courses"],
                "successfully_classified_learning_materials": category_counts["learning_materials"],
                "unclassified_records": category_counts["unclassified"],
                "extraction_error_count": 0,
                "extraction_errors": [],
                "resources": resources,
                "jsonld_validation": {
                    "validated_documents": len(entries),
                    "invalid_jsonld_documents": 0,
                    "context_resolution_failures": 0,
                    "results": [
                        {
                            "stable_id": entry["stable_id"],
                            "container": entry["container"],
                            "status": "valid",
                            "valid": True,
                        }
                        for entry in entries
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def write_resource_files(data_dir: Path, source: dict, normalized: dict, category: str) -> None:
    resource_id = normalized["stable_id"]
    jsonld_path = data_dir / category / f"{resource_id}.jsonld"
    normalized_path = data_dir / "normalized" / category / f"{resource_id}.json"
    jsonld_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    jsonld_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_database_initialization_creates_exactly_two_unique_containers(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")

    containers = repository.containers()

    assert len(containers) == 2
    assert {item["container_id"] for item in containers} == set(CONTAINERS)
    assert len({item["container_iri"] for item in containers}) == 2
    assert {item["container_iri"] for item in containers} == {
        f"{BASE_IRI.rstrip('/')}/container/courses",
        f"{BASE_IRI.rstrip('/')}/container/learning_materials",
    }


def test_import_creates_stable_resource_and_version_one_iris(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    original = copy.deepcopy(source)

    category = repository.import_resource(source, normalized)
    stored = repository.get_resource(normalized["stable_id"])
    version = stored["current_version"]

    assert category == "courses"
    assert stored["source_iri"] == source["id"]
    assert stored["resource_iri"] == f"{BASE_IRI.rstrip('/')}/resource/{normalized['stable_id']}"
    assert stored["container_id"] == "courses"
    assert stored["current_version_iri"] == f"{stored['resource_iri']}/version/1"
    assert version["version_number"] == 1
    assert version["version_iri"] == f"{stored['resource_iri']}/version/1"
    assert version["graph_iri"] == f"{BASE_IRI.rstrip('/')}/graph/resource/{normalized['stable_id']}/version/1"
    assert version["previous_version_iri"] is None
    assert version["original_jsonld"] == original
    assert source == original
    assert "resource_iri" not in version["original_jsonld"]
    assert version["normalized_metadata"]["oersi_id"] == source["id"]
    assert version["created_at"] == stored["first_imported_at"]


def test_repeated_import_is_idempotent_and_preserves_version_snapshot(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized)
    original_resource = repository.get_resource(normalized["stable_id"])
    original_version = original_resource["current_version"]

    repository.import_resource(source, normalized)

    assert repository.stats()["courses"] == 1
    assert repository.stats()["resource_versions"] == 1
    assert repository.stats()["pending_updates"] == 0
    updated_resource = repository.get_resource(normalized["stable_id"])
    assert updated_resource["current_version"] == original_version
    assert updated_resource["last_observed_at"] >= original_resource["last_observed_at"]


def test_metadata_hash_uses_sorted_object_keys_and_json_content_equality():
    assert metadata_hash({"b": [1, 2], "a": {"z": 1}}) == metadata_hash({"a": {"z": 1}, "b": [1, 2]})
    assert metadata_hash({"a": 1}) != metadata_hash({"a": "1"})


def test_classified_metadata_change_creates_only_one_new_version(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized)
    original_snapshot = repository.get_resource(normalized["stable_id"])["current_version"]["original_jsonld"]
    changed = copy.deepcopy(source)
    changed["description"] = "Updated metadata from OERSI"
    changed_normalized = normalize_metadata(changed)
    changed_normalized["classification"] = normalized["classification"]

    repository.import_resource(changed, changed_normalized)
    repository.import_resource(changed, changed_normalized)

    first, second = repository.list_versions(normalized["stable_id"])
    assert first["original_jsonld"] == original_snapshot
    assert second["original_jsonld"] == changed
    assert second["metadata_hash"] == metadata_hash(changed)
    assert second["normalized_metadata"]["classification"]["container"] == "courses"
    assert repository.get_resource(normalized["stable_id"])["current_version"] == second
    assert repository.list_pending_updates() == []


def test_classification_change_is_pending_and_never_moves_resource(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/course")
    repository.import_resource(source, normalized)
    changed = copy.deepcopy(source)
    changed["learningResourceType"] = [{"id": "https://w3id.org/kim/hcrt/textbook"}]
    changed_normalized = normalize_metadata(changed)
    changed_category, changed_reason = classify_resource(changed)
    changed_normalized["classification"] = {"container": changed_category, "reason": changed_reason}

    assert repository.import_resource(changed, changed_normalized) == "learning_materials"
    assert repository.list_resources("courses")[0]["resource_id"] == normalized["stable_id"]
    assert repository.list_resources("learning_materials") == []
    assert repository.list_pending_updates()[0]["candidate_classification"] == "learning_materials"
    assert repository.stats()["resource_versions"] == 1


def test_unknown_or_ambiguous_resource_is_kept_unclassified(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/unknown")

    assert repository.import_resource(source, normalized) == "unclassified"
    assert repository.stats()["containers"] == 2
    assert repository.stats()["unclassified_resources"] == 1
    assert repository.stats()["resources"] == 1
    assert repository.stats()["resource_versions"] == 1
    assert repository.list_resources("courses") == []
    assert repository.list_resources("learning_materials") == []
    assert "No recognized" in repository.list_unclassified()[0]["classification_reason"]
    unclassified_resource = repository.get_resource(normalized["stable_id"])
    assert unclassified_resource["container_id"] is None
    assert unclassified_resource["resource_iri"].endswith(f"/resource/{normalized['stable_id']}")

    mixed, mixed_normalized = make_record(
        "https://example.edu/resource/mixed", "https://w3id.org/kim/hcrt/course"
    )
    mixed["learningResourceType"].append({"id": "https://w3id.org/kim/hcrt/video"})
    mixed_normalized = normalize_metadata(mixed)
    _, reason = classify_resource(mixed)
    mixed_normalized["classification"] = {"container": "unclassified", "reason": reason}
    assert repository.import_resource(mixed, mixed_normalized) == "unclassified"
    stored_mixed = next(
        row for row in repository.list_unclassified() if row["source_iri"] == mixed["id"]
    )
    assert "Ambiguous" in stored_mixed["classification_reason"]
    assert repository.stats()["resource_versions"] == 2


def test_unclassified_resource_reclassification_is_queued_not_moved(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/other")
    repository.import_resource(source, normalized)
    changed = copy.deepcopy(source)
    changed["learningResourceType"] = [{"id": "https://w3id.org/kim/hcrt/course"}]
    changed_normalized = normalize_metadata(changed)
    changed_category, changed_reason = classify_resource(changed)
    changed_normalized["classification"] = {"container": changed_category, "reason": changed_reason}

    repository.import_resource(changed, changed_normalized)

    assert repository.stats()["unclassified_resources"] == 1
    assert repository.list_resources("courses") == []
    assert repository.list_pending_updates()[0]["resource_id"] == normalized["stable_id"]
    assert repository.list_pending_updates()[0]["candidate_classification"] == "courses"


def test_database_rejects_mixed_namespace_configuration(tmp_path: Path):
    path = tmp_path / "repo.sqlite"
    make_repository(path)

    other = Repository(path, base_iri="https://other.example/oersi", context_loader=LocalAmbContextLoader())
    with pytest.raises(ValueError, match="Database namespace"):
        other.initialize()


def test_vocabulary_namespace_is_persisted_and_must_match(tmp_path: Path):
    path = tmp_path / "repo.sqlite"
    vocabulary = "https://vocab.example.edu/oersi/"
    repository = Repository(
        path,
        base_iri=BASE_IRI,
        vocabulary_iri=vocabulary,
        context_loader=LocalAmbContextLoader(),
    )
    repository.initialize()
    with repository._connection() as connection:
        saved = connection.execute(
            "SELECT config_value FROM repository_config WHERE config_key='vocabulary_iri'"
        ).fetchone()[0]
    assert saved == f"{vocabulary.rstrip('/')}/"

    incompatible = Repository(
        path,
        base_iri=BASE_IRI,
        vocabulary_iri="https://other.example/vocab/",
        context_loader=LocalAmbContextLoader(),
    )
    with pytest.raises(ValueError, match="vocabulary namespace"):
        incompatible.initialize()

    with pytest.raises(ValueError, match="vocabulary namespace"):
        incompatible.stats()


def test_vocabulary_namespace_must_be_valid_http_iri(tmp_path: Path):
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        Repository(
            tmp_path / "repo.sqlite",
            base_iri=BASE_IRI,
            vocabulary_iri="urn:oersi:vocab",
            context_loader=LocalAmbContextLoader(),
        )


def test_legacy_database_is_not_modified_or_silently_reset(tmp_path: Path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE repository_config (config_key TEXT PRIMARY KEY, config_value TEXT NOT NULL)")
        connection.execute("INSERT INTO repository_config VALUES ('base_iri', ?)", (BASE_IRI.rstrip("/"),))
        connection.execute("CREATE TABLE legacy_resource (resource_id TEXT PRIMARY KEY, marker TEXT)")
        connection.execute("INSERT INTO legacy_resource VALUES ('preserve-me', 'data')")

    repository = Repository(path, base_iri=BASE_IRI, context_loader=LocalAmbContextLoader())
    with pytest.raises(RuntimeError, match="left unchanged.*migration procedure"):
        repository.initialize()

    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT marker FROM legacy_resource WHERE resource_id='preserve-me'").fetchone()
        assert row == ("data",)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='containers'").fetchone()[0] == 0


def test_invalid_jsonld_is_rejected_before_resource_import(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    source.pop("@context")

    with pytest.raises(ValueError, match="Source JSON-LD validation failed"):
        repository.import_resource(source, normalized)

    assert repository.stats()["resources"] == 0
    assert repository.stats()["resource_versions"] == 0


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("stable_id", "oersi-not-the-source-id", "stable_id"),
        ("oersi_id", "https://wrong.example/id", "oersi_id"),
        ("title", "Tampered title", "title"),
        ("resource_type", [{"id": "https://w3id.org/kim/hcrt/video", "label": "Video"}], "resource_type"),
        ("classification", {"container": "learning_materials", "reason": "wrong"}, "classification category"),
    ],
)
def test_inconsistent_normalized_metadata_is_rejected(tmp_path: Path, field, bad_value, message):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    normalized[field] = bad_value

    with pytest.raises(ValueError, match=message):
        repository.import_resource(source, normalized)

    assert repository.stats()["resources"] == 0
    assert repository.stats()["resource_versions"] == 0


def test_version_snapshots_are_immutable(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized)
    stored = repository.get_resource(normalized["stable_id"])

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with repository._transaction() as connection:
            connection.execute(
                "UPDATE resource_versions SET original_jsonld='{}' WHERE version_iri=?",
                (stored["current_version_iri"],),
            )


def test_transaction_rolls_back_resource_when_version_insert_fails(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    with repository._transaction() as connection:
        connection.execute(
            """CREATE TRIGGER fail_version_insert BEFORE INSERT ON resource_versions
               BEGIN SELECT RAISE(ABORT, 'test rollback'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        repository.import_resource(source, normalized)

    assert repository.get_resource(normalized["stable_id"]) is None
    assert repository.stats()["resource_versions"] == 0


def test_foreign_keys_are_enabled_and_enforced(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")

    with repository._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO resources(resource_id, source_iri, resource_iri, container_id,
                   first_imported_at, last_observed_at) VALUES ('bad','source','resource','missing','n','n')"""
            )


def test_resource_current_version_must_belong_to_same_resource(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source_a, normalized_a = make_record("https://example.edu/resource/a")
    source_b, normalized_b = make_record("https://example.edu/resource/b")
    repository.import_resource(source_a, normalized_a)
    repository.import_resource(source_b, normalized_b)
    resource_a = repository.get_resource(normalized_a["stable_id"])
    resource_b = repository.get_resource(normalized_b["stable_id"])

    with pytest.raises(sqlite3.IntegrityError):
        with repository._transaction() as connection:
            connection.execute(
                "UPDATE resources SET current_version_iri=? WHERE resource_id=?",
                (resource_a["current_version_iri"], normalized_b["stable_id"]),
            )

    assert repository.get_resource(normalized_b["stable_id"])["current_version_iri"] == resource_b["current_version_iri"]


def test_previous_version_must_belong_to_same_resource(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source_a, normalized_a = make_record("https://example.edu/resource/a")
    source_b, normalized_b = make_record("https://example.edu/resource/b")
    repository.import_resource(source_a, normalized_a)
    repository.import_resource(source_b, normalized_b)
    resource_a = repository.get_resource(normalized_a["stable_id"])

    with pytest.raises(sqlite3.IntegrityError):
        with repository._transaction() as connection:
            connection.execute(
                """INSERT INTO resource_versions
                   (resource_id, version_number, version_iri, graph_iri, original_jsonld,
                    normalized_metadata, metadata_hash, created_at, previous_version_iri)
                   VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized_b["stable_id"],
                    f"{BASE_IRI.rstrip('/')}/resource/{normalized_b['stable_id']}/version/2",
                    f"{BASE_IRI.rstrip('/')}/graph/resource/{normalized_b['stable_id']}/version/2",
                    json.dumps(source_b),
                    json.dumps(normalized_b),
                    metadata_hash(source_b),
                    "2026-01-01T00:00:00+00:00",
                    resource_a["current_version_iri"],
                ),
            )
    assert repository.stats()["resource_versions"] == 2


def test_unclassified_rows_require_matching_persistent_resource_and_source_iri(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/unknown")
    repository.import_resource(source, normalized)

    with pytest.raises(sqlite3.IntegrityError):
        with repository._transaction() as connection:
            connection.execute(
                """INSERT INTO unclassified_resources
                   (resource_id, source_iri, original_jsonld, classification_reason,
                    first_observed_at, last_observed_at)
                   VALUES ('orphan', 'https://orphan.example/', '{}', 'missing', 'n', 'n')"""
            )

    with pytest.raises(sqlite3.IntegrityError):
        with repository._transaction() as connection:
            connection.execute(
                """INSERT INTO unclassified_resources
                   (resource_id, source_iri, original_jsonld, classification_reason,
                    first_observed_at, last_observed_at)
                   VALUES (?, 'https://wrong.example/id', ?, 'mismatch', 'n', 'n')""",
                (normalized["stable_id"], json.dumps(source)),
            )

    with pytest.raises(sqlite3.IntegrityError, match="must match source_iri"):
        with repository._transaction() as connection:
            mismatched = copy.deepcopy(source)
            mismatched["id"] = "https://wrong.example/id"
            connection.execute(
                """INSERT INTO unclassified_resources
                   (resource_id, source_iri, original_jsonld, classification_reason,
                    first_observed_at, last_observed_at)
                   VALUES (?, ?, ?, 'mismatch', 'n', 'n')""",
                (normalized["stable_id"], source["id"], json.dumps(mismatched)),
            )


def test_classified_resource_cannot_be_added_to_unclassified_table(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/course")
    repository.import_resource(source, normalized)

    with pytest.raises(sqlite3.IntegrityError, match="must match source_iri"):
        with repository._transaction() as connection:
            connection.execute(
                """INSERT INTO unclassified_resources
                   (resource_id, source_iri, original_jsonld, classification_reason,
                    first_observed_at, last_observed_at)
                   VALUES (?, ?, ?, 'incorrect', 'n', 'n')""",
                (normalized["stable_id"], source["id"], json.dumps(source)),
            )


def test_database_persists_across_repository_instances(tmp_path: Path):
    path = tmp_path / "repo.sqlite"
    first = make_repository(path)
    source, normalized = make_record()
    first.import_resource(source, normalized)

    restarted = make_repository(path)

    assert restarted.stats()["courses"] == 1
    assert restarted.get_resource(normalized["stable_id"])["source_iri"] == source["id"]


def test_resource_iris_are_unique_for_distinct_sources(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    records = [make_record(f"https://example.edu/resource/{i}") for i in range(2)]
    for source, normalized in records:
        repository.import_resource(source, normalized)

    resources = repository.list_resources("courses")
    assert len(resources) == 2
    assert len({row["resource_iri"] for row in resources}) == 2
    assert {row["source_iri"] for row in resources} == {record[0]["id"] for record in records}


def test_import_directory_uses_phase1_paths(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record()
    category = normalized["classification"]["container"]
    resource_id = normalized["stable_id"]
    write_resource_files(data_dir, source, normalized, category)
    write_manifest(data_dir, [{"stable_id": resource_id, "oersi_id": source["id"], "container": category}])

    result = repository.import_directory(data_dir)

    assert result == {"courses": 1, "learning_materials": 0, "unclassified": 0, "errors": 0}
    assert repository.stats()["resource_versions"] == 1


def test_import_directory_excludes_stale_files_not_in_latest_manifest(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    current_source, current_normalized = make_record("https://example.edu/current")
    stale_source, stale_normalized = make_record("https://example.edu/stale")
    write_resource_files(data_dir, current_source, current_normalized, "courses")
    write_resource_files(data_dir, stale_source, stale_normalized, "courses")
    write_manifest(
        data_dir,
        [{
            "stable_id": current_normalized["stable_id"],
            "oersi_id": current_source["id"],
            "container": "courses",
        }],
    )

    result = repository.import_directory(data_dir)

    assert result == {"courses": 1, "learning_materials": 0, "unclassified": 0, "errors": 0}
    assert repository.stats()["resources"] == 1
    assert repository.get_resource(stale_normalized["stable_id"]) is None
    assert (data_dir / "courses" / f"{stale_normalized['stable_id']}.jsonld").exists()
    stats_after_first = repository.stats()
    assert repository.import_directory(data_dir) == result
    assert repository.stats() == stats_after_first


def test_manifest_category_change_imports_latest_classification_and_ignores_old_file(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    old_source, old_normalized = make_record("https://example.edu/reclassified", "https://w3id.org/kim/hcrt/course")
    latest_source = copy.deepcopy(old_source)
    latest_source["learningResourceType"] = [{"id": "https://w3id.org/kim/hcrt/textbook"}]
    latest_normalized = normalize_metadata(latest_source)
    latest_category, latest_reason = classify_resource(latest_source)
    latest_normalized["classification"] = {"container": latest_category, "reason": latest_reason}
    write_resource_files(data_dir, old_source, old_normalized, "courses")
    write_manifest(
        data_dir,
        [{
            "stable_id": old_normalized["stable_id"],
            "oersi_id": old_source["id"],
            "container": "courses",
        }],
    )
    initial = repository.import_directory(data_dir)
    assert initial["courses"] == 1

    write_resource_files(data_dir, old_source, old_normalized, "courses")
    write_resource_files(data_dir, latest_source, latest_normalized, "learning_materials")
    write_manifest(
        data_dir,
        [{
            "stable_id": latest_normalized["stable_id"],
            "oersi_id": latest_source["id"],
            "container": "learning_materials",
        }],
    )

    result = repository.import_directory(data_dir)

    assert result == {"courses": 0, "learning_materials": 1, "unclassified": 0, "errors": 0}
    assert repository.stats()["courses"] == 1
    assert repository.stats()["learning_materials"] == 0
    imported = repository.get_resource(latest_normalized["stable_id"])
    assert imported["source_iri"] == latest_source["id"]
    assert imported["current_version"]["original_jsonld"] == old_source
    assert repository.list_pending_updates()[0]["candidate_classification"] == "learning_materials"
    assert json.loads(repository.list_pending_updates()[0]["candidate_jsonld"]) == latest_source
    assert (data_dir / "courses" / f"{old_normalized['stable_id']}.jsonld").exists()


def test_import_directory_requires_latest_complete_successful_manifest(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record()
    category = normalized["classification"]["container"]
    write_resource_files(data_dir, source, normalized, category)
    with pytest.raises(FileNotFoundError, match="Latest extraction manifest"):
        repository.import_directory(data_dir)

    write_manifest(data_dir, [{"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category}])

    summary_path = data_dir / "summary.json"
    manifest = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest["resources"] = []
    summary_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        repository.import_directory(data_dir)

    write_manifest(data_dir, [{"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category}])
    manifest = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest["extraction_error_count"] = 1
    manifest["extraction_errors"] = [{"stage": "request", "message": "test failure"}]
    summary_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="extraction errors"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0


def test_manifest_missing_resource_file_fails_without_falling_back_to_stale_files(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record()
    category = normalized["classification"]["container"]
    write_manifest(data_dir, [{"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category}])

    with pytest.raises(ValueError, match="JSON-LD file is missing"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0


def test_manifest_missing_normalized_file_fails_clearly(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record()
    category = normalized["classification"]["container"]
    write_resource_files(data_dir, source, normalized, category)
    (data_dir / "normalized" / category / f"{normalized['stable_id']}.json").unlink()
    write_manifest(data_dir, [{"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category}])

    with pytest.raises(ValueError, match="normalized metadata file is missing"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0


def test_manifest_category_must_match_jsonld_and_normalized_files(tmp_path: Path):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record(type_id="https://w3id.org/kim/hcrt/course")
    write_resource_files(data_dir, source, normalized, "courses")
    write_manifest(
        data_dir,
        [{"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": "learning_materials"}],
    )

    with pytest.raises(ValueError, match="JSON-LD file is missing"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0

    # A file exists under the manifest category, but its source type still disagrees.
    write_resource_files(data_dir, source, normalized, "learning_materials")
    with pytest.raises(ValueError, match="conflicts with the current JSON-LD classification"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0


@pytest.mark.parametrize("contradictory", [False, True])
def test_duplicate_manifest_identifiers_are_rejected(tmp_path: Path, contradictory: bool):
    repository = make_repository(tmp_path / "repo.sqlite")
    data_dir = tmp_path / "data"
    source, normalized = make_record()
    category = normalized["classification"]["container"]
    write_resource_files(data_dir, source, normalized, category)
    entry = {"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category}
    duplicate = dict(entry)
    if contradictory:
        duplicate["container"] = "learning_materials"
    write_manifest(data_dir, [entry, duplicate])

    with pytest.raises(ValueError, match="manifest has a (duplicate|contradictory) stable_id"):
        repository.import_directory(data_dir)
    assert repository.stats()["resources"] == 0
