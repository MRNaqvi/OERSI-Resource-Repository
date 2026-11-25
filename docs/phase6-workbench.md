# OERSI repository workbench

This local Streamlit interface runs on `feature/phase6-web-interface`, based on the Phase 5 merge commit `ca79905940b0d917347813f612b29d12f3fd3504`. It uses the semantic-validation APIs from [Phase 5 PR #5](https://github.com/MRNaqvi/OERSI-Resource-Repository/pull/5).

## Start with an existing repository

Install the dependencies in a Python 3.12 environment and launch from the repository root.

```bash
python -m pip install -r requirements.txt
export OERSI_REPOSITORY_DATABASE=/path/to/repository.sqlite3
python -m streamlit run streamlit_app.py
```

The default address is `http://127.0.0.1:8501` and the default SQLite path is `data/repository.sqlite3`. The dashboard reports a missing or incompatible database and the RDF upload tools are still available. The database and deployment configuration come from the operator's environment or existing `.env` file.

| Environment variable | Purpose |
| --- | --- |
| `OERSI_REPOSITORY_DATABASE` | Existing local SQLite database |
| `OERSI_REPOSITORY_BASE_IRI` | Namespace matching the database configuration. Defaults to `https://example.org/oersi/`. |
| `OERSI_REPOSITORY_VOCABULARY_IRI` | Optional configured vocabulary IRI |
| `OERSI_RDF_CONTEXT_SNAPSHOT` and `OERSI_RDF_CONTEXT_SHA256` | Operator-approved context directory and manifest fingerprint for RDF materialization, SQLite-backed source fidelity, and accepting reviews |
| `OERSI_WORKBENCH_BACKUP_DIR` | Existing operator-controlled directory for migration backups |

## Workbench pages

| Page | Workflow |
| --- | --- |
| [Dashboard](phase6/screenshots/01-dashboard.png) | Show SQLite inventory, schema version, pending count, on-demand RDF size, and recent session validation results. |
| [Resources](phase6/screenshots/02-resources.png) | Filter by title, identifier, IRI or classification. Inspect stored metadata, provenance, immutable versions, version comparisons, and both historical clocks. |
| [Upload RDF](phase6/screenshots/03-upload.png) | Drop a TriG or N-Quads file, inspect graph sizes, predicates, classes, resource IRIs, and sample statements. Uploads belong to the browser session. |
| [Semantic Validation](phase6/screenshots/04-validation-general.png) | Use general RDF syntax checks and optional local Turtle SHACL Core shapes. [Repository mode](phase6/screenshots/05-validation-repository.png) recognizes the configured OERSI vocabulary, runs the Phase 5 checks, and shows unavailable checks as `Not evaluated`. Download reports as JSON and SHACL Turtle. |
| [SPARQL Workbench](phase6/screenshots/06-sparql.png) | Run local SELECT or ASK, load the competency-question examples and their bindings, inspect graph scopes, and download JSON or CSV. Use `GRAPH` in the query to address named graphs. |
| [Knowledge Graph](phase6/screenshots/07-knowledge-graph.png) | Select a graph and neighborhood center, then inspect a node's properties and relationships. The chart renders up to 35 nodes and 75 edges at depth two. |
| [Migrations](phase6/screenshots/08-migration.png) | Preview supported SQLite migrations and use the existing backup and integrity transaction. The RDF tab shows candidate classes and fields for a future reviewed mapping. |
| [Pending Reviews](phase6/screenshots/09-pending-reviews.png) | Inspect the original metadata, proposed metadata and changes. Confirm a decision and inspect the immutable history in Resources. |
| [Settings](phase6/screenshots/10-settings.png) | Inspect operator configuration and verify the approved context. |

To compare a file with SQLite, select **OERSI repository** and enable completeness and fidelity. Use the same namespace and resource selection as the uploaded export. The app checks the authoritative snapshot and pin using the Phase 5 validator. File-only structural validation is useful for diagnosis, but cannot establish completeness or compare RDF with the original source.

For migration, choose a fresh `.sqlite3` backup filename in the configured directory and confirm the full path shown in the app. The preview distinguishes Phase 2, the earlier Phase 3 draft, and a reviewed database. Apply uses only the existing migration routines, checks repository integrity, and preserves the original backup. The RDF mapping tab is read-only. Potential identities and classifications require a separate approved import design.

## Reproduce the demonstration

Create a new directory for the disposable benchmark and follow the environment commands printed by the tool. The demo retains the benchmark's four resources and seven versions, with a simulated pending reclassification on the synthetic course.

```bash
demo_root=$(mktemp -d)
python -m src.workbench_demo "$demo_root/demo"
```

In Resources, open **Biology 1510** and use **At a time** with `2026-01-15T12:00:00Z`. Observation time returns Version 4 and repository time returns Version 3. In Semantic Validation, load **SQLite repository**, use **OERSI repository** mode and enable completeness and fidelity. The approved context resolves offline. The unchanged seven snapshots remain the authoritative baseline.

Upload [general.trig](../examples/workbench/general.trig) from the separate simulated collection. The file has two named graphs. General validation with [general-shapes.ttl](../examples/workbench/general-shapes.ttl) reports a missing license statement. OERSI repository mode identifies the dataset as outside the configured model. The default graph remains separate from the two named graphs in the upload laboratory and workbench queries.

## Boundaries and test coverage

The service uses the existing SQLite repository, RDF export, restricted SPARQL, and semantic-validation APIs. SQLite reads use read-only transactions. The two writing workflows require explicit user confirmation. Uploading, inspecting, validating and querying RDF work on isolated in-memory datasets. The application accepts TriG, N-Quads and separate Turtle SHACL Core shapes; further dataset formats require separate parser tests.

RDF operations on uploaded data run in a child process with a 16-second wall-clock timeout. On Linux the worker applies a 768 MiB address-space ceiling and CPU limits of 12 seconds soft and 13 seconds hard before parser warm-up and request deserialization. Stricter inherited limits are retained. Python audit hooks provide best-effort checks for Python-mediated file, socket, and process operations during RDF processing. The bundled repository shapes are an allowed read. The worker runs with the local account's OS permissions. A hardened operating-system sandbox would require separate OS-level isolation. [RDFLib security considerations](https://rdflib.readthedocs.io/en/latest/security_considerations/)

Pickle is an internal protocol between the trusted Streamlit process and its short-lived worker through private stdin and stdout pipes. The parent constructs the message and encodes uploaded RDF and SHACL as bytes and queries as text. These are data fields in the pickle generated by the application, so browser content cannot supply the pickle opcode stream. The child generates its own reply, which the parent reads on the private pipe. The framing preserves RDF literal lexical forms rather than letting typed literals normalize when crossing the pipe. The protocol assumes the local application, Python environment, and dependencies are trusted.

Parsing allows TriG and N-Quads byte inputs with a fixed, non-dereferenced base IRI. The upload limits are 4 MiB, 25,000 quads, and 500 named graphs. Separate Turtle SHACL Core shapes are limited to 256 KiB and 1,500 triples. External SHACL directives, imports and advanced execution are excluded. SQLite-backed JSON-LD uses verified context pins and reports missing approvals.

SPARQL queries are limited to 32 KiB and use the Phase 4 restrictions on SERVICE, FROM, FROM NAMED, and updates. A SELECT evaluates up to 1,001 solutions at the outer boundary, after the query's own order, projection, LIMIT and OFFSET. The extra solution determines truncation, and only the first 1,000 rows reach JSON serialization and exports. ASK returns its boolean directly. An upstream sort, join or aggregation can still use substantial resources, and the row cap places no fixed byte ceiling on the response. Worker time and memory limits still apply to these operations.

The workbench is single-user and local. It has no multi-user authentication. Full RDF materialization and browsing are bounded; use a selected resource for larger repositories. Validation history and RDF uploads are session-local. Evidence reflects selected benchmark inputs and does not establish source factual accuracy. The separate installed workbench dependencies are Streamlit and Plotly. Run `python -m pytest -q` for core, service-layer, and Streamlit integration tests.
