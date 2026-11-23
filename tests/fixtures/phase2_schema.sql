-- Phase 2 schema from the merged a867a4b revision, used to exercise the in-place migration.
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
