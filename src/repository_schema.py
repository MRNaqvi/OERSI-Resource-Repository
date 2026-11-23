"""SQLite schema and backup-first migration for the resource repository."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


SCHEMA_VERSION = "3"
PENDING_TABLE = """
CREATE TABLE IF NOT EXISTS pending_updates (
    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    source_iri TEXT NOT NULL,
    candidate_jsonld TEXT NOT NULL,
    candidate_normalized_metadata TEXT,
    candidate_metadata_hash TEXT NOT NULL,
    candidate_classification TEXT NOT NULL
        CHECK(candidate_classification IN ('courses', 'learning_materials', 'unclassified')),
    detected_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    base_version_iri TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'accepted', 'rejected', 'superseded')),
    decided_at TEXT,
    accepted_version_iri TEXT,
    FOREIGN KEY(resource_id, source_iri)
        REFERENCES resources(resource_id, source_iri) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(resource_id, base_version_iri) REFERENCES resource_versions(resource_id, version_iri),
    FOREIGN KEY(resource_id, accepted_version_iri) REFERENCES resource_versions(resource_id, version_iri),
    CONSTRAINT pending_decision_timestamp CHECK((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT pending_accepted_version CHECK((status = 'accepted') = (accepted_version_iri IS NOT NULL))
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS repository_config (
    config_key TEXT PRIMARY KEY,
    config_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY CHECK(container_id IN ('courses', 'learning_materials')),
    container_iri TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    source_iri TEXT NOT NULL UNIQUE,
    resource_iri TEXT NOT NULL UNIQUE,
    container_id TEXT CHECK(container_id IN ('courses', 'learning_materials'))
        REFERENCES containers(container_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    title TEXT,
    source_url TEXT,
    current_version_iri TEXT NOT NULL,
    first_imported_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    last_observed_hash TEXT,
    FOREIGN KEY(resource_id, current_version_iri)
        REFERENCES resource_versions(resource_id, version_iri)
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE(resource_id, source_iri)
);

CREATE TABLE IF NOT EXISTS resource_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    version_iri TEXT NOT NULL UNIQUE,
    graph_iri TEXT NOT NULL UNIQUE,
    original_jsonld TEXT NOT NULL,
    normalized_metadata TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    previous_version_iri TEXT,
    observed_at TEXT,
    source_modified_at TEXT,
    UNIQUE(resource_id, version_iri),
    FOREIGN KEY(resource_id, previous_version_iri)
        REFERENCES resource_versions(resource_id, version_iri)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    UNIQUE(resource_id, version_number)
);

CREATE TABLE IF NOT EXISTS unclassified_resources (
    resource_id TEXT PRIMARY KEY,
    source_iri TEXT NOT NULL UNIQUE,
    original_jsonld TEXT NOT NULL,
    classification_reason TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    FOREIGN KEY(resource_id, source_iri)
        REFERENCES resources(resource_id, source_iri)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

""" + PENDING_TABLE + """

CREATE INDEX IF NOT EXISTS pending_updates_resource_status
ON pending_updates(resource_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS pending_updates_active_candidate
ON pending_updates(resource_id, base_version_iri, candidate_metadata_hash, candidate_classification)
WHERE status = 'pending';

CREATE TRIGGER IF NOT EXISTS resources_current_version_advance
BEFORE UPDATE OF current_version_iri ON resources
WHEN NEW.current_version_iri IS NOT OLD.current_version_iri
    AND NOT EXISTS (
        SELECT 1 FROM resource_versions AS previous
        JOIN resource_versions AS successor
            ON successor.resource_id = previous.resource_id
            AND successor.version_number = previous.version_number + 1
            AND successor.previous_version_iri = previous.version_iri
        WHERE previous.resource_id = OLD.resource_id
            AND previous.version_iri = OLD.current_version_iri
            AND successor.version_iri = NEW.current_version_iri
    )
BEGIN
    SELECT RAISE(ABORT, 'current version must advance to its immediate successor');
END;

CREATE TRIGGER IF NOT EXISTS resource_versions_no_update
BEFORE UPDATE ON resource_versions
BEGIN
    SELECT RAISE(ABORT, 'resource_versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS resource_versions_no_delete
BEFORE DELETE ON resource_versions
BEGIN
    SELECT RAISE(ABORT, 'resource_versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS resource_versions_sequential_insert
BEFORE INSERT ON resource_versions
WHEN NEW.observed_at IS NULL
    OR (NEW.version_number = 1 AND NEW.previous_version_iri IS NOT NULL)
    OR (NEW.version_number > 1 AND NOT EXISTS (
        SELECT 1 FROM resources AS r
        JOIN resource_versions AS v ON v.version_iri = r.current_version_iri
        WHERE r.resource_id = NEW.resource_id AND v.version_iri = NEW.previous_version_iri
            AND v.version_number = NEW.version_number - 1
            AND COALESCE(v.observed_at, v.created_at) < NEW.observed_at
    ))
BEGIN
    SELECT RAISE(ABORT, 'version must follow the current snapshot in observation order');
END;

CREATE TRIGGER IF NOT EXISTS resource_versions_source_iri_insert
BEFORE INSERT ON resource_versions
WHEN json_valid(NEW.original_jsonld) = 0
    OR json_extract(NEW.original_jsonld, '$.id') IS NOT (
            SELECT source_iri FROM resources WHERE resource_id = NEW.resource_id
    )
BEGIN
        SELECT RAISE(ABORT, 'resource version JSON-LD id must match its resource source_iri');
END;

CREATE TRIGGER IF NOT EXISTS unclassified_source_iri_insert
BEFORE INSERT ON unclassified_resources
WHEN json_valid(NEW.original_jsonld) = 0
    OR json_extract(NEW.original_jsonld, '$.id') IS NOT NEW.source_iri
    OR (
        SELECT container_id FROM resources
        WHERE resource_id = NEW.resource_id AND source_iri = NEW.source_iri
    ) IS NOT NULL
BEGIN
        SELECT RAISE(ABORT, 'unclassified JSON-LD id must match source_iri');
END;

CREATE TRIGGER IF NOT EXISTS unclassified_source_iri_update
BEFORE UPDATE OF resource_id, source_iri, original_jsonld ON unclassified_resources
WHEN json_valid(NEW.original_jsonld) = 0
    OR json_extract(NEW.original_jsonld, '$.id') IS NOT NEW.source_iri
    OR (
        SELECT container_id FROM resources
        WHERE resource_id = NEW.resource_id AND source_iri = NEW.source_iri
    ) IS NOT NULL
BEGIN
        SELECT RAISE(ABORT, 'unclassified JSON-LD id must match source_iri');
END;
"""


def create_schema(connection: sqlite3.Connection) -> None:
    # executescript would implicitly commit the migration or initialization transaction.
    statement = ""
    for line in SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete SQLite schema statement")


def has_current_version_guard(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        """SELECT 1 FROM sqlite_master WHERE type = 'trigger'
           AND name = 'resources_current_version_advance' AND tbl_name = 'resources'"""
    ).fetchone() is not None


def check_integrity(connection: sqlite3.Connection, base_iri: str) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Repository database has foreign-key violations")
    invalid = connection.execute(
        """
        SELECT r.resource_id FROM resources AS r
        LEFT JOIN resource_versions AS current
            ON current.resource_id = r.resource_id AND current.version_iri = r.current_version_iri
        WHERE current.version_iri IS NULL OR current.version_number != (
            SELECT MAX(v.version_number) FROM resource_versions AS v WHERE v.resource_id = r.resource_id
        ) OR r.resource_iri != ? || '/resource/' || r.resource_id
        UNION ALL
        SELECT v.resource_id FROM resource_versions AS v
        JOIN resources AS r ON r.resource_id = v.resource_id
        LEFT JOIN resource_versions AS prev
            ON prev.resource_id = v.resource_id AND prev.version_iri = v.previous_version_iri
        WHERE v.version_iri != r.resource_iri || '/version/' || v.version_number
            OR v.graph_iri != ? || '/graph/resource/' || v.resource_id || '/version/' || v.version_number
            OR (v.version_number = 1 AND v.previous_version_iri IS NOT NULL)
            OR (v.version_number > 1 AND (prev.version_number IS NULL
                OR prev.version_number != v.version_number - 1))
        LIMIT 1
        """,
        (base_iri, base_iri),
    ).fetchone()
    if invalid is not None:
        raise RuntimeError(f"Repository database has invalid version relationships for {invalid[0]!r}")


def backup_database(database: Path, destination: Path) -> None:
    """Publish a consistent SQLite backup without overwriting any existing path."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Backup already exists: {destination}")
    with TemporaryDirectory(prefix=".oersi-backup-", dir=destination.parent) as directory:
        backup = Path(directory) / "repository.sqlite3"
        source_uri = f"{database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite backup failed its integrity check")
        os.chmod(backup, 0o600)
        os.link(backup, destination)


def migrate_phase2(connection: sqlite3.Connection) -> None:
    """Make additive snapshot changes and replace the pending table's global deduplication constraint."""
    connection.execute("ALTER TABLE resource_versions ADD COLUMN observed_at TEXT")
    connection.execute("ALTER TABLE resource_versions ADD COLUMN source_modified_at TEXT")
    connection.execute("ALTER TABLE resources ADD COLUMN last_observed_hash TEXT")
    connection.execute("ALTER TABLE pending_updates RENAME TO phase2_pending_updates")
    connection.execute(PENDING_TABLE)
    connection.execute(
        """
        INSERT INTO pending_updates (
            update_id, resource_id, source_iri, candidate_jsonld, candidate_metadata_hash,
            candidate_classification, detected_at, observed_at, base_version_iri, status
        )
        SELECT p.update_id, p.resource_id, p.source_iri, p.candidate_jsonld, p.candidate_metadata_hash,
            p.candidate_classification, p.detected_at, p.detected_at, r.current_version_iri, p.status
        FROM phase2_pending_updates AS p JOIN resources AS r USING (resource_id)
        """
    )
    connection.execute("DROP TABLE phase2_pending_updates")
    connection.execute(
        """
        UPDATE resources AS r SET last_observed_hash = COALESCE(
            (
                SELECT p.candidate_metadata_hash FROM pending_updates AS p
                WHERE p.resource_id = r.resource_id AND p.observed_at = r.last_observed_at
                ORDER BY p.update_id DESC LIMIT 1
            ),
            CASE WHEN r.last_observed_at = r.first_imported_at THEN (
                SELECT v.metadata_hash FROM resource_versions AS v WHERE v.version_iri = r.current_version_iri
            ) END
        )
        """
    )
    create_schema(connection)
    connection.execute(
        "UPDATE repository_config SET config_value = ? WHERE config_key = 'schema_version'",
        (SCHEMA_VERSION,),
    )


def migrate_phase3_draft(connection: sqlite3.Connection) -> None:
    """Install the reviewed schema on a pre-correction Phase 3 database."""
    connection.execute("ALTER TABLE pending_updates RENAME TO phase3_draft_pending_updates")
    connection.execute(PENDING_TABLE)
    connection.execute(
        """
        INSERT INTO pending_updates (
            update_id, resource_id, source_iri, candidate_jsonld, candidate_normalized_metadata,
            candidate_metadata_hash, candidate_classification, detected_at, observed_at,
            base_version_iri, status, decided_at, accepted_version_iri
        )
        SELECT update_id, resource_id, source_iri, candidate_jsonld, candidate_normalized_metadata,
            candidate_metadata_hash, candidate_classification, detected_at, observed_at,
            base_version_iri, status, decided_at, accepted_version_iri
        FROM phase3_draft_pending_updates
        """
    )
    connection.execute("DROP TABLE phase3_draft_pending_updates")
    create_schema(connection)
