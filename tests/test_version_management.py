from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.classification import classify_resource
from src.metadata import normalize_metadata
from src.repository import Repository, metadata_hash
from tests.jsonld_fixtures import FailingAmbContextLoader, LocalAmbContextLoader
from tests.test_repository import BASE_IRI, make_record, make_repository, write_manifest, write_resource_files

MATERIAL = [{"id": "https://w3id.org/kim/hcrt/textbook"}]


def when(day):
    return f"2026-01-{day:02}T12:00:00.000000+00:00"


def with_normalized(source, **changes):
    source = {**source, **changes}
    normalized = normalize_metadata(source)
    category, reason = classify_resource(source)
    normalized["classification"] = {"container": category, "reason": reason}
    return source, normalized


def write_batch(directory, day, *records):
    entries = []
    for source, normalized in records:
        category = normalized["classification"]["container"]
        write_resource_files(directory, source, normalized, category)
        entries.append({"stable_id": normalized["stable_id"], "oersi_id": source["id"], "container": category})
    write_manifest(directory, entries, when(day))


def test_version_chain_provenance_projections_and_reversion(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    original, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(original, normalized, observed_at=when(1))
    before = repository.get_resource(identifier)

    modified_at = "2025-12-31T17:30:20-05:00"
    second = with_normalized(
        original, description="Changed notes", name="A new title", dateModified=modified_at,
        mainEntityOfPage=[{"id": "https://example.edu/revised-course", "type": "WebPage"}],
    )
    repository.import_resource(*second, observed_at=when(2))
    updated = repository.get_resource(identifier)
    assert updated["resource_iri"] == before["resource_iri"]
    assert updated["container_id"] == before["container_id"] == "courses"
    assert updated["title"] == "A new title"
    assert updated["source_url"] == "https://example.edu/revised-course"
    assert updated["current_version"]["source_modified_at"] == "2025-12-31T22:30:20.000000+00:00"
    assert updated["current_version"]["observed_at"] == when(2)
    assert updated["current_version"]["created_at"] > when(2)
    assert updated["current_version"]["original_jsonld"]["dateModified"] == modified_at

    third = with_normalized(second[0], keywords=["example"])
    repository.import_resource(*third, observed_at=when(3))
    repository.import_resource(*third, observed_at=when(4))
    assert repository.get_current_version(identifier)["version_number"] == 3
    assert repository.get_resource(identifier)["last_observed_at"] == when(4)
    repository.import_resource(original, normalized, observed_at=when(5))

    versions = repository.list_versions(before["resource_iri"])
    assert [version["version_number"] for version in versions] == [1, 2, 3, 4]
    assert [version["observed_at"] for version in versions] == [when(day) for day in (1, 2, 3, 5)]
    assert [version["previous_version_iri"] for version in versions] == [
        None, *(version["version_iri"] for version in versions[:-1])
    ]
    for number, version in enumerate(versions, start=1):
        assert version["version_iri"] == f"{before['resource_iri']}/version/{number}"
        assert version["graph_iri"] == f"{BASE_IRI}graph/resource/{identifier}/version/{number}"
        assert repository.get_version(version["version_iri"]) == version
        assert repository.get_version(before["resource_iri"], number) == version
    assert versions[0] == before["current_version"]
    assert versions[0]["source_modified_at"] is None
    assert versions[0]["original_jsonld"] == versions[-1]["original_jsonld"]
    assert versions[0]["metadata_hash"] == versions[-1]["metadata_hash"]
    assert repository.get_resource(identifier)["current_version_iri"] == versions[-1]["version_iri"]
    assert repository.stats()["resources"] == 1
    assert repository.list_pending_updates() == []

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with repository._transaction() as connection:
            connection.execute("DELETE FROM resource_versions WHERE version_number = 2")


def test_historical_queries_use_observation_time_and_compare_json(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    original, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(original, normalized, observed_at=when(1))
    later = {**original, "description": "Simulated revision", "dateModified": "2024-05-01T12:00:00Z", "extra": None}
    del later["customSourceProperty"]
    repository.import_resource(*with_normalized(later), observed_at=when(3))

    first, second = repository.list_versions(identifier)
    before_queries = repository.database_path.read_bytes()
    assert repository.get_previous_version(first["version_iri"]) is None
    assert repository.get_previous_version(second["version_iri"]) == first
    assert repository.get_version_at_time(identifier, "2025-12-31T23:59:59Z") is None
    assert repository.get_version_at_time(identifier, when(2)) == first
    assert repository.get_version_at_time(identifier, "2026-01-03T14:00:00+02:00") == second
    with pytest.raises(ValueError, match="explicit time zone"):
        repository.get_version_at_time(identifier, "2026-01-03T12:00:00")
    assert repository.get_version("https://unknown.example/version/1") is None
    changes = repository.compare_versions(identifier, 1, 2)
    assert changes["original_jsonld"]["added"] == {"extra": None}
    assert set(changes["original_jsonld"]["removed"]) == {"customSourceProperty"}
    assert changes["original_jsonld"]["changed"]["description"] == {
        "from": original["description"], "to": later["description"],
    }
    assert changes["normalized_metadata"]["changed"]["modification_date"]["to"] == later["dateModified"]
    assert repository.database_path.read_bytes() == before_queries


def test_stale_and_equal_observations_do_not_change_current_state(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    updated = with_normalized(source, description="New")
    repository.import_resource(*updated, observed_at=when(4))
    repository.import_resource(*updated, observed_at=when(4))
    repository.import_resource(source, normalized, observed_at=when(2))
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(3))

    with pytest.raises(ValueError, match="same observation time"):
        repository.import_resource(source, normalized, observed_at=when(4))
    assert repository.get_current_version(identifier)["version_number"] == 2
    assert repository.get_resource(identifier)["last_observed_at"] == when(4)
    assert repository.list_pending_updates(include_decided=True) == []


def test_manifest_observation_order_and_equal_time_batch_identity(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    old, current = tmp_path / "old", tmp_path / "current"
    write_batch(old, 1, (source, normalized))
    repository.import_directory(old)
    changed = with_normalized(source, description="New extraction")
    write_batch(current, 3, changed)
    result = repository.import_directory(current)
    assert repository.import_directory(current) == result
    assert repository.get_current_version(identifier)["observed_at"] == when(3)
    with pytest.raises(ValueError, match="older than"):
        repository.import_directory(old)

    write_batch(current, 3, (source, normalized))
    with pytest.raises(ValueError, match="same observation time"):
        repository.import_directory(current)
    assert repository.get_current_version(identifier)["original_jsonld"] == changed[0]
    assert repository.stats()["resource_versions"] == 2


@pytest.mark.parametrize("invalid_timestamp", [None, "2026-01-01", "2026-01-01T12:00:00"])
def test_manifest_requires_timezone_aware_run_at(tmp_path, invalid_timestamp):
    repository = make_repository(tmp_path / "repo.sqlite")
    directory = tmp_path / "data"
    write_batch(directory, 1, make_record())
    path = directory / "summary.json"
    summary = json.loads(path.read_text())
    summary["run_at"] = invalid_timestamp
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="run_at"):
        repository.import_directory(directory)
    assert repository.stats()["resources"] == 0


def test_manifest_hash_rejects_mismatched_source(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    directory = tmp_path / "data"
    source, normalized = make_record()
    write_batch(directory, 1, (source, normalized))
    path = directory / "summary.json"
    summary = json.loads(path.read_text())
    summary["resources"][0]["metadata_hash"] = metadata_hash(source)
    path.write_text(json.dumps(summary))
    changed, changed_normalized = with_normalized(source, description="Different file from another extraction")
    write_resource_files(directory, changed, changed_normalized, "courses")
    with pytest.raises(ValueError, match="Manifest metadata hash"):
        repository.import_directory(directory)
    assert repository.stats()["resources"] == 0


def test_accept_classification_and_keep_audit_and_history(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    changed = with_normalized(source, learningResourceType=MATERIAL, description="Reclassified")
    repository.import_resource(*changed, observed_at=when(2))
    repository.import_resource(*changed, observed_at=when(3))

    pending, = repository.list_pending_updates()
    assert pending["observed_at"] == when(3)
    assert repository.get_resource(identifier)["container_id"] == "courses"
    assert repository.get_current_version(identifier)["version_number"] == 1
    diff = repository.review_update(pending["update_id"])
    assert diff["changes"]["normalized_metadata"]["changed"]["classification"]["to"]["container"] == (
        "learning_materials"
    )
    decision = repository.accept_update(pending["update_id"])
    assert decision["status"] == "accepted"
    assert decision["detected_at"] == pending["detected_at"]
    assert decision["decided_at"] >= decision["detected_at"]
    assert decision["candidate_jsonld"] == changed[0]
    assert repository.get_resource(identifier)["container_id"] == "learning_materials"
    first, second = repository.list_versions(identifier)
    assert second["observed_at"] == when(3)
    assert second["version_iri"] == decision["accepted_version_iri"]
    assert first["version_iri"] == decision["base_version_iri"] == second["previous_version_iri"]
    assert repository.get_version_at_time(identifier, when(2)) == first
    assert repository.get_version_at_time(identifier, when(3)) == second

    repository.import_resource(*changed, observed_at=when(4))
    assert repository.list_versions(identifier) == [first, second]
    assert repository.list_pending_updates() == []
    assert [row["status"] for row in repository.list_pending_updates(include_decided=True)] == ["accepted"]
    with pytest.raises(ValueError, match="already has status 'accepted'"):
        repository.accept_update(pending["update_id"])


def test_rejected_candidate_is_suppressed_against_same_base_only(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))

    candidate = with_normalized(source, learningResourceType=MATERIAL)
    repository.import_resource(*candidate, observed_at=when(2))
    update_id = repository.list_pending_updates()[0]["update_id"]
    rejected = repository.reject_update(update_id)
    assert rejected["status"] == "rejected"
    assert rejected["decided_at"]
    assert rejected["accepted_version_iri"] is None
    repository.import_resource(source, normalized, observed_at=when(3))
    repository.import_resource(*candidate, observed_at=when(4))
    assert repository.list_pending_updates() == []
    assert len(repository.list_versions(identifier)) == 1

    repository.import_resource(*with_normalized(source, description="New base"), observed_at=when(5))
    repository.import_resource(*candidate, observed_at=when(6))
    newer, = repository.list_pending_updates()
    assert newer["base_version_iri"].endswith("/version/2")
    assert newer["update_id"] != update_id
    assert [row["status"] for row in repository.list_pending_updates(include_decided=True)] == ["rejected", "pending"]


def test_superseded_candidate_can_recur_and_accepted_candidate_cannot_overwrite(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    material = with_normalized(source, learningResourceType=MATERIAL)
    repository.import_resource(*material, observed_at=when(2))
    superseded, = repository.list_pending_updates()
    repository.import_resource(source, normalized, observed_at=when(3))
    assert repository.list_pending_updates() == []
    repository.import_resource(*material, observed_at=when(4))
    recurring, = repository.list_pending_updates()
    assert recurring["update_id"] != superseded["update_id"]
    with pytest.raises(ValueError, match="superseded"):
        repository.accept_update(superseded["update_id"])

    repository.accept_update(recurring["update_id"])
    assert repository.get_current_version(identifier)["version_number"] == 2
    repository.import_resource(source, normalized, observed_at=when(5))
    repository.accept_update(repository.list_pending_updates()[0]["update_id"])
    repository.import_resource(*material, observed_at=when(6))
    reopening, = repository.list_pending_updates()
    assert reopening["base_version_iri"].endswith("/version/3")
    assert reopening["update_id"] != recurring["update_id"]
    assert [row["status"] for row in repository.list_pending_updates(include_decided=True)] == [
        "superseded", "accepted", "accepted", "pending",
    ]


def test_unclassified_transitions_keep_current_projection_consistent(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    original, normalized = make_record()
    identifier = normalized["stable_id"]
    unclassified = with_normalized(original, learningResourceType=[])
    repository.import_resource(*unclassified, observed_at=when(1))
    revised = with_normalized(unclassified[0], description="Uncertain record revised")
    repository.import_resource(*revised, observed_at=when(2))

    row, = repository.list_unclassified()
    assert row["first_observed_at"] == when(1)
    assert row["last_observed_at"] == when(2)
    assert json.loads(row["original_jsonld"]) == unclassified[0]
    assert repository.get_current_version(identifier)["version_number"] == 1
    repository.accept_update(repository.list_pending_updates()[0]["update_id"])
    row, = repository.list_unclassified()
    assert row["first_observed_at"] == when(1)
    assert json.loads(row["original_jsonld"]) == revised[0]

    repository.import_resource(original, normalized, observed_at=when(3))
    repository.accept_update(repository.list_pending_updates()[0]["update_id"])
    assert repository.list_unclassified() == []
    assert repository.get_resource(identifier)["container_id"] == "courses"

    repository.import_resource(*unclassified, observed_at=when(4))
    repository.accept_update(repository.list_pending_updates()[0]["update_id"])
    row, = repository.list_unclassified()
    assert row["first_observed_at"] == when(4)
    assert repository.get_resource(identifier)["container_id"] is None
    assert [version["version_number"] for version in repository.list_versions(identifier)] == [1, 2, 3, 4]


def test_accept_revalidates_and_checks_candidate_again_after_validation(tmp_path, monkeypatch):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    pending, = repository.list_pending_updates()
    failing = Repository(repository.database_path, context_loader=FailingAmbContextLoader())
    with pytest.raises(ValueError, match="Source JSON-LD validation failed"):
        failing.accept_update(pending["update_id"])
    assert repository.list_pending_updates() == [pending]

    prepare = repository._prepare_record
    def prepare_with_concurrent_update(*args, **kwargs):
        record = prepare(*args, **kwargs)
        second = Repository(repository.database_path, context_loader=LocalAmbContextLoader())
        second.import_resource(*with_normalized(source, description="Another observation"), observed_at=when(3))
        return record

    monkeypatch.setattr(repository, "_prepare_record", prepare_with_concurrent_update)
    with pytest.raises(ValueError, match="superseded"):
        repository.accept_update(pending["update_id"])
    assert repository.get_current_version(identifier)["version_number"] == 2
    assert repository.get_resource(identifier)["container_id"] == "courses"


def test_accept_rolls_back_version_pointer_and_unclassified_bookkeeping(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, learningResourceType=[]), observed_at=when(2))
    pending, = repository.list_pending_updates()
    before = repository.get_resource(identifier)
    with repository._transaction() as connection:
        connection.execute(
            """CREATE TRIGGER fail_decision BEFORE UPDATE OF status ON pending_updates
               BEGIN SELECT RAISE(ABORT, 'forced decision failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced decision failure"):
        repository.accept_update(pending["update_id"])
    assert repository.list_pending_updates() == [pending]
    assert repository.get_resource(identifier) == before
    assert repository.list_versions(identifier) == [before["current_version"]]
    assert repository.list_unclassified() == []


def test_failed_later_batch_record_rolls_back_existing_version_and_batch_watermark(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    directory = tmp_path / "data"
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    write_batch(directory, 1, (source, normalized))
    repository.import_directory(directory)
    before = repository.get_resource(identifier)
    new_source, new_normalized = make_record("https://example.edu/another")
    with repository._transaction() as connection:
        connection.execute(
            """CREATE TRIGGER fail_second_record BEFORE INSERT ON resource_versions
               WHEN NEW.version_number=1
               BEGIN SELECT RAISE(ABORT, 'forced late record failure'); END"""
        )
    write_batch(directory, 3, with_normalized(source, description="New description"), (new_source, new_normalized))

    with pytest.raises(sqlite3.IntegrityError, match="forced late record failure"):
        repository.import_directory(directory)
    assert repository.get_resource(identifier) == before
    assert repository.get_resource(new_normalized["stable_id"]) is None
    assert repository.stats()["resource_versions"] == 1
    write_batch(directory, 2, with_normalized(source, name="Later valid batch"))
    repository.import_directory(directory)
    assert repository.get_current_version(identifier)["observed_at"] == when(2)


def test_bad_jsonld_in_later_batch_record_never_partially_imports(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    directory = tmp_path / "data"
    source, normalized = make_record("https://example.edu/invalid")
    source.pop("@context")
    write_batch(directory, 1, make_record(), (source, normalized))
    with pytest.raises(ValueError, match="Source JSON-LD validation failed"):
        repository.import_directory(directory)
    assert repository.stats()["resources"] == 0


def test_concurrent_replay_produces_one_version_number(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized, observed_at=when(1))
    changed = with_normalized(source, description="Simultaneous observation")

    def import_record():
        worker = Repository(repository.database_path, context_loader=LocalAmbContextLoader())
        return worker.import_resource(*changed, observed_at=when(2))

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = [workers.submit(import_record) for _ in range(4)]
        assert [result.result() for result in results] == ["courses"] * 4
    assert [version["version_number"] for version in repository.list_versions(normalized["stable_id"])] == [1, 2]


def test_database_rejects_a_version_that_skips_its_predecessor(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="A revision"), observed_at=when(2))
    with pytest.raises(sqlite3.IntegrityError, match="follow the current snapshot"):
        with repository._transaction() as connection:
            _, version, graph = repository._iris(identifier, 3)
            connection.execute(
                """INSERT INTO resource_versions (
                       resource_id, version_number, version_iri, graph_iri, original_jsonld,
                       normalized_metadata, metadata_hash, created_at, observed_at, previous_version_iri
                   )
                   SELECT resource_id, 3, ?, ?, original_jsonld, normalized_metadata, metadata_hash,
                       ?, ?, version_iri FROM resource_versions WHERE resource_id = ? AND version_number = 1""",
                (version, graph, when(3), when(3), identifier),
            )
    with repository._connection() as connection:
        constraints = connection.execute("PRAGMA foreign_key_list(resource_versions)").fetchall()
    previous_key = next(row["id"] for row in constraints if row["from"] == "previous_version_iri")
    assert {
        (row["from"], row["to"]) for row in constraints if row["id"] == previous_key
    } == {("resource_id", "resource_id"), ("previous_version_iri", "version_iri")}
