"""SQLite persistence for OERSI resources and immutable metadata history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urlsplit

from src.classification import classify_resource
from src.jsonld_validation import JsonLdContextLoader, validate_jsonld_file
from src.metadata import canonical_json, metadata_hash, normalize_metadata, stable_resource_id
from src.repository_schema import (
    SCHEMA_VERSION,
    backup_database,
    check_integrity,
    create_schema,
    has_current_version_guard,
    migrate_phase2,
    migrate_phase3_draft,
)

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def observation_time(value: Any) -> str:
    """Normalize an explicit ISO-8601 instant for SQLite ordering."""
    try:
        if isinstance(value, str):
            timestamp = datetime.fromisoformat(value)
            if timestamp.utcoffset() is not None:
                return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        pass
    raise ValueError("Expected an ISO-8601 timestamp with an explicit time zone")


def source_modified_time(value: Any) -> str | None:
    try:
        return observation_time(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ImportRecord:
    resource_id: str
    source_iri: str
    category: str
    classification_reason: str
    original_jsonld: str
    normalized_json: str
    metadata_hash: str
    title: str | None
    source_url: str | None
    source_modified_at: str | None


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

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            source = f"{self.database_path.resolve().as_uri()}?mode=ro"
        else:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            source = self.database_path
        connection = sqlite3.connect(source, uri=read_only, timeout=30, isolation_level=None)
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
        connection = self._connect(read_only=True)
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create a new Phase 3 database or validate the existing namespace."""
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            if existing:
                self._ensure_initialized(connection)
                return

            create_schema(connection)
            connection.executemany(
                "INSERT INTO repository_config(config_key, config_value) VALUES (?, ?)",
                (
                    ("base_iri", self.base_iri),
                    ("vocabulary_iri", self.vocabulary_iri),
                    ("schema_version", SCHEMA_VERSION),
                ),
            )
            now = utc_now()
            connection.executemany(
                """INSERT INTO containers (container_id, container_iri, name, description, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (key, f"{self.base_iri}/container/{key}", details["name"], details["description"], now)
                    for key, details in CONTAINERS.items()
                ),
            )

    def _ensure_initialized(self, connection: sqlite3.Connection, *, migrating: bool = False) -> str:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        config = {}
        if "repository_config" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(repository_config)")}
            if {"config_key", "config_value"} <= columns:
                config = dict(connection.execute("SELECT config_key, config_value FROM repository_config"))

        saved_version = config.get("schema_version")
        if saved_version is None:
            raise RuntimeError(
                "Database is uninitialized or predates the Phase 2 integrity schema and was left unchanged. "
                "See the README migration procedure."
            )
        if saved_version not in ("2", SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported schema version {saved_version!r} for this operation")
        if not migrating:
            if saved_version == "2":
                raise RuntimeError("Phase 2 database requires explicit migration. Run migrate --backup PATH.")
            if not has_current_version_guard(connection):
                raise RuntimeError("Phase 3 draft requires explicit migration. Run migrate --backup PATH.")
        required = {"containers", "resources", "resource_versions", "unclassified_resources", "pending_updates"}
        if missing := required - tables:
            raise RuntimeError(f"Repository schema is incomplete; missing tables: {', '.join(sorted(missing))}")
        if config.get("base_iri") != self.base_iri:
            raise ValueError(f"Database namespace is {config.get('base_iri')!r}, configured {self.base_iri!r}")
        if config.get("vocabulary_iri") != self.vocabulary_iri:
            raise ValueError(
                f"Database vocabulary namespace is {config.get('vocabulary_iri')!r}, "
                f"configured {self.vocabulary_iri!r}"
            )
        containers = dict(connection.execute("SELECT container_id, container_iri FROM containers"))
        if containers != {key: f"{self.base_iri}/container/{key}" for key in CONTAINERS}:
            raise RuntimeError("Repository must contain exactly the two configured containers")
        return saved_version

    def migrate(self, backup_path: Path | str) -> None:
        """Back up and migrate a Phase 2 or pre-correction Phase 3 database in place."""
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Repository database does not exist: {self.database_path}")
        with self._transaction() as connection:
            version = self._ensure_initialized(connection, migrating=True)
            if version == SCHEMA_VERSION and has_current_version_guard(connection):
                raise RuntimeError("Repository already has the reviewed Phase 3 schema")
            check_integrity(connection, self.base_iri)
            backup_database(self.database_path, Path(backup_path))
            if version == "2":
                migrate_phase2(connection)
            else:
                migrate_phase3_draft(connection)
            check_integrity(connection, self.base_iri)
            self._ensure_initialized(connection)

    def _iris(self, resource_id: str, version_number: int = 1) -> tuple[str, str, str]:
        resource_iri = f"{self.base_iri}/resource/{resource_id}"
        version_iri = f"{resource_iri}/version/{version_number}"
        graph_iri = f"{self.base_iri}/graph/resource/{resource_id}/version/{version_number}"
        return resource_iri, version_iri, graph_iri

    def import_directory(self, data_dir: Path | str) -> dict[str, int]:
        """Import only records in the latest successful Phase 1 summary manifest.

        Existing category files are never treated as proof of being current:
        they are considered only when referenced by ``summary.json``.
        """
        root = Path(data_dir)
        counts = {"courses": 0, "learning_materials": 0, "unclassified": 0, "errors": 0}
        manifest_path = root / "summary.json"
        observed_at, entries = self._load_extraction_manifest(manifest_path)
        records = []
        for entry in entries:
            resource_id = entry["stable_id"]
            category = entry["container"]
            jsonld_path = root / category / f"{resource_id}.jsonld"
            normalized_path = root / "normalized" / category / f"{resource_id}.json"
            if not jsonld_path.is_file():
                raise ValueError(
                    f"Latest extraction manifest lists {resource_id!r} in {category!r}, "
                    f"but its JSON-LD file is missing: {jsonld_path}"
                )
            if not normalized_path.is_file():
                raise ValueError(
                    f"Latest extraction manifest lists {resource_id!r} in {category!r}, "
                    f"but its normalized metadata file is missing: {normalized_path}"
                )
            try:
                with jsonld_path.open(encoding="utf-8") as source_file:
                    source = json.load(source_file)
                with normalized_path.open(encoding="utf-8") as normalized_file:
                    normalized = json.load(normalized_file)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read manifest resource {resource_id!r}: {exc}") from exc
            if not isinstance(source, dict):
                raise ValueError(f"Manifest JSON-LD for {resource_id!r} must have an object root")
            if source.get("id") != entry["oersi_id"]:
                raise ValueError(
                    f"Manifest OERSI identifier for {resource_id!r} does not match {jsonld_path}"
                )
            if not isinstance(normalized, dict) or normalized.get("stable_id") != resource_id:
                raise ValueError(
                    f"Manifest stable ID for {resource_id!r} does not match {normalized_path}"
                )
            if normalized.get("oersi_id") != entry["oersi_id"]:
                raise ValueError(
                    f"Manifest OERSI identifier for {resource_id!r} does not match {normalized_path}"
                )
            actual_category, _ = classify_resource(source)
            if actual_category != category:
                raise ValueError(
                    f"Latest manifest category {category!r} for {resource_id!r} conflicts with "
                    f"the current JSON-LD classification {actual_category!r}"
                )
            normalized_classification = normalized.get("classification")
            if (
                not isinstance(normalized_classification, dict)
                or normalized_classification.get("container") != category
            ):
                raise ValueError(
                    f"Latest manifest category {category!r} for {resource_id!r} conflicts with "
                    f"the normalized metadata at {normalized_path}"
                )
            record = self._prepare_record(source, normalized, expected_category=category)
            if "metadata_hash" in entry and entry["metadata_hash"] != record.metadata_hash:
                raise ValueError(f"Manifest metadata hash does not match JSON-LD for {resource_id!r}")
            records.append(record)
            counts[record.category] += 1

        fingerprint = metadata_hash(sorted((record.resource_id, record.metadata_hash) for record in records))
        with self._transaction() as connection:
            self._ensure_initialized(connection)
            previous = connection.execute(
                "SELECT config_value FROM repository_config WHERE config_key = 'latest_extraction'"
            ).fetchone()
            if previous:
                latest = json.loads(previous[0])
                if observed_at < latest["observed_at"]:
                    raise ValueError("Extraction manifest is older than the last imported extraction")
                if observed_at == latest["observed_at"]:
                    if fingerprint != latest["metadata_hash"]:
                        raise ValueError("Different extraction content has the same observation time")
                    return counts
            for record in records:
                self._apply_record(connection, record, observed_at)
            connection.execute(
                """INSERT INTO repository_config VALUES ('latest_extraction', ?)
                   ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value""",
                (json.dumps({"observed_at": observed_at, "metadata_hash": fingerprint}),),
            )
        return counts

    @staticmethod
    def _load_extraction_manifest(manifest_path: Path) -> tuple[str, list[dict[str, str]]]:
        """Validate the latest completed Phase 1 summary and return its entries."""
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"Latest extraction manifest is required but missing: {manifest_path}. "
                "Run Phase 1 extraction successfully before importing; stale directory files are not imported."
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read extraction manifest {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("Extraction manifest must have a JSON object root")

        required_counts = (
            "fetched_records",
            "unique_records",
            "extraction_error_count",
            "successfully_classified_courses",
            "successfully_classified_learning_materials",
            "unclassified_records",
        )
        for field in required_counts:
            value = manifest.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Extraction manifest has missing or invalid {field!r}")
        if manifest["extraction_error_count"] != 0 or manifest.get("extraction_errors"):
            raise ValueError(
                "Latest extraction manifest records extraction errors; complete a successful extraction before import"
            )

        unique_records = manifest["unique_records"]
        if manifest["fetched_records"] < unique_records:
            raise ValueError("Extraction manifest fetched_records is smaller than unique_records")
        resources = manifest.get("resources")
        if not isinstance(resources, list) or len(resources) != unique_records:
            raise ValueError(
                "Extraction manifest resources list is missing or incomplete "
                f"(unique_records={unique_records}, entries={len(resources) if isinstance(resources, list) else 'invalid'})"
            )
        expected_category_counts = {
            "courses": manifest["successfully_classified_courses"],
            "learning_materials": manifest["successfully_classified_learning_materials"],
            "unclassified": manifest["unclassified_records"],
        }
        if sum(expected_category_counts.values()) != unique_records:
            raise ValueError("Extraction manifest category counts do not sum to unique_records")

        validation = manifest.get("jsonld_validation")
        if not isinstance(validation, dict):
            raise ValueError("Extraction manifest has no JSON-LD validation summary")
        if (
            validation.get("invalid_jsonld_documents") != 0
            or validation.get("context_resolution_failures") != 0
            or validation.get("validated_documents") != unique_records
        ):
            raise ValueError(
                "Latest extraction manifest does not confirm successful JSON-LD validation "
                "for every unique resource"
            )
        validation_results = validation.get("results")
        if not isinstance(validation_results, list) or len(validation_results) != unique_records:
            raise ValueError("Extraction manifest JSON-LD validation results are incomplete")

        entries_by_id: dict[str, dict[str, str]] = {}
        source_ids: set[str] = set()
        category_counts = {key: 0 for key in expected_category_counts}
        for raw in resources:
            if not isinstance(raw, dict):
                raise ValueError("Extraction manifest contains a malformed resource entry")
            stable_id = raw.get("stable_id")
            oersi_id = raw.get("oersi_id")
            category = raw.get("container")
            if not isinstance(stable_id, str) or not stable_id:
                raise ValueError("Extraction manifest resource has no stable_id")
            if not isinstance(oersi_id, str) or not oersi_id.strip():
                raise ValueError(f"Extraction manifest resource {stable_id!r} has no valid oersi_id")
            if stable_id != stable_resource_id(oersi_id):
                raise ValueError(f"Extraction manifest stable_id does not match oersi_id for {stable_id!r}")
            if not isinstance(category, str) or category not in expected_category_counts:
                raise ValueError(f"Extraction manifest resource {stable_id!r} has invalid category {category!r}")
            entry = {"stable_id": stable_id, "oersi_id": oersi_id, "container": category}
            if "metadata_hash" in raw:
                digest = raw["metadata_hash"]
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError(f"Extraction manifest has invalid metadata hash for {stable_id!r}")
                entry["metadata_hash"] = digest
            if stable_id in entries_by_id:
                previous = entries_by_id[stable_id]
                kind = "duplicate" if previous == entry else "contradictory"
                raise ValueError(f"Extraction manifest has a {kind} stable_id entry {stable_id!r}")
            if oersi_id in source_ids:
                raise ValueError(f"Extraction manifest has duplicate OERSI identifier {oersi_id!r}")
            entries_by_id[stable_id] = entry
            source_ids.add(oersi_id)
            category_counts[category] += 1
        if category_counts != expected_category_counts:
            raise ValueError(
                f"Extraction manifest category counts do not match resource entries: "
                f"expected {expected_category_counts}, found {category_counts}"
            )

        results_by_id: dict[str, dict[str, Any]] = {}
        for result in validation_results:
            if not isinstance(result, dict) or result.get("status") != "valid" or result.get("valid") is not True:
                raise ValueError("Extraction manifest contains an invalid JSON-LD validation result")
            stable_id = result.get("stable_id")
            if not isinstance(stable_id, str) or stable_id not in entries_by_id:
                raise ValueError("Extraction manifest validation result references an unknown resource")
            if stable_id in results_by_id:
                raise ValueError(f"Extraction manifest has duplicate JSON-LD validation result {stable_id!r}")
            if result.get("container") != entries_by_id[stable_id]["container"]:
                raise ValueError(f"Extraction manifest validation category conflicts for {stable_id!r}")
            results_by_id[stable_id] = result
        if set(results_by_id) != set(entries_by_id):
            raise ValueError("Extraction manifest is missing JSON-LD validation results for listed resources")
        try:
            observed_at = observation_time(manifest.get("run_at"))
        except ValueError as exc:
            raise ValueError("Extraction manifest run_at must be an ISO-8601 timestamp with a time zone") from exc
        return observed_at, list(entries_by_id.values())

    def import_resource(
        self,
        source: dict[str, Any],
        normalized: dict[str, Any],
        expected_category: str | None = None,
        *,
        observed_at: str | None = None,
    ) -> str:
        """Import one observation and return its classification category."""
        observed_at = utc_now() if observed_at is None else observation_time(observed_at)
        record = self._prepare_record(source, normalized, expected_category)
        with self._transaction() as connection:
            self._ensure_initialized(connection)
            self._apply_record(connection, record, observed_at)
        return record.category

    def _prepare_record(
        self, source: dict[str, Any], normalized: dict[str, Any], expected_category: str | None
    ) -> ImportRecord:
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
        for field, expected in expected_normalized.items():
            if normalized.get(field) != expected:
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

        if expected_category is not None and expected_category != source_category:
            raise ValueError(
                f"Source classifies as {source_category!r}, but file is under {expected_category!r}"
            )
        return ImportRecord(
            resource_id=resource_id,
            source_iri=source_iri,
            category=source_category,
            classification_reason=source_reason,
            original_jsonld=json.dumps(source, ensure_ascii=False, indent=2),
            normalized_json=json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2),
            metadata_hash=metadata_hash(source),
            title=expected_normalized["title"],
            source_url=expected_normalized["source_url"],
            source_modified_at=source_modified_time(expected_normalized["modification_date"]),
        )

    def _apply_record(
        self, connection: sqlite3.Connection, record: ImportRecord, observed_at: str
    ) -> None:
        existing = connection.execute(
            """SELECT r.*, v.metadata_hash, v.version_number
               FROM resources AS r JOIN resource_versions AS v ON v.version_iri = r.current_version_iri
               WHERE r.source_iri = ?""",
            (record.source_iri,),
        ).fetchone()
        if existing is None:
            self._create_resource(connection, record, observed_at)
            return

        if observed_at <= existing["last_observed_at"]:
            if (
                observed_at == existing["last_observed_at"]
                and existing["last_observed_hash"] not in (None, record.metadata_hash)
            ):
                raise ValueError(f"Conflicting metadata at the same observation time for {record.resource_id}")
            return

        category = existing["container_id"] or "unclassified"
        if record.category == category and record.metadata_hash == existing["metadata_hash"]:
            self._supersede_pending(connection, record.resource_id)
        elif record.category == category and category != "unclassified":
            self._advance_version(
                connection, record, observed_at, existing["version_number"], existing["current_version_iri"],
                created_at=utc_now(),
            )
            self._supersede_pending(connection, record.resource_id)
        else:
            self._queue_pending(connection, record, observed_at, existing["current_version_iri"])

        connection.execute(
            """UPDATE resources SET last_observed_at = ?, last_observed_hash = ? WHERE resource_id = ?""",
            (observed_at, record.metadata_hash, record.resource_id),
        )
        if category == "unclassified":
            connection.execute(
                "UPDATE unclassified_resources SET last_observed_at = ? WHERE resource_id = ?",
                (observed_at, record.resource_id),
            )

    def _create_resource(
        self, connection: sqlite3.Connection, record: ImportRecord, observed_at: str
    ) -> None:
        resource_iri, version_iri, _ = self._iris(record.resource_id)
        created_at = utc_now()
        connection.execute(
            """INSERT INTO resources
               (resource_id, source_iri, resource_iri, container_id, title, source_url,
                current_version_iri, first_imported_at, last_observed_at, last_observed_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.resource_id, record.source_iri, resource_iri, self._container_id(record.category),
                record.title, record.source_url, version_iri, created_at, observed_at, record.metadata_hash,
            ),
        )
        self._insert_version(connection, record, observed_at, created_at, 1, None)
        if record.category == "unclassified":
            self._record_unclassified(connection, record, observed_at)

    def _insert_version(
        self, connection: sqlite3.Connection, record: ImportRecord, observed_at: str, created_at: str,
        number: int, previous_version_iri: str | None,
    ) -> str:
        _, version_iri, graph_iri = self._iris(record.resource_id, number)
        connection.execute(
            """INSERT INTO resource_versions
               (resource_id, version_number, version_iri, graph_iri, original_jsonld,
                normalized_metadata, metadata_hash, created_at, previous_version_iri,
                observed_at, source_modified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.resource_id, number, version_iri, graph_iri, record.original_jsonld, record.normalized_json,
                record.metadata_hash, created_at, previous_version_iri, observed_at, record.source_modified_at,
            ),
        )
        return version_iri

    def _advance_version(
        self, connection: sqlite3.Connection, record: ImportRecord, observed_at: str,
        current_number: int, current_iri: str, *, created_at: str,
    ) -> str:
        version_iri = self._insert_version(
            connection, record, observed_at, created_at, current_number + 1, current_iri
        )
        connection.execute(
            """UPDATE resources SET current_version_iri = ?, container_id = ?, title = ?, source_url = ?
               WHERE resource_id = ?""",
            (version_iri, self._container_id(record.category), record.title, record.source_url, record.resource_id),
        )
        return version_iri

    @staticmethod
    def _container_id(category: str) -> str | None:
        return None if category == "unclassified" else category

    @staticmethod
    def _record_unclassified(
        connection: sqlite3.Connection, record: ImportRecord, observed_at: str
    ) -> None:
        connection.execute(
            """INSERT INTO unclassified_resources
               (resource_id, source_iri, original_jsonld, classification_reason, first_observed_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(resource_id) DO UPDATE SET
                   original_jsonld = excluded.original_jsonld,
                   classification_reason = excluded.classification_reason,
                   last_observed_at = excluded.last_observed_at""",
            (record.resource_id, record.source_iri, record.original_jsonld, record.classification_reason,
             observed_at, observed_at),
        )

    @staticmethod
    def _supersede_pending(
        connection: sqlite3.Connection, resource_id: str, keep_update_id: int | None = None
    ) -> None:
        connection.execute(
            """UPDATE pending_updates SET status = 'superseded', decided_at = ?
               WHERE resource_id = ? AND status = 'pending' AND update_id IS NOT ?""",
            (utc_now(), resource_id, keep_update_id),
        )

    def _queue_pending(
        self, connection: sqlite3.Connection, record: ImportRecord, observed_at: str, current_iri: str
    ) -> None:
        matching = connection.execute(
            """SELECT update_id, status FROM pending_updates
               WHERE resource_id = ? AND base_version_iri = ?
                   AND candidate_metadata_hash = ? AND candidate_classification = ?
                   AND status IN ('pending', 'rejected')
               ORDER BY update_id DESC LIMIT 1""",
            (record.resource_id, current_iri, record.metadata_hash, record.category),
        ).fetchone()
        self._supersede_pending(connection, record.resource_id, matching["update_id"] if matching else None)
        if matching:
            if matching["status"] == "pending":
                connection.execute(
                    """UPDATE pending_updates SET observed_at = ?,
                           candidate_normalized_metadata = COALESCE(candidate_normalized_metadata, ?)
                       WHERE update_id = ?""",
                    (observed_at, record.normalized_json, matching["update_id"]),
                )
            return
        connection.execute(
            """INSERT INTO pending_updates
               (resource_id, source_iri, candidate_jsonld, candidate_normalized_metadata,
                candidate_metadata_hash, candidate_classification, detected_at, observed_at, base_version_iri)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.resource_id, record.source_iri, record.original_jsonld, record.normalized_json,
             record.metadata_hash, record.category, utc_now(), observed_at, current_iri),
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

    @staticmethod
    def _resource_row(connection: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM resources WHERE resource_id = ? OR resource_iri = ?",
            (identifier, identifier),
        ).fetchone()

    @staticmethod
    def _version_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        version = dict(row)
        version["original_jsonld"] = json.loads(row["original_jsonld"])
        version["normalized_metadata"] = json.loads(row["normalized_metadata"])
        if row["observed_at"] is None:
            # Phase 2 recorded only the repository's import time for Version 1.
            version["observed_at"] = row["created_at"]
            version["source_modified_at"] = source_modified_time(
                version["normalized_metadata"].get("modification_date")
            )
        return version

    @staticmethod
    def _version_row(connection: sqlite3.Connection, version_iri: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM resource_versions WHERE version_iri = ?", (version_iri,)
        ).fetchone()

    @staticmethod
    def _numbered_version(
        connection: sqlite3.Connection, identifier: str, number: int
    ) -> sqlite3.Row | None:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("Version number must be a positive integer")
        if number > 2**63 - 1:
            return None
        return connection.execute(
            """SELECT v.* FROM resource_versions AS v JOIN resources AS r USING (resource_id)
               WHERE (r.resource_id = ? OR r.resource_iri = ?) AND v.version_number = ?""",
            (identifier, identifier, number),
        ).fetchone()

    def get_resource(self, identifier: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            row = self._resource_row(connection, identifier)
            if row is None:
                return None
            result = dict(row)
            result["current_version"] = self._version_dict(self._version_row(connection, row["current_version_iri"]))
            return result

    def get_current_version(self, identifier: str) -> dict[str, Any] | None:
        resource = self.get_resource(identifier)
        return None if resource is None else resource["current_version"]

    def list_versions(self, identifier: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            rows = connection.execute(
                """SELECT v.* FROM resource_versions AS v JOIN resources AS r USING (resource_id)
                   WHERE r.resource_id = ? OR r.resource_iri = ? ORDER BY v.version_number""",
                (identifier, identifier),
            )
            return [self._version_dict(row) for row in rows]

    def get_version(self, identifier: str, version_number: int | None = None) -> dict[str, Any] | None:
        """Find by version IRI, or by resource ID/IRI and a version number."""
        with self._connection() as connection:
            self._ensure_initialized(connection)
            if version_number is None:
                row = self._version_row(connection, identifier)
            else:
                row = self._numbered_version(connection, identifier, version_number)
            return self._version_dict(row)

    def get_previous_version(self, version_iri: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            row = connection.execute(
                """SELECT prev.* FROM resource_versions AS prev
                   JOIN resource_versions AS v ON prev.version_iri = v.previous_version_iri
                   WHERE v.version_iri = ?""",
                (version_iri,),
            ).fetchone()
            return self._version_dict(row)

    def get_version_at_time(
        self, identifier: str, timestamp: str, *, basis: Literal["observation", "repository"] = "observation",
    ) -> dict[str, Any] | None:
        """Find the version as of an observation or recorded repository creation time."""
        if basis not in ("observation", "repository"):
            raise ValueError(f"Unknown history time basis {basis!r}")
        timestamp = observation_time(timestamp)
        with self._connection() as connection:
            self._ensure_initialized(connection)
            row = connection.execute(
                """SELECT v.* FROM resource_versions AS v JOIN resources AS r USING (resource_id)
                   WHERE (r.resource_id = ? OR r.resource_iri = ?)
                       AND CASE WHEN ? = 'repository' THEN v.created_at
                                ELSE COALESCE(v.observed_at, v.created_at) END <= ?
                   ORDER BY v.version_number DESC LIMIT 1""",
                (identifier, identifier, basis, timestamp),
            ).fetchone()
            return self._version_dict(row)

    def compare_versions(
        self, identifier: str, from_version: int, to_version: int
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            before = self._version_dict(self._numbered_version(connection, identifier, from_version))
            after = self._version_dict(self._numbered_version(connection, identifier, to_version))
        if before is None or after is None:
            raise LookupError(f"Both versions must exist for resource {identifier!r}")
        return {
            "from_version_iri": before["version_iri"],
            "to_version_iri": after["version_iri"],
            "original_jsonld": _property_changes(before["original_jsonld"], after["original_jsonld"]),
            "normalized_metadata": _property_changes(before["normalized_metadata"], after["normalized_metadata"]),
        }

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

    def list_pending_updates(self, *, include_decided: bool = False) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM pending_updates WHERE ? OR status = 'pending' ORDER BY update_id",
                    (include_decided,),
                )
            ]

    @staticmethod
    def _pending_row(connection: sqlite3.Connection, update_id: int) -> sqlite3.Row:
        if not 1 <= update_id <= 2**63 - 1:
            raise LookupError(f"Pending update {update_id} does not exist")
        row = connection.execute(
            "SELECT * FROM pending_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Pending update {update_id} does not exist")
        return row

    @staticmethod
    def _pending_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        source = json.loads(row["candidate_jsonld"])
        if row["candidate_normalized_metadata"] is None:
            # Phase 2 recorded the original JSON-LD but not the normalized candidate.
            normalized = normalize_metadata(source)
            category, reason = classify_resource(source)
            normalized["classification"] = {"container": category, "reason": reason}
        else:
            normalized = json.loads(row["candidate_normalized_metadata"])
        result["candidate_jsonld"] = source
        result["candidate_normalized_metadata"] = normalized
        return result

    @staticmethod
    def _require_pending(row: sqlite3.Row) -> None:
        if row["status"] != "pending":
            raise ValueError(f"Update {row['update_id']} already has status {row['status']!r}")

    def _applicable_candidate(
        self, connection: sqlite3.Connection, candidate: sqlite3.Row
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        self._require_pending(candidate)
        resource = self._resource_row(connection, candidate["resource_id"])
        if (
            resource["current_version_iri"] != candidate["base_version_iri"]
            or resource["last_observed_at"] != candidate["observed_at"]
            or resource["last_observed_hash"] != candidate["candidate_metadata_hash"]
        ):
            raise ValueError(f"Update {candidate['update_id']} is stale; it needs a fresh observation")
        current = self._version_row(connection, resource["current_version_iri"])
        if (
            current["metadata_hash"] == candidate["candidate_metadata_hash"]
            and resource["container_id"] == self._container_id(candidate["candidate_classification"])
        ):
            raise ValueError(f"Update {candidate['update_id']} already matches the current version")
        return resource, current

    def review_update(self, update_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            candidate = self._pending_dict(self._pending_row(connection, update_id))
            resource = self._resource_row(connection, candidate["resource_id"])
            current = self._version_dict(self._version_row(connection, resource["current_version_iri"]))
            return {
                "candidate": candidate,
                "current_version": current,
                "changes": {
                    "original_jsonld": _property_changes(current["original_jsonld"], candidate["candidate_jsonld"]),
                    "normalized_metadata": _property_changes(
                        current["normalized_metadata"], candidate["candidate_normalized_metadata"]
                    ),
                },
            }

    def accept_update(self, update_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_initialized(connection)
            row = self._pending_row(connection, update_id)
            self._applicable_candidate(connection, row)
            candidate = self._pending_dict(row)

        # The JSON-LD context loader can use the network. Validate before taking the write lock.
        record = self._prepare_record(
            candidate["candidate_jsonld"], candidate["candidate_normalized_metadata"],
            candidate["candidate_classification"],
        )
        if (
            record.metadata_hash != candidate["candidate_metadata_hash"]
            or record.resource_id != candidate["resource_id"]
            or record.source_iri != candidate["source_iri"]
        ):
            raise ValueError(f"Update {update_id} no longer matches its stored source identity or hash")

        with self._transaction() as connection:
            self._ensure_initialized(connection)
            fresh = self._pending_row(connection, update_id)
            resource, current = self._applicable_candidate(connection, fresh)
            payload_fields = (
                "resource_id", "source_iri", "candidate_jsonld", "candidate_normalized_metadata",
                "candidate_metadata_hash", "candidate_classification",
            )
            if any(fresh[key] != row[key] for key in payload_fields):
                raise ValueError(f"Update {update_id} changed during validation; review it again")
            observed_at = fresh["observed_at"]
            decided_at = utc_now()
            version_iri = self._advance_version(
                connection, record, observed_at, current["version_number"], resource["current_version_iri"],
                created_at=decided_at,
            )
            if record.category == "unclassified":
                self._record_unclassified(connection, record, observed_at)
            elif resource["container_id"] is None:
                connection.execute("DELETE FROM unclassified_resources WHERE resource_id = ?", (record.resource_id,))
            connection.execute(
                """UPDATE pending_updates
                   SET status = 'accepted', decided_at = ?, accepted_version_iri = ?,
                       candidate_normalized_metadata = COALESCE(candidate_normalized_metadata, ?)
                   WHERE update_id = ?""",
                (decided_at, version_iri, record.normalized_json, update_id),
            )
            self._supersede_pending(connection, record.resource_id)
            return self._pending_dict(self._pending_row(connection, update_id))

    def reject_update(self, update_id: int) -> dict[str, Any]:
        with self._transaction() as connection:
            self._ensure_initialized(connection)
            self._require_pending(self._pending_row(connection, update_id))
            connection.execute(
                "UPDATE pending_updates SET status = 'rejected', decided_at = ? WHERE update_id = ?",
                (utc_now(), update_id),
            )
            return self._pending_dict(self._pending_row(connection, update_id))


def _property_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": {key: after[key] for key in sorted(after.keys() - before.keys())},
        "removed": {key: before[key] for key in sorted(before.keys() - after.keys())},
        "changed": {
            key: {"from": before[key], "to": after[key]}
            for key in sorted(before.keys() & after.keys())
            if canonical_json(before[key]) != canonical_json(after[key])
        },
    }


def validate_jsonld_file_from_object(
    source: dict[str, Any], source_iri: str, document_loader: Any
) -> dict[str, Any]:
    """Validate an in-memory object through a temporary JSON-LD serialization."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="oersi-jsonld-validate-") as temp_dir:
        path = Path(temp_dir) / "source.jsonld"
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return validate_jsonld_file(path, expected_id=source_iri, document_loader=document_loader)
