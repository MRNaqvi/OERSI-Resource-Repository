# OERSI Resource Repository — Phase 1 and Phase 2

A small, metadata-only Python client for the official OERSI Elasticsearch search API. It stores original API search responses, writes each original OERSI resource as a top-level JSON-LD document, stores derived application metadata separately, and classifies records without downloading PDFs, videos, or course content.

## API and observed metadata

- API: `https://oersi.org/api/search/oer_data/_search`
- Official documentation: <https://oersi.org/pages/en/docs/integrate/metadata-search/>
- The API accepts an Elasticsearch JSON search body in a `POST` request. The client sends an identifiable `User-Agent`, a configurable timeout, and retries temporary HTTP failures.
- A live 20-record initial sample was inspected before implementation. In its records, the stable resource identifier was the top-level `_source.id`; title was `name`; resource types appeared in `learningResourceType` as objects containing an `id` and multilingual `prefLabel`. Other observed properties included `description`, `keywords`, `about`, `creator`, `publisher`, `inLanguage`, `license`, `mainEntityOfPage` (with nested provider), and `datePublished`. `dateModified` is read when supplied.

## Classification

Classification uses exact OERSI type identifiers from the observed `learningResourceType[].id` metadata, not title text or guessed field names:

- **Courses:** an exact `https://w3id.org/kim/hcrt/course` type ID.
- **Learning materials:** one or more recognized concrete HCRT types, including observed `video`, `textbook`, `text`, `drill_and_practice`, `worksheet`, `slide`, `audio`, and other explicit content types in `src/classification.py`.
- **Unclassified:** missing type identifiers, unknown types, and ambiguous broad types such as `other`, `portal`, and `web_page`. A resource with both a recognized course and a recognized material type is also unclassified with an explicit ambiguity reason.

The API's `type: LearningResource` is the general schema type and does not distinguish courses from learning materials; the specific `learningResourceType` IDs provide that distinction. The vocabulary is intentionally allowlisted so new or ambiguous values are not silently assigned.

## JSON-LD and output layout

Each category output is a JSON-LD 1.1 document serialized directly from the OERSI API hit's `_source` object. The source object remains at the document root, including its original `@context`, `id`, `type`, multilingual values, nested properties, and any unrecognized source properties. The extractor does not rewrite `id` as `@id` or `type` as `@type`; the official AMB context at <https://w3id.org/kim/amb/context.jsonld> defines those aliases (and `@vocab: http://schema.org/`).

- `data/courses/<stable_id>.jsonld`
- `data/learning_materials/<stable_id>.jsonld`
- `data/unclassified/<stable_id>.jsonld`
- `data/normalized/<category>/<stable_id>.json` — repository-specific `stable_id`, `oersi_id`, `classification`, and normalized fields only. It does not contain `@context` or an embedded copy of source metadata.
- `data/raw/` — full original OERSI API response bodies.
- `data/summary.json` — extraction counts, errors, per-document JSON-LD validation states, and expanded identifiers/types.

PyLD performs JSON-LD 1.1 expansion on each serialized `.jsonld` output. Validation checks JSON parsing, the presence and reference to the official AMB context, successful expansion, the expanded identifier against the original OERSI `id`, and the expanded type values against the original type terms processed using that context. Network/HTTP context retrieval failures are reported separately from invalid JSON-LD. The CLI exits nonzero if extraction, JSON-LD validation, or context resolution fails. Validation is non-mutating; failed source documents are retained unchanged for inspection and their errors are recorded in the summary. The context loader doesn't treat the HTTP `Link` header as a context URL; fetched context documents use their own `@context`. Automated tests use controlled local context loaders and mocked HTTP responses, so they do not depend on network availability.

## Setup

1. Create and activate a virtual environment using the normal VS Code Python environment workflow.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and configure an identifiable User-Agent for regular API use. Keep personal contact details out of the committed template. `.env` is ignored by Git.

## Run extraction

From the repository root, run:

```sh
python -m src.cli
```

The default run fetches a broad 20-record sample first, then makes targeted searches as needed to try to reach 10 courses and 10 learning materials, up to 100 unique records. Adjust the configuration in `.env`, or override it with CLI flags, for example:

```sh
python -m src.cli --limit 40 --sample-size 20 --target-per-category 10 --timeout 30
```

Outputs follow the layout in [JSON-LD and output layout](#json-ld-and-output-layout). Stable filenames use an `oersi-<sha256-prefix>` identifier derived only from the OERSI identifier.

A rerun overwrites records with the same stable ID; this phase does not implement immutable versions, snapshots, identity reconciliation, or update history. Existing output files not returned by a later run are not automatically deleted.

## Tests

Run automated tests from the repository root:

```sh
python -m pytest
```

## Phase 1 extraction scope

Phase 1 extraction remains metadata-only. Resource binary/content URLs may be recorded as metadata, but PDFs, videos, and complete course content are never downloaded. The Phase 1 extraction CLI remains available as `python -m src.cli`; Phase 2 persistence is provided separately by `src.repository` and `python -m src.repository_cli`.

## Phase 2 — Persistent containers and initial versions

Phase 2 adds a Python built-in `sqlite3` repository. The persistent database has exactly two containers (`courses` and `learning_materials`). Unclassified records still receive a stable resource IRI and immutable Version 1 snapshot, but their `container_id` is `NULL` and they are additionally listed in the separate unclassified review table; they are not silently assigned to either container. Each newly imported resource receives one immutable Version 1 snapshot; subsequent changed metadata or classification is recorded as a pending review candidate. Automatic version increments are not implemented.

### Configure and initialize

Set `OERSI_REPOSITORY_BASE_IRI` in `.env` to the namespace your deployment controls. The default `https://example.org/oersi/` is a development example only, not a claim that the domain is owned or dereferenceable. The database stores this namespace and refuses to open it using a different base IRI.

The repository vocabulary defaults to `<base-IRI>/vocab/`. To configure it independently, set `OERSI_REPOSITORY_VOCABULARY_IRI` or pass `--vocabulary-iri URL`; it must be a valid absolute HTTP(S) namespace and is persisted alongside the base IRI. Reopening with a different value is rejected rather than silently changing repository semantics.

```sh
python -m src.repository_cli init
```

The default database path is `data/repository.sqlite3`. Override it with `--database PATH`, the namespace with `--base-iri URL`, and the vocabulary with `--vocabulary-iri URL`; global options go before the subcommand. SQLite foreign-key enforcement is enabled for every connection. Composite foreign keys ensure a resource's current version and a version's previous version belong to the same resource. Unclassified rows reference the matching resource and source IRI. Version rows are protected by database triggers against update and delete.

### SQLite schema migration

The integrity review adds composite foreign keys and persisted vocabulary/schema-version configuration. Existing Phase 2 databases do **not** have these constraints; initialization detects their missing `schema_version` and refuses to modify them. No database is automatically reset or migrated. To preserve an existing database, make a verified backup and perform an explicit, reviewed migration that creates new tables with the current schema, copies and validates resources/versions, checks `PRAGMA foreign_key_check`, preserves the stored base and vocabulary IRIs, then atomically swaps the tables. Keep the original file until the migrated copy passes validation. A simple Phase 2 database can alternatively be re-imported from retained Phase 1 JSON-LD/normalized files into a separately named new database, but that is a rebuild and will assign new ingestion timestamps; it is not an in-place migration.

### Extract and import

Run the Phase 1 extraction if needed, then initialize and import the JSON-LD/normalized pairs:

```sh
python -m src.cli --limit 20 --sample-size 20 --target-per-category 5
python -m src.repository_cli init
python -m src.repository_cli import --data-dir data
```

Import validates each original JSON-LD with PyLD before storage. It reads `data/<category>/<stable_id>.jsonld` and its separate `data/normalized/<category>/<stable_id>.json` file. Source JSON-LD is stored byte-for-byte as the JSON text value (the JSON object is not rewritten or enriched); normalized fields and repository identity are stored separately. SQLite files and sidecars are ignored by Git.

### Inspect the repository

```sh
python -m src.repository_cli containers
python -m src.repository_cli resources courses
python -m src.repository_cli resources learning_materials
python -m src.repository_cli get oersi-<stable-id>
python -m src.repository_cli get 'https://example.org/oersi/resource/oersi-<stable-id>'
python -m src.repository_cli stats
python -m src.repository_cli unclassified
python -m src.repository_cli pending
```

`stats` reports both containers, their resource counts, unclassified records, stored versions, and pending updates. Pending candidate content is deduplicated by source IRI, canonical JSON SHA-256, and candidate classification. Canonicalization sorts object keys and uses deterministic JSON separators; this detects JSON content equality, **not full RDF/JSON-LD semantic equivalence**.

### Persistent identifiers and semantic vocabulary

With base namespace `B` and stable Phase 1 resource ID `R`, the database forms:

- Container: `B/container/{courses|learning_materials}`
- Stable resource: `B/resource/R`
- Version 1: `B/resource/R/version/1`
- Reserved named graph: `B/graph/resource/R/version/1`

The original OERSI `id` remains the resource's `source_iri` and is never replaced with the repository IRI. Resource IRIs persist independently from version IRIs. The first imported snapshot is Version 1 as observed by this repository, not a claim about the provider's original publication history.

The original JSON-LD continues to use the OERSI/AMB context. The repository vocabulary is separate from the source vocabulary and defaults to `<OERSI_REPOSITORY_BASE_IRI>/vocab/` (or can be set independently); `repo:currentVersion` denotes the current version from the stable resource to its snapshot. Future RDF export may also use `dcterms:hasVersion`, `dcterms:isVersionOf`, `prov:wasRevisionOf`, and `prov:generatedAtTime`. No `prov:wasRevisionOf` is asserted for Version 1. Named graph IRIs are reserved only: this phase adds no RDF triplestore or SPARQL endpoint. See [docs/repository-vocabulary.md](docs/repository-vocabulary.md) for term definitions.

Repeated identical imports update `last_observed_at` without duplicating resources or versions. Changed JSON or reclassification creates a pending review entry; the current Version 1 snapshot and container assignment remain unchanged. No automatic schema migration, version-history workflow, or Version 2 creation is included in Phase 2.
