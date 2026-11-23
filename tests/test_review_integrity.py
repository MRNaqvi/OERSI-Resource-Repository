from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing

import pytest

import src.repository as repository_module
import src.repository_cli as cli
from src.repository_schema import PENDING_TABLE, create_schema
from tests.jsonld_fixtures import LocalAmbContextLoader
from tests.test_repository import make_record, make_repository
from tests.test_repository_migration import phase2_database
from tests.test_version_management import MATERIAL, when, with_normalized

POINTER_ERROR = "current version must advance to its immediate successor"


def set_current(connection, resource_id, version_iri):
    connection.execute(
        "UPDATE resources SET current_version_iri = ? WHERE resource_id = ?",
        (version_iri, resource_id),
    )


def append_snapshot(connection, repository, identifier, number, previous_iri):
    _, version_iri, graph_iri = repository._iris(identifier, number)
    connection.execute(
        """
        INSERT INTO resource_versions (
            resource_id, version_number, version_iri, graph_iri, original_jsonld,
            normalized_metadata, metadata_hash, created_at, observed_at, previous_version_iri
        )
        SELECT resource_id, ?, ?, ?, original_jsonld, normalized_metadata, metadata_hash, ?, ?, ?
        FROM resource_versions WHERE resource_id = ? AND version_number = 1
        """,
        (number, version_iri, graph_iri, when(number + 10), when(number), previous_iri, identifier),
    )
    return version_iri


def import_course(repository):
    source, normalized = make_record()
    repository.import_resource(source, normalized, observed_at=when(1))
    return source, normalized["stable_id"]


def test_sql_pointer_allows_two_consecutive_advances_and_observation_updates(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    _, identifier = import_course(repository)
    first = repository.get_current_version(identifier)
    with repository._transaction() as connection:
        second = append_snapshot(connection, repository, identifier, 2, first["version_iri"])
        set_current(connection, identifier, second)
        third = append_snapshot(connection, repository, identifier, 3, second)
        set_current(connection, identifier, third)
        set_current(connection, identifier, third)
        connection.execute("UPDATE resources SET last_observed_at = ? WHERE resource_id = ?", (when(4), identifier))

    versions = repository.list_versions(identifier)
    assert versions[0] == first
    assert [item["version_iri"] for item in versions] == [first["version_iri"], second, third]
    assert versions[1]["previous_version_iri"] == first["version_iri"]
    assert versions[2]["previous_version_iri"] == second
    assert repository.get_resource(identifier)["last_observed_at"] == when(4)
    assert repository.get_current_version(identifier) == versions[2]


def test_sql_pointer_rejects_backward_transition_for_same_resource(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, identifier = import_course(repository)
    repository.import_resource(*with_normalized(source, description="New metadata"), observed_at=when(2))
    first, second = repository.list_versions(identifier)
    with pytest.raises(sqlite3.IntegrityError, match=POINTER_ERROR):
        with repository._transaction() as connection:
            set_current(connection, identifier, first["version_iri"])
    assert repository.get_current_version(identifier) == second
    assert repository.list_versions(identifier) == [first, second]


def test_sql_pointer_cannot_skip_to_an_existing_version(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, identifier = import_course(repository)
    repository.import_resource(*with_normalized(source, description="Version 2"), observed_at=when(2))
    repository.import_resource(*with_normalized(source, description="Version 3"), observed_at=when(3))
    first, second, third = repository.list_versions(identifier)
    with repository._transaction() as connection:
        # Reproduce a pointer that lagged behind an existing chain in the draft schema.
        connection.execute("DROP TRIGGER resources_current_version_advance")
        set_current(connection, identifier, first["version_iri"])
        create_schema(connection)

    with pytest.raises(sqlite3.IntegrityError, match=POINTER_ERROR):
        with repository._transaction() as connection:
            set_current(connection, identifier, third["version_iri"])
    assert repository.get_current_version(identifier) == first
    assert repository.list_versions(identifier) == [first, second, third]


def test_sql_pointer_requires_the_successor_to_link_to_the_old_pointer(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    _, identifier = import_course(repository)
    first = repository.get_current_version(identifier)
    with repository._transaction() as connection:
        # Create a malformed snapshot in a disposable database to isolate the pointer guard.
        connection.execute("DROP TRIGGER resource_versions_sequential_insert")
        second = append_snapshot(connection, repository, identifier, 2, None)
        create_schema(connection)

    with pytest.raises(sqlite3.IntegrityError, match=POINTER_ERROR):
        with repository._transaction() as connection:
            set_current(connection, identifier, second)
    assert repository.get_current_version(identifier) == first


@pytest.mark.parametrize(
    ("status", "has_timestamp", "has_version", "constraint"),
    [
        ("pending", True, False, "pending_decision_timestamp"),
        ("pending", False, True, "pending_accepted_version"),
        ("pending", True, True, "pending_decision_timestamp"),
        ("accepted", False, False, "pending_decision_timestamp"),
        ("accepted", False, True, "pending_decision_timestamp"),
        ("accepted", True, False, "pending_accepted_version"),
        ("rejected", False, False, "pending_decision_timestamp"),
        ("rejected", False, True, "pending_decision_timestamp"),
        ("rejected", True, True, "pending_accepted_version"),
        ("superseded", False, False, "pending_decision_timestamp"),
        ("superseded", False, True, "pending_decision_timestamp"),
        ("superseded", True, True, "pending_accepted_version"),
    ],
)
def test_sql_audit_rejects_invalid_insert_and_update_combinations(
    tmp_path, status, has_timestamp, has_version, constraint
):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, identifier = import_course(repository)
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    pending, = repository.list_pending_updates()
    values = (
        status,
        when(3) if has_timestamp else None,
        repository.get_current_version(identifier)["version_iri"] if has_version else None,
        pending["update_id"],
    )
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        with repository._transaction() as connection:
            connection.execute(
                """UPDATE pending_updates SET status = ?, decided_at = ?, accepted_version_iri = ?
                   WHERE update_id = ?""",
                values,
            )
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        with repository._transaction() as connection:
            connection.execute(
                """INSERT INTO pending_updates (
                       resource_id, source_iri, candidate_jsonld, candidate_metadata_hash,
                       candidate_classification, detected_at, observed_at, base_version_iri,
                       status, decided_at, accepted_version_iri
                   )
                   SELECT resource_id, source_iri, candidate_jsonld, candidate_metadata_hash || '-test',
                          candidate_classification, detected_at, observed_at, base_version_iri, ?, ?, ?
                   FROM pending_updates WHERE update_id = ?""",
                values,
            )
    assert repository.list_pending_updates(include_decided=True) == [pending]


def test_accepted_decision_still_requires_a_version_from_the_same_resource(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, _ = import_course(repository)
    other, normalized = make_record("https://example.edu/another-course")
    repository.import_resource(other, normalized, observed_at=when(1))
    unrelated_version = repository.get_current_version(normalized["stable_id"])
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    pending, = repository.list_pending_updates()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with repository._transaction() as connection:
            connection.execute(
                """UPDATE pending_updates SET status = 'accepted', decided_at = ?, accepted_version_iri = ?
                   WHERE update_id = ?""",
                (when(3), unrelated_version["version_iri"], pending["update_id"]),
            )
    assert repository.list_pending_updates(include_decided=True) == [pending]


def test_sql_audit_constraint_failure_rolls_back_snapshot_and_pointer(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, identifier = import_course(repository)
    first = repository.get_current_version(identifier)
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    pending, = repository.list_pending_updates()
    before = repository.get_resource(identifier)
    with pytest.raises(sqlite3.IntegrityError, match="pending_accepted_version"):
        with repository._transaction() as connection:
            second = append_snapshot(connection, repository, identifier, 2, first["version_iri"])
            set_current(connection, identifier, second)
            connection.execute(
                "UPDATE pending_updates SET status = 'accepted', decided_at = ? WHERE update_id = ?",
                (when(3), pending["update_id"]),
            )
    assert repository.get_resource(identifier) == before
    assert repository.list_versions(identifier) == [first]
    assert repository.list_pending_updates(include_decided=True) == [pending]


def test_workflow_records_decision_times_and_separate_history_bases(tmp_path, monkeypatch, capsys):
    clock = when(11)
    monkeypatch.setattr(repository_module, "utc_now", lambda: clock)
    repository = make_repository(tmp_path / "repo.sqlite")
    source, identifier = import_course(repository)
    first = repository.get_current_version(identifier)

    clock = when(12)
    material = with_normalized(source, learningResourceType=MATERIAL)
    repository.import_resource(*material, observed_at=when(2))
    initial, = repository.list_pending_updates()
    assert (initial["status"], initial["decided_at"], initial["accepted_version_iri"]) == ("pending", None, None)
    assert initial["detected_at"] == when(12)

    clock = when(13)
    repository.import_resource(*with_normalized(source), observed_at=when(3))
    old, = repository.list_pending_updates(include_decided=True)
    assert old["update_id"] == initial["update_id"]
    assert (old["status"], old["decided_at"], old["accepted_version_iri"]) == ("superseded", when(13), None)

    clock = when(14)
    repository.import_resource(*material, observed_at=when(4))
    proposal, = repository.list_pending_updates()
    clock = when(15)
    rejected = repository.reject_update(proposal["update_id"])
    assert (rejected["status"], rejected["decided_at"], rejected["accepted_version_iri"]) == (
        "rejected", when(15), None,
    )

    clock = when(16)
    repository.import_resource(
        *with_normalized(material[0], description="Revised proposal"), observed_at=when(5),
    )
    pending, = repository.list_pending_updates()
    assert repository.get_version_at_time(identifier, when(16), basis="repository") == first
    clock = when(17)
    accepted = repository.accept_update(pending["update_id"])
    second = repository.get_current_version(identifier)
    assert (accepted["status"], accepted["decided_at"], accepted["accepted_version_iri"]) == (
        "accepted", when(17), second["version_iri"],
    )
    assert second["created_at"] == when(17)
    assert second["observed_at"] == when(5)
    assert second["source_modified_at"] is None
    assert second["previous_version_iri"] == first["version_iri"]
    assert repository.get_resource(identifier)["container_id"] == "learning_materials"

    assert repository.get_version_at_time(identifier, when(5)) == second
    assert repository.get_version_at_time(identifier, when(16), basis="repository") == first
    assert repository.get_version_at_time(identifier, when(17), basis="repository") == second
    assert repository.get_version_at_time(identifier, when(10), basis="repository") is None
    assert repository.list_versions(identifier) == [first, second]

    def command(*flags):
        monkeypatch.setattr(sys, "argv", [
            "repository_cli", "--database", str(repository.database_path),
            "at-time", identifier, "--timestamp", when(16), *flags,
        ])
        assert cli.main() == 0
        return json.loads(capsys.readouterr().out)

    assert command() == command("--basis", "observation") == second
    assert command("--basis", "repository") == first


def test_phase2_pending_migrates_into_reviewed_schema_and_can_be_accepted(tmp_path):
    database = tmp_path / "phase2.sqlite"
    original, _, _ = phase2_database(database)
    repository = repository_module.Repository(database, context_loader=LocalAmbContextLoader())
    repository.migrate(tmp_path / "backup.sqlite")
    identifier = original[1]["stable_id"]
    first = repository.get_current_version(identifier)
    candidate, = repository.list_pending_updates()
    assert (candidate["status"], candidate["decided_at"], candidate["accepted_version_iri"]) == (
        "pending", None, None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="pending_accepted_version"):
        with repository._transaction() as connection:
            connection.execute(
                "UPDATE pending_updates SET status = 'accepted', decided_at = ? WHERE update_id = ?",
                (when(3), candidate["update_id"]),
            )
    accepted = repository.accept_update(candidate["update_id"])
    second = repository.get_current_version(identifier)
    assert repository.get_version(identifier, 1) == first
    assert second["previous_version_iri"] == first["version_iri"]
    assert accepted["accepted_version_iri"] == second["version_iri"]
    assert second["created_at"] == accepted["decided_at"]
    with pytest.raises(sqlite3.IntegrityError, match=POINTER_ERROR):
        with repository._transaction() as connection:
            set_current(connection, identifier, first["version_iri"])


def install_draft_schema(repository):
    draft_pending = PENDING_TABLE.split("    CONSTRAINT pending_decision_timestamp", maxsplit=1)[0] + (
        "    CHECK(status = 'accepted' OR accepted_version_iri IS NULL)\n);"
    )
    with repository._transaction() as connection:
        connection.execute("DROP TRIGGER resources_current_version_advance")
        connection.execute("ALTER TABLE pending_updates RENAME TO reviewed_pending_updates")
        connection.execute(draft_pending)
        connection.execute("INSERT INTO pending_updates SELECT * FROM reviewed_pending_updates")
        connection.execute("DROP TABLE reviewed_pending_updates")


def test_draft_upgrade_preserves_valid_review_history_and_snapshots(tmp_path):
    repository = make_repository(tmp_path / "draft.sqlite")
    source, identifier = import_course(repository)
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    pending, = repository.list_pending_updates()
    repository.accept_update(pending["update_id"])
    repository.import_resource(*with_normalized(source), observed_at=when(3))
    history = repository.list_versions(identifier)
    audit = repository.list_pending_updates(include_decided=True)
    install_draft_schema(repository)
    backup = tmp_path / "draft-backup.sqlite"

    with pytest.raises(RuntimeError, match="Phase 3 draft requires explicit migration"):
        repository.initialize()
    repository.migrate(backup)

    assert repository.list_versions(identifier) == history
    assert repository.list_pending_updates(include_decided=True) == audit
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'resources_current_version_advance'"
        ).fetchone() is None
    with pytest.raises(sqlite3.IntegrityError, match=POINTER_ERROR):
        with repository._transaction() as connection:
            set_current(connection, identifier, history[0]["version_iri"])


def test_draft_upgrade_rolls_back_invalid_audit_and_keeps_backup(tmp_path):
    repository = make_repository(tmp_path / "draft.sqlite")
    source, _ = import_course(repository)
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    install_draft_schema(repository)
    with repository._transaction() as connection:
        connection.execute("UPDATE pending_updates SET status = 'accepted'")
    before = repository.database_path.read_bytes()
    backup = tmp_path / "draft-backup.sqlite"

    with pytest.raises(sqlite3.IntegrityError, match="pending_decision_timestamp"):
        repository.migrate(backup)
    assert repository.database_path.read_bytes() == before
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT status, decided_at, accepted_version_iri FROM pending_updates"
        ).fetchone() == ("accepted", None, None)
