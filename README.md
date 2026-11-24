# OERSI Resource Repository

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
- `data/summary.json` — UTC run timestamp, extraction counts, errors, per-document JSON-LD validation results, and source metadata hashes.

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

An extraction rerun replaces the output files for IDs it observes. Older files absent from a new extraction stay on disk. Import each successful extraction into SQLite to keep a version history.

## Tests

Run automated tests from the repository root:

```sh
python -m pytest
```

## SQLite resource repository

The Phase 1 command `python -m src.cli` extracts OERSI metadata. The repository command `python -m src.repository_cli` imports that metadata into SQLite. It manages two persistent containers, `courses` and `learning_materials`. Unclassified records have stable identities and version history with a null container assignment. The `unclassified` command lists them.

### Configure and initialize

Set `OERSI_REPOSITORY_BASE_IRI` to a namespace you control. The default `https://example.org/oersi/` is for development. The repository vocabulary defaults to `<base-IRI>/vocab/`. Set `OERSI_REPOSITORY_VOCABULARY_IRI` or pass `--vocabulary-iri URL` to configure it independently. Both namespaces are stored in the database and must match on subsequent opens.

```sh
python -m src.repository_cli init
```

The default database is `data/repository.sqlite3`. Set `OERSI_REPOSITORY_DATABASE` or pass `--database PATH` to use another file. Put `--database`, `--base-iri`, and `--vocabulary-iri` before the subcommand.

### SQLite schema migration

Schema version 3 supports an explicit in-place migration from the Phase 2 integrity schema (version 2). Use the existing namespace settings and specify a fresh backup filename in an existing directory.

```sh
python -m src.repository_cli --database data/repository.sqlite3 migrate --backup data/repository.phase2-backup.sqlite3
```

Migration takes a SQLite backup before changing the schema, including any committed WAL data. It preserves the resource and Version 1 rows, IRIs, timestamps, exact stored snapshot JSON, and pending candidates. It extends the version and resource tables, replaces the pending table with a decision-aware schema, and checks foreign keys and version relationships. Migration rolls back on failure, and a completed backup remains available. To recover, point the Phase 2 code at a copy of that backup. Databases predating the numbered integrity schema require a separately reviewed migration.

Pre-correction Phase 3 databases also use the explicit `migrate --backup` command. They retain schema version 3. The upgrade adds the pointer trigger and rebuilds the pending table with the decision checks, preserving valid audit rows. A prior invalid pointer or inconsistent audit row causes migration to fail with the original database preserved. The upgrade never guesses a missing decision or accepted version.

Phase 2 had no per-version extraction timestamp. Migrated Version 1 history uses its recorded repository creation timestamp as the historical observation boundary. Legacy candidates retain their original detection time as the observation boundary. Acceptance requires evidence that the candidate was the latest observation or a fresh import of that candidate. Source-modification instants are recovered from retained metadata when available.

### Extract and import

```sh
python -m src.cli --limit 20 --sample-size 20 --target-per-category 5
python -m src.repository_cli import --data-dir data
```

Import reads only the records listed in the complete, successful `summary.json`. It checks the source and stable IDs, classification, derived metadata, and the source hash when the manifest includes one. Older Phase 1 manifests with a valid `run_at` remain supported. To keep source-file/manifest pairs consistent, finish extraction before importing from a shared working directory.

Files are read and revalidated before the SQLite write transaction, including JSON-LD context resolution. A single transaction applies the complete validated batch and records the latest successful manifest. Validation failures and database write failures leave the batch unapplied. Import counts describe the records processed from the manifest by incoming classification.

The manifest's timezone-aware `run_at` supplies the observation time. A manifest older than the last imported manifest is rejected. An identical batch at the same time is a no-op, and different content at that instant is rejected. A single-record API import uses its explicit `observed_at` or the current UTC time. For each resource, older observations are ignored, identical observations at the same time are idempotent, and conflicting content at an already recorded instant is rejected. A migrated resource with an unknown last-observation hash ignores equal-time inputs conservatively.

### Resource identity and automatic versions

A resource keeps the same `resource_iri` throughout its history. Each version is an immutable snapshot containing the original OERSI JSON-LD structure and a separate normalized application projection. SQLite serializes the JSON object, so file whitespace and encoding can differ. The source's own ID remains the `source_iri`.

- A new resource gets Version 1 and a current-version pointer.
- An unchanged observation advances the resource's last-observed timestamp without creating a version.
- A newer metadata change for a classified resource in the same container appends the next version, links to the preceding version, and advances the pointer, current title, and source URL transactionally.
- A newer reversion to historical content also appends a version. The earlier snapshot keeps its original identity.
- Classification changes and changed metadata for unclassified resources go to review. The current snapshot and container persist until acceptance.

Change detection uses SHA-256 of JSON with sorted object keys and deterministic separators. It compares JSON content, including array order. Semantically equivalent RDF graphs may have different JSON hashes.

Every new snapshot has an `observed_at` source observation and a `created_at` repository version-creation time. Both use UTC. When the source supplies a timezone-aware `dateModified`, the repository also records its UTC instant as `source_modified_at`. Missing values, dates without a time, and times without a zone leave `source_modified_at` null. The raw value remains in the original metadata.

### Current and historical retrieval

```sh
python -m src.repository_cli containers
python -m src.repository_cli resources courses
python -m src.repository_cli resources learning_materials
python -m src.repository_cli stats
python -m src.repository_cli unclassified

resource=oersi-REPLACE_WITH_RESOURCE_ID
python -m src.repository_cli get "$resource"
python -m src.repository_cli history "$resource"
python -m src.repository_cli get-version "$resource" --version 2
python -m src.repository_cli get-version "https://example.org/oersi/resource/$resource/version/2"
python -m src.repository_cli at-time "$resource" --timestamp 2026-11-23T12:00:00Z
python -m src.repository_cli at-time "$resource" --timestamp 2026-11-23T12:00:00Z --basis repository
python -m src.repository_cli compare "$resource" --from 1 --to 3
```

Resource lookups accept the stable ID or resource IRI. `history` lists snapshots in order and includes each `previous_version_iri`. The API can also fetch that predecessor. `compare` lists added, removed, and changed top-level properties in the original and normalized JSON.

- **Observation time (the default)** selects the highest version whose `observed_at` is at or before the requested UTC instant. Migrated Phase 2 snapshots use `created_at` as a fallback. A review accepted later appears retrospectively at the candidate's observation time.
- **Repository acceptance time** (`--basis repository`) selects the highest version whose recorded `created_at` is at or before the requested UTC instant. It covers both automatic versions and accepted reviews. A new review version shares its creation timestamp with the acceptance decision. The timestamp is recorded inside the transaction and does not measure the exact instant of SQLite commit visibility.

For example, if a classification change was observed at noon and accepted at 14:00 UTC, a 13:00 query after acceptance returns the new version on the observation timeline and the previous version on the repository timeline. Both timelines use inclusive boundaries and return no match before the first eligible version. Pending and rejected proposals remain in the review audit. Source-reported modification dates are preserved separately.

### Review classification and unclassified metadata changes

```sh
python -m src.repository_cli pending
python -m src.repository_cli review 1
python -m src.repository_cli accept 1
python -m src.repository_cli reject 2
python -m src.repository_cli pending --all
```

`review` shows the candidate, current snapshot, and property differences. Acceptance revalidates the candidate and checks that it still represents the latest observation against the same base version. It appends a version, updates the current pointer and container, and updates the unclassified listing in the same transaction. Rejection records a decision and leaves the resource untouched. `pending --all` also displays accepted, rejected, and superseded candidates and their decision timestamps.

Repeating an active candidate refreshes its observation time while retaining its first detection time. A newer, different observation supersedes the active candidate. A superseded candidate can open a new review if observed again. A rejected candidate stays suppressed against the same base version and can be considered again against a later base version. Repeating an accepted candidate while it is current leaves the version intact. Observing that content again against a later current version follows the usual version or review rules.

SQLite requires a decision timestamp for every resolved candidate and an accepted-version IRI exactly when the status is `accepted`. The accepted version must belong to the same resource. Pending candidates have neither field.

### Version IRIs and RDF provenance

With base namespace `B`, resource ID `R`, and version number `N`:

| Identity | IRI |
| --- | --- |
| Container | `B/container/{courses\|learning_materials}` |
| Stable resource | `B/resource/R` |
| Immutable version | `B/resource/R/version/N` |
| Reserved named graph | `B/graph/resource/R/version/N` |

SQLite enforces same-resource version references and protects snapshots against updates and deletions. Its current-pointer trigger permits only a move to the immediate successor that links back to the current version. Ordinary updates to observation metadata remain permitted. The RDF export uses the same stored IRIs and timestamps. [Repository vocabulary](docs/repository-vocabulary.md)

## RDF export and local SPARQL

SQLite remains authoritative. The RDF layer reads the repository in one read-only snapshot, then builds an in-memory RDFLib dataset. The default graph holds repository resources, containers, version history, and provenance. Each immutable version has its own primary named graph at `{base}/graph/resource/{resource_id}/version/{version_number}`. It contains the RDF interpretation of the source JSON-LD default graph. Source triples are queried with `GRAPH` and stay separate from repository metadata.

PyLD revalidates and converts the stored JSON-LD using JSON-LD 1.1 processing and the same approved offline context snapshot for both operations. The original resource `id`, `type`, and `@context` stay intact. The source IRI identifies the original entity in the named graph. The version's `dcterms:source` points there from the default graph. If a source document already names graphs, including graphs declared with nested `@graph`, those graphs get distinct version-scoped names. Repository statements record each original name so its scope stays recoverable. [JSON-LD to RDF processing](https://www.w3.org/TR/json-ld11-api/#deserialize-json-ld-to-rdf-algorithm)

### Capture and approve a context baseline

```sh
python -m src.rdf_cli acquire-contexts --output data/rdf-contexts/baseline-1
python -m src.rdf_cli inspect-contexts --input data/rdf-contexts/baseline-1
```

`acquire-contexts` explicitly uses the network. It follows PyLD's context requests for all selected stored versions and saves each original HTTP response body in `documents/<sha256>.jsonld`. The manifest records the capture time, each requested URL, its final URL after redirects, and the SHA-256 of the saved body. Add `--resource ID_OR_IRI` to capture only the dependencies of one resource's complete history. Nested contexts and relative imports resolve against the final remote document URL. Relative identifiers in resource data still use the source JSON-LD's base rules.

Review `manifest.json` and the saved documents before approving the baseline. `inspect-contexts` verifies the document hashes and prints the exact manifest fingerprint. Record that fingerprint separately and supply it explicitly on export or a SQLite-backed query. Both `--context-snapshot PATH` and `--context-sha256 SHA256` are global options. The equivalent environment settings are `OERSI_RDF_CONTEXT_SNAPSHOT` and `OERSI_RDF_CONTEXT_SHA256`.

```sh
export OERSI_RDF_CONTEXT_SNAPSHOT=data/rdf-contexts/baseline-1
export OERSI_RDF_CONTEXT_SHA256='<reviewed manifest_sha256>'
```

The exporter verifies the approved manifest fingerprint and every document hash before using the pins. Both validation and conversion use verified bytes in memory with isolated PyLD resolvers. An unrecognized URL, missing pin, or hash mismatch aborts the export. RDF export and SQLite-backed queries use no network. Keep the approved manifest and documents with the exported RDF, and record the digest printed by the export command.

When a context changes or new source records need additional contexts, acquire a candidate in a new directory and compare its URLs and document hashes with the prior baseline. Acquisition refuses to replace an existing directory. Using the new directory and its separately reviewed fingerprint explicitly approves it for a new export. Keep the older snapshot and its approved fingerprint to reproduce earlier exports.

Phase 1–3 did not record the context bytes used during ingestion. A newly acquired snapshot establishes a baseline for future RDF exports of the existing JSON-LD. It cannot recover or attest to an earlier interpretation. Separately approved baselines can yield different RDF for the same stored version and graph IRIs.

### Export and validate

```sh
python -m src.rdf_cli export --format trig --output data/rdf/repository.trig
python -m src.rdf_cli export --format nquads --output data/rdf/repository.nq
python -m src.rdf_cli validate --input data/rdf/repository.trig
python -m src.rdf_cli validate --input data/rdf/repository.nq
```

With an approved baseline configured, the exporter includes every accepted version by default. Add `--resource ID_OR_IRI` to export a single resource and its complete version history. Export writes a new file and refuses to replace an existing one. Git ignores the generated `data/rdf/` and `data/rdf-contexts/` directories. Validation parses the dataset and reports graph and quad counts. SQLite access uses the same database and namespace settings as `src.repository_cli`. Place global options such as `--database PATH`, `--base-iri IRI`, and `--vocabulary-iri IRI` before the command.

TriG and N-Quads preserve the default and nonempty named graph boundaries. TriG writes explicit named graph blocks, including empty ones. N-Quads expresses graphs through quads, and a receiving RDF library can discard empty graph registrations. The default-graph mapping still records their IRIs. Blank nodes retain their relationships within one source version, including across that version's source graphs. Their labels are local to each export and may change on reload. [RDF datasets](https://www.w3.org/TR/rdf11-concepts/#section-dataset) · [N-Quads](https://www.w3.org/TR/n-quads/)

### Run sample queries

```sh
python -m src.rdf_cli query --file queries/list-courses.rq
python -m src.rdf_cli query --input data/rdf/repository.nq --file queries/list-versions.rq
resource='https://example.org/oersi/resource/oersi-fb246966e56e7e24cec9'
python -m src.rdf_cli query --input data/rdf/repository.nq --file queries/current-metadata.rq \
  --bind-iri "resource=$resource"
python -m src.rdf_cli query --input data/rdf/repository.nq --file queries/historical-metadata.rq \
  --bind-iri "resource=$resource" --bind-integer versionNumber=1
python -m src.rdf_cli query --input data/rdf/repository.nq --file queries/metadata-at-time.rq \
  --bind-iri "resource=$resource" --bind-time at=2026-11-23T12:00:00Z
```

Choose an existing resource IRI from the listing for resource-specific queries. Without `--input`, the CLI materializes the current SQLite snapshot. With `--input`, it queries the local file independently of SQLite and the source context. The CLI supplies the configured `repo:` prefix. To use the samples in a different SPARQL tool, declare `repo:` with your repository vocabulary IRI. Typed bindings supply values without rewriting the SPARQL text.

| Sample | Result and optional bindings |
| --- | --- |
| [list-courses.rq](queries/list-courses.rq) | Current members of Courses |
| [list-learning-materials.rq](queries/list-learning-materials.rq) | Current members of Learning Materials |
| [current-metadata.rq](queries/current-metadata.rq) | Current version triples, optionally bound to `resource` |
| [historical-metadata.rq](queries/historical-metadata.rq) | Historical triples, optionally bound to `resource`, `versionNumber`, or the version IRI as `version` |
| [list-versions.rq](queries/list-versions.rq) | Version numbers, IRIs, graph IRIs, and current flags |
| [version-revisions.rq](queries/version-revisions.rq) | Immediate predecessor relationships |
| [classification-changes.rq](queries/classification-changes.rq) | Differences in classification between successive accepted versions |
| [metadata-at-time.rq](queries/metadata-at-time.rq) | Observation and repository timelines side by side. Bind `at` for a specific cutoff. Without it, the query uses the current time. |
| [source-provenance.rq](queries/source-provenance.rq) | Source IRI, observation time, version creation time, and optional source-reported instant |
| [compare-version-triples.rq](queries/compare-version-triples.rq) | Ground triples added or removed in the primary source graphs. Bind `fromVersion` and `toVersion` to override Versions 1 and 2. |

SELECT returns SPARQL Results JSON with term types, datatype IRIs, and language tags. ASK returns a JSON boolean. The local evaluator accepts SELECT and ASK. It checks the parsed query and algebra before evaluation and rejects `SERVICE`, `FROM`, `FROM NAMED`, and update syntax. Selecting triples with `GRAPH` uses only the materialized dataset. Queries that build from SQLite use the approved local context snapshot. There is no HTTP SPARQL service or separate triplestore. [RDFLib SPARQL processor](https://rdflib.readthedocs.io/en/latest/apidocs/rdflib.plugins.sparql.processor/)

### Read provenance and comparisons

Current container membership and version-specific classification are independent. A reviewed reclassification changes the current membership and adds a snapshot with its own classification. Prior snapshots retain their original classification. Unclassified resources still have history and lack a current container membership triple.

The temporal sample selects the highest eligible version in each timeline using the same rules as the SQLite CLI. Observation time uses `repo:observedAt`, including later-accepted changes retrospectively. Repository time uses `prov:generatedAtTime`, recorded when the repository created the accepted snapshot. It approximates commit visibility. Phase 2 snapshots use the existing creation-time fallback for observation time. A valid timezone-aware `dateModified` from the source is exposed separately as `repo:sourceModifiedAt`; dates without an explicit instant remain only in the source metadata.

The RDF comparison reports graph-level differences in ground triples and skips triples with blank-node terms. The Python metadata hash and JSON comparison still operate on stored JSON content. Different JSON-LD documents can yield equivalent RDF, and blank-node-aware dataset equivalence requires comparing graph structure and blank-node relationships. RDF exports materialize the whole selection in memory. Use selected-resource export or a reusable local dataset for repeated queries. JSON-LD fields without an RDF interpretation, including unresolved relative IRIs, remain in SQLite. Export reports non-IRI predicates that would require generalized RDF. Reproducing an export uses the same accepted snapshots, context baseline, and processing software. Equivalent exports can differ in blank-node labels and statement order. [Repository vocabulary](docs/repository-vocabulary.md)
