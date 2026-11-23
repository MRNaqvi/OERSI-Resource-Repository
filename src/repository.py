"""SQLite persistence for Phase 2 resources and immutable initial snapshots.

Metadata hashes represent canonical JSON content equality (sorted object keys),
not RDF/JSON-LD semantic equivalence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from src.classification import classify_resource
from src.jsonld_validation import JsonLdContextLoader, validate_jsonld_file
from src.metadata import normalize_metadata, stable_resource_id

CONTAINERS: dict[str, dict[str, str]] = {
    "courses": {
        "name": "Courses",
        "description": "OERSI resources classified as courses by learningResourceType.",
    },
    "learning_materials": {
        "name": "Learning Materials",
        "description": "OERSI resources classified as concrete educational learning materials.",
    },
}

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

CREATE TABLE IF NOT EXISTS pending_updates (
    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_iri TEXT NOT NULL,
    candidate_jsonld TEXT NOT NULL,
    candidate_metadata_hash TEXT NOT NULL,
    candidate_classification TEXT NOT NULL CHECK(candidate_classification IN ('courses', 'learning_materials', 'unclassified')),
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(source_iri, candidate_metadata_hash, candidate_classification)
);

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    """Serialize stable JSON with sorted mapping keys and compact separators."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def metadata_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_base_iri(base_iri: str) -> str:
    """Normalize a namespace URL and reject unusable IRI bases."""
    if not isinstance(base_iri, str) or not base_iri.strip():
        raise ValueError("Repository base IRI must be a non-empty absolute URL")
    normalized = base_iri.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Repository base IRI must be an HTTP(S) URL without query or fragment")
    return normalized


def normalize_vocabulary_iri(vocabulary_iri: str) -> str:
    """Normalize the repository vocabulary namespace as an absolute HTTP IRI."""
    return normalize_base_iri(vocabulary_iri) + "/"


class Repository:
    """Transactional SQLite repository for OERSI metadata resources."""

    def __init__(
        self,
        database_path: Path | str,
        base_iri: str = "https://example.org/oersi/",
        context_loader: Any | None = None,
        vocabulary_iri: str | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.base_iri = normalize_base_iri(base_iri)
        configured_vocabulary = vocabulary_iri or f"{self.base_iri}/vocab/"
        self.vocabulary_iri = normalize_vocabulary_iri(configured_vocabulary)
        self.context_loader = context_loader or JsonLdContextLoader()

    def _connect(self) -> sqlite3.Connection:
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        statement = ""
        for line in SCHEMA.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    connection.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete SQLite schema statement")

    def initialize(self) -> None:
        """Create schema, pin namespace configuration, and insert two containers."""
        with self._transaction() as connection:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing_tables:
                config_exists = "repository_config" in existing_tables
                schema_row = None
                if config_exists:
                    config_columns = {
                        row[1] for row in connection.execute("PRAGMA table_info(repository_config)")
                    }
                    if {"config_key", "config_value"} <= config_columns:
                        schema_row = connection.execute(
                            "SELECT config_value FROM repository_config WHERE config_key = 'schema_version'"
                        ).fetchone()
                if schema_row is None:
                    raise RuntimeError(
                        "Existing Phase 2 database predates the integrity schema and was left unchanged. "
                        "See README.md 'SQLite schema migration' for the explicit safe migration procedure."
                    )
                if schema_row[0] != "2":
                    raise RuntimeError(
                        f"Unsupported repository schema version {schema_row[0]!r}; database was left unchanged"
                    )
                for required_table in (
                    "containers",
                    "resources",
                    "resource_versions",
                    "unclassified_resources",
                    "pending_updates",
                ):
                    if required_table not in existing_tables:
                        raise RuntimeError(
                            f"Repository schema version 2 is incomplete ({required_table} missing); "
                            "database was left unchanged"
                        )
                saved_base = connection.execute(
                    "SELECT config_value FROM repository_config WHERE config_key='base_iri'"
                ).fetchone()
                saved_vocab = connection.execute(
                    "SELECT config_value FROM repository_config WHERE config_key='vocabulary_iri'"
                ).fetchone()
                if saved_base is None:
                    raise RuntimeError(
                        "Existing schema version 2 database has no base IRI configuration; "
                        "database was left unchanged"
                    )
                if saved_vocab is None:
                    raise RuntimeError(
                        "Existing schema version 2 database has no vocabulary IRI configuration; "
                        "database was left unchanged"
                    )
                if saved_base[0] != self.base_iri:
                    raise ValueError(
                        f"Database namespace is {saved_base[0]!r}, "
                        f"not configured namespace {self.base_iri!r}"
                    )
                if saved_vocab[0] != self.vocabulary_iri:
                    raise ValueError(
                        f"Database vocabulary namespace is {saved_vocab[0]!r}, "
                        f"not configured vocabulary namespace {self.vocabulary_iri!r}"
                    )
            self._create_schema(connection)
            saved = connection.execute(
                "SELECT config_value FROM repository_config WHERE config_key = 'base_iri'"
            ).fetchone()
            if saved is not None and saved[0] != self.base_iri:
                raise ValueError(
                    f"Database namespace is {saved[0]!r}, not configured namespace {self.base_iri!r}"
                )
            connection.execute(
                "INSERT OR IGNORE INTO repository_config(config_key, config_value) VALUES ('base_iri', ?)",
                (self.base_iri,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO repository_config(config_key, config_value) VALUES ('vocabulary_iri', ?)",
                (self.vocabulary_iri,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO repository_config(config_key, config_value) VALUES ('schema_version', '2')"
            )
            saved_vocabulary = connection.execute(
                "SELECT config_value FROM repository_config WHERE config_key = 'vocabulary_iri'"
            ).fetchone()
            if saved_vocabulary is None or saved_vocabulary[0] != self.vocabulary_iri:
                raise ValueError(
                    f"Database vocabulary namespace is {saved_vocabulary[0] if saved_vocabulary else None!r}, "
                    f"not configured vocabulary namespace {self.vocabulary_iri!r}"
                )
            now = utc_now()
            for container_id, details in CONTAINERS.items():
                connection.execute(
                    """INSERT OR IGNORE INTO containers
                       (container_id, container_iri, name, description, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        container_id,
                        f"{self.base_iri}/container/{container_id}",
                        details["name"],
                        details["description"],
                        now,
                    ),
                )
            rows = connection.execute("SELECT container_id, container_iri FROM containers").fetchall()
            if len(rows) != 2 or {row["container_id"] for row in rows} != set(CONTAINERS):
                raise RuntimeError("Repository database must contain exactly the two supported containers")

    def _ensure_initialized(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT config_value FROM repository_config WHERE config_key = 'base_iri'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Repository database is not initialized; run init first")
        if row[0] != self.base_iri:
            raise ValueError(
                f"Database namespace is {row[0]!r}, not configured namespace {self.base_iri!r}"
            )
        vocabulary = connection.execute(
            "SELECT config_value FROM repository_config WHERE config_key = 'vocabulary_iri'"
        ).fetchone()
        if vocabulary is None or vocabulary[0] != self.vocabulary_iri:
            raise ValueError(
                f"Database vocabulary namespace is {vocabulary[0] if vocabulary else None!r}, "
                f"not configured vocabulary namespace {self.vocabulary_iri!r}"
            )
        version = connection.execute(
            "SELECT config_value FROM repository_config WHERE config_key = 'schema_version'"
        ).fetchone()
        if version is None or version[0] != "2":
            raise RuntimeError("Repository database schema version is not supported")

    def _iris(self, resource_id: str, version_number: int = 1) -> tuple[str, str, str]:
        resource_iri = f"{self.base_iri}/resource/{resource_id}"
        version_iri = f"{resource_iri}/version/{version_number}"
        graph_iri = f"{self.base_iri}/graph/resource/{resource_id}/version/{version_number}"
        return resource_iri, version_iri, graph_iri

    def import_directory(self, data_dir: Path | str) -> dict[str, int]:
        """Import generated Phase 1 JSON-LD and matching normalized records."""
        root = Path(data_dir)
        counts = {"courses": 0, "learning_materials": 0, "unclassified": 0, "errors": 0}
        for category in (*CONTAINERS.keys(), "unclassified"):
            for jsonld_path in sorted((root / category).glob("*.jsonld")):
                normalized_path = root / "normalized" / category / f"{jsonld_path.stem}.json"
                try:
                    with jsonld_path.open(encoding="utf-8") as source_file:
                        source = json.load(source_file)
                    with normalized_path.open(encoding="utf-8") as normalized_file:
                        normalized = json.load(normalized_file)
                    result = self.import_resource(source, normalized, expected_category=category)
                    counts[result] += 1
                except Exception:
                    counts["errors"] += 1
                    raise
        return counts

    def import_resource(
        self,
        source: dict[str, Any],
        normalized: dict[str, Any],
        expected_category: str | None = None,
    ) -> str:
        """Validate and transactionally import one JSON-LD resource.

        Return ``courses``, ``learning_materials``, or ``unclassified``. Existing
        snapshots are never modified; changed candidates enter pending_updates.
        """
        if not isinstance(source, dict):
            raise ValueError("Original JSON-LD root must be an object")
        source_iri = source.get("id")
        if not isinstance(source_iri, str) or not source_iri.strip():
            raise ValueError("Original OERSI JSON-LD has no non-empty id")
        if not isinstance(normalized, dict):
            raise ValueError("Normalized application metadata must be an object")
        resource_id = normalized.get("stable_id")
        if not isinstance(resource_id, str) or resource_id != stable_resource_id(source_iri):
            raise ValueError("Normalized stable_id does not match the OERSI source identifier")
        if normalized.get("oersi_id") != source_iri:
            raise ValueError("Normalized oersi_id does not match the original JSON-LD id")

        expected_normalized = normalize_metadata(source)
        for field in (
            "stable_id",
            "oersi_id",
            "title",
            "description",
            "resource_type",
            "keywords",
            "subject",
            "creator",
            "publisher",
            "language",
            "license",
            "source_url",
            "publication_date",
            "modification_date",
            "source_provider",
        ):
            if normalized.get(field) != expected_normalized.get(field):
                raise ValueError(
                    f"Normalized metadata field {field!r} does not match the original OERSI JSON-LD"
                )

        source_category, source_reason = classify_resource(source)
        supplied_classification = normalized.get("classification")
        if not isinstance(supplied_classification, dict):
            raise ValueError("Normalized metadata must include a classification object")
        if supplied_classification.get("container") != source_category:
            raise ValueError(
                "Normalized classification category does not match original OERSI resource types"
            )
        if supplied_classification.get("reason") != source_reason:
            raise ValueError(
                "Normalized classification reason does not match original OERSI resource types"
            )

        validation = validate_jsonld_file_from_object(source, source_iri, self.context_loader)
        if not validation["valid"]:
            raise ValueError(f"Source JSON-LD validation failed: {validation}")

        container, classification_reason = source_category, source_reason
        if expected_category is not None and expected_category != container:
            raise ValueError(
                f"Source classifies as {container!r}, but file is under {expected_category!r}"
            )
        incoming_category = container
        original_jsonld = json.dumps(source, ensure_ascii=False, indent=2)
        normalized_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2)
        candidate_hash = metadata_hash(source)
        now = utc_now()

        with self._transaction() as connection:
            self._ensure_initialized(connection)
            existing = connection.execute(
                "SELECT * FROM resources WHERE source_iri = ?", (source_iri,)
            ).fetchone()
            if existing is not None:
                current_version = connection.execute(
                    "SELECT metadata_hash FROM resource_versions WHERE version_iri = ?",
                    (existing["current_version_iri"],),
                ).fetchone()
                if current_version is None:
                    raise RuntimeError("Current resource version is missing")
                old_category = existing["container_id"] or "unclassified"
                if candidate_hash == current_version["metadata_hash"] and incoming_category == old_category:
                    connection.execute(
                        "UPDATE resources SET last_observed_at = ? WHERE resource_id = ?",
                        (now, existing["resource_id"]),
                    )
                    if old_category == "unclassified":
                        connection.execute(
                            "UPDATE unclassified_resources SET last_observed_at = ? WHERE resource_id = ?",
                            (now, existing["resource_id"]),
                        )
                    return incoming_category
                self._insert_pending(
                    connection, existing["resource_id"], source_iri, original_jsonld,
                    candidate_hash, incoming_category, now,
                )
                connection.execute(
                    "UPDATE resources SET last_observed_at = ? WHERE resource_id = ?",
                    (now, existing["resource_id"]),
                )
                if old_category == "unclassified":
                    connection.execute(
                        "UPDATE unclassified_resources SET last_observed_at = ? WHERE resource_id = ?",
                        (now, existing["resource_id"]),
                    )
                return incoming_category

            resource_iri, version_iri, graph_iri = self._iris(resource_id, 1)
            container_id: str | None = None
            if incoming_category != "unclassified":
                container_iri = f"{self.base_iri}/container/{incoming_category}"
                container_row = connection.execute(
                    "SELECT container_id FROM containers WHERE container_id = ? AND container_iri = ?",
                    (incoming_category, container_iri),
                ).fetchone()
                if container_row is None:
                    raise RuntimeError(f"Container {incoming_category!r} does not exist in this repository")
                container_id = incoming_category
            connection.execute(
                """INSERT INTO resources
                   (resource_id, source_iri, resource_iri, container_id, title, source_url,
                    current_version_iri, first_imported_at, last_observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resource_id, source_iri, resource_iri, container_id,
                    normalized.get("title"), normalized.get("source_url"), version_iri, now, now,
                ),
            )
            connection.execute(
                """INSERT INTO resource_versions
                   (resource_id, version_number, version_iri, graph_iri, original_jsonld,
                    normalized_metadata, metadata_hash, created_at, previous_version_iri)
                   VALUES (?, 1, ?, ?, ?, ?, ?, ?, NULL)""",
                (resource_id, version_iri, graph_iri, original_jsonld, normalized_json, candidate_hash, now),
            )
            if incoming_category == "unclassified":
                connection.execute(
                    """INSERT INTO unclassified_resources
                       (resource_id, source_iri, original_jsonld, classification_reason,
                        first_observed_at, last_observed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (resource_id, source_iri, original_jsonld, classification_reason, now, now),
                )
            return incoming_category

    @staticmethod
    def _insert_pending(
        connection: sqlite3.Connection,
        resource_id: str | None,
        source_iri: str,
        original_jsonld: str,
        candidate_hash: str,
        classification: str,
        detected_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO pending_updates
               (resource_id, source_iri, candidate_jsonld, candidate_metadata_hash,
                candidate_classification, detected_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(source_iri, candidate_metadata_hash, candidate_classification) DO NOTHING""",
            (resource_id, source_iri, original_jsonld, candidate_hash, classification, detected_at),
        )

    def containers(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return [dict(row) for row in connection.execute("SELECT * FROM containers ORDER BY container_id")]

    def list_resources(self, container_id: str) -> list[dict[str, Any]]:
        if container_id not in CONTAINERS:
            raise ValueError(f"Unknown container {container_id!r}")
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM resources WHERE container_id = ? ORDER BY resource_id", (container_id,)
                )
            ]

    def get_resource(self, identifier: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ? OR resource_iri = ?", (identifier, identifier)
            ).fetchone()
            if row is None:
                return None
            version = connection.execute(
                "SELECT * FROM resource_versions WHERE version_iri = ?", (row["current_version_iri"],)
            ).fetchone()
            result = dict(row)
            result["current_version"] = dict(version) if version else None
            if version:
                result["current_version"]["original_jsonld"] = json.loads(version["original_jsonld"])
                result["current_version"]["normalized_metadata"] = json.loads(version["normalized_metadata"])
            return result

    def stats(self) -> dict[str, int]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return {
                "containers": connection.execute("SELECT COUNT(*) FROM containers").fetchone()[0],
                "resources": connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
                "courses": connection.execute("SELECT COUNT(*) FROM resources WHERE container_id='courses'").fetchone()[0],
                "learning_materials": connection.execute(
                    "SELECT COUNT(*) FROM resources WHERE container_id='learning_materials'"
                ).fetchone()[0],
                "unclassified_resources": connection.execute(
                    "SELECT COUNT(*) FROM unclassified_resources"
                ).fetchone()[0],
                "resource_versions": connection.execute("SELECT COUNT(*) FROM resource_versions").fetchone()[0],
                "pending_updates": connection.execute(
                    "SELECT COUNT(*) FROM pending_updates WHERE status='pending'"
                ).fetchone()[0],
            }

    def list_unclassified(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM unclassified_resources ORDER BY resource_id"
                )
            ]

    def list_pending_updates(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return [dict(row) for row in connection.execute("SELECT * FROM pending_updates ORDER BY update_id")]


def validate_jsonld_file_from_object(
    source: dict[str, Any], source_iri: str, document_loader: Any
) -> dict[str, Any]:
    """Validate an in-memory object through a temporary JSON-LD serialization."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="oersi-jsonld-validate-") as temp_dir:
        path = Path(temp_dir) / "source.jsonld"
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return validate_jsonld_file(path, expected_id=source_iri, document_loader=document_loader)
