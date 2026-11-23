from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.repository import CONTAINERS, Repository, metadata_hash
from tests.jsonld_fixtures import LocalAmbContextLoader
from tests.test_repository import BASE_IRI, make_record
from tests.test_version_management import MATERIAL, when, with_normalized

PHASE2_SCHEMA = Path(__file__).parent / "fixtures" / "phase2_schema.sql"


def phase2_database(path, *, journal_mode="delete"):
    base = BASE_IRI.rstrip("/")
    classified = with_normalized(make_record()[0], dateModified="2025-10-22T17:00:00-03:00")
    unclassified = make_record("https://example.edu/unknown", "https://w3id.org/kim/hcrt/other")
    candidate = with_normalized(classified[0], learningResourceType=MATERIAL)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(PHASE2_SCHEMA.read_text())
        with connection:
            connection.executemany(
                "INSERT INTO repository_config VALUES (?, ?)",
                [("schema_version", "2"), ("base_iri", base), ("vocabulary_iri", f"{base}/vocab/")],
            )
            connection.executemany(
                "INSERT INTO containers VALUES (?, ?, ?, ?, ?)",
                [
                    (key, f"{base}/container/{key}", data["name"], data["description"], when(1))
                    for key, data in CONTAINERS.items()
                ],
            )
            for source, normalized in (classified, unclassified):
                identifier = normalized["stable_id"]
                resource = f"{base}/resource/{identifier}"
                version = f"{resource}/version/1"
                is_classified = source == classified[0]
                connection.execute(
                    "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, source["id"], resource, "courses" if is_classified else None,
                     normalized["title"], normalized["source_url"], version, when(1),
                     when(2) if is_classified else when(1)),
                )
                original_jsonld = json.dumps(source, ensure_ascii=False, indent=3)
                connection.execute(
                    """INSERT INTO resource_versions (resource_id, version_number, version_iri, graph_iri,
                           original_jsonld, normalized_metadata, metadata_hash, created_at)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
                    (identifier, version, f"{base}/graph/resource/{identifier}/version/1", original_jsonld,
                     json.dumps(normalized, indent=3), metadata_hash(source), when(1)),
                )
                if not is_classified:
                    connection.execute(
                        "INSERT INTO unclassified_resources VALUES (?, ?, ?, ?, ?, ?)",
                        (identifier, source["id"], original_jsonld, normalized["classification"]["reason"],
                         when(1), when(1)),
                    )
            source, normalized = candidate
            connection.execute(
                """INSERT INTO pending_updates (update_id, resource_id, source_iri, candidate_jsonld,
                       candidate_metadata_hash, candidate_classification, detected_at)
                   VALUES (12, ?, ?, ?, ?, 'learning_materials', ?)""",
                (normalized["stable_id"], source["id"], json.dumps(source, ensure_ascii=False, indent=3),
                 metadata_hash(source), when(2)),
            )
    return classified, unclassified, candidate


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_migration_preserves_populated_phase2_values_and_makes_a_consistent_backup(
    tmp_path, journal_mode
):
    path = tmp_path / "phase2.sqlite"
    backup = tmp_path / "backup.sqlite"
    classified, unclassified, candidate = phase2_database(path, journal_mode=journal_mode)
    repository = Repository(path, context_loader=LocalAmbContextLoader())
    identifier = classified[1]["stable_id"]
    with pytest.raises(RuntimeError, match="explicit migration"):
        repository.initialize()

    repository.migrate(backup)
    repository.initialize()
    assert backup.is_file()
    with closing(sqlite3.connect(backup)) as saved, closing(sqlite3.connect(path)) as migrated:
        assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = "SELECT config_value FROM repository_config WHERE config_key = 'schema_version'"
        assert saved.execute(sql).fetchone() == ("2",)
        assert migrated.execute(sql).fetchone() == ("3",)
        fields = """
            SELECT resource_id, version_number, version_iri, graph_iri, original_jsonld,
                   normalized_metadata, metadata_hash, created_at, previous_version_iri
            FROM resource_versions ORDER BY version_id
        """
        assert migrated.execute(fields).fetchall() == saved.execute(fields).fetchall()
        fields = """
            SELECT update_id, resource_id, source_iri, candidate_jsonld, candidate_metadata_hash,
                   candidate_classification, detected_at, status FROM pending_updates
        """
        assert migrated.execute(fields).fetchall() == saved.execute(fields).fetchall()
        assert migrated.execute("SELECT * FROM unclassified_resources").fetchall() == (
            saved.execute("SELECT * FROM unclassified_resources").fetchall()
        )

    initial = repository.get_current_version(identifier)
    assert initial["original_jsonld"] == classified[0]
    assert initial["observed_at"] == initial["created_at"] == when(1)
    assert initial["source_modified_at"] == "2025-10-22T20:00:00.000000+00:00"
    assert repository.get_resource(identifier)["last_observed_at"] == when(2)
    assert repository.get_resource(unclassified[1]["stable_id"])["container_id"] is None
    accepted = repository.accept_update(12)
    assert accepted["status"] == "accepted"
    assert accepted["candidate_jsonld"] == candidate[0]
    audit, = repository.list_pending_updates(include_decided=True)
    assert json.loads(audit["candidate_normalized_metadata"]) == candidate[1]
    assert repository.get_current_version(identifier)["previous_version_iri"] == initial["version_iri"]
    assert repository.get_resource(identifier)["container_id"] == "learning_materials"
    assert repository.get_version(identifier, 1) == initial

    # A later classified metadata update still advances the migrated resource normally.
    repository.import_resource(*with_normalized(candidate[0], name="Post-migration update"), observed_at=when(3))
    assert len(repository.list_versions(identifier)) == 3


def test_migration_does_not_overwrite_backup_or_modify_database_on_backup_failure(tmp_path):
    path = tmp_path / "phase2.sqlite"
    phase2_database(path)
    backup = tmp_path / "already-exists.sqlite"
    backup.write_bytes(b"existing user backup")
    before = path.read_bytes()
    repository = Repository(path)
    with pytest.raises(FileExistsError, match="Backup already exists"):
        repository.migrate(backup)
    assert backup.read_bytes() == b"existing user backup"
    assert path.read_bytes() == before


def test_failed_schema_change_rolls_back_and_preserves_backup(tmp_path):
    path = tmp_path / "phase2.sqlite"
    phase2_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE phase2_pending_updates (marker TEXT)")
    backup = tmp_path / "backup.sqlite"
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="already another table"):
        Repository(path).migrate(backup)
    assert backup.is_file()
    assert path.read_bytes() == before
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert saved.execute("SELECT COUNT(*) FROM pending_updates").fetchone() == (1,)


def test_migrated_candidate_requires_a_fresh_observation_if_last_state_is_unknown(tmp_path):
    path = tmp_path / "phase2.sqlite"
    classified, _, candidate = phase2_database(path)
    identifier = classified[1]["stable_id"]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE resources SET last_observed_at = ? WHERE resource_id = ?", (when(3), identifier)
        )
    repository = Repository(path, context_loader=LocalAmbContextLoader())
    repository.migrate(tmp_path / "backup.sqlite")
    with pytest.raises(ValueError, match="stale"):
        repository.accept_update(12)
    repository.import_resource(*candidate, observed_at=when(4))
    assert repository.accept_update(12)["status"] == "accepted"
    assert repository.get_current_version(identifier)["observed_at"] == when(4)
