# OERSI Resource Repository — Phase 1: Data Extraction

A small, metadata-only Python client for the official OERSI Elasticsearch search API. It stores original API search responses, derives normalized metadata, and classifies records without downloading PDFs, videos, or course content.

## API and observed metadata

- API: `https://oersi.org/api/search/oer_data/_search`
- Official documentation: <https://oersi.org/pages/en/docs/integrate/metadata-search/>
- The API accepts an Elasticsearch JSON search body in a `POST` request. The client sends an identifiable `User-Agent`, a configurable timeout, and retries temporary HTTP failures.
- A live 20-record initial sample was inspected before implementation. In its records, the stable resource identifier was the top-level `_source.id`; title was `name`; resource types appeared in `learningResourceType` as objects containing an `id` and multilingual `prefLabel`. Other observed properties included `description`, `keywords`, `about`, `creator`, `publisher`, `inLanguage`, `license`, `mainEntityOfPage` (with nested provider), and `datePublished`. `dateModified` is read when supplied.

## Classification

Classification uses exact OERSI type identifiers from the observed `learningResourceType[].id` metadata, not title text or guessed field names:

- **Courses:** an exact `https://w3id.org/kim/hcrt/course` type ID.
- **Learning materials:** one or more recognized concrete HCRT types, including observed `video`, `textbook`, `text`, `drill_and_practice`, `worksheet`, `slide`, `audio`, and other explicit content types in `src/classification.py`.
- **Unclassified:** missing type identifiers, unrecognized types, and ambiguous broad types such as `other`, `portal`, and `web_page` unless the same record also has a recognized concrete material type. An exact course type takes precedence if multiple types are present.

The API's `type: LearningResource` is the general schema type and does not distinguish courses from learning materials; the specific `learningResourceType` IDs provide that distinction. The vocabulary is intentionally allowlisted so new or ambiguous values are not silently assigned.

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

Outputs:

- `data/raw/` — full JSON responses for each API request.
- `data/courses/`, `data/learning_materials/`, and `data/unclassified/` — one normalized JSON record per resource. Each record includes `source_metadata`, preserving the original OERSI metadata object, and an `oersi-<sha256-prefix>` stable ID derived only from the OERSI identifier.
- `data/summary.json` — fetched and unique record counts, classification counts, errors, request response paths, and classification rule.

A rerun overwrites records with the same stable ID; this phase does not implement immutable versions, snapshots, identity reconciliation, or update history. Existing output files not returned by a later run are not automatically deleted.

## Tests

Run automated tests from the repository root:

```sh
python -m pytest
```

## Phase 1 scope

Only metadata is extracted. Resource binary/content URLs may be recorded as metadata, but they are never downloaded. The module boundaries (`oersi_client`, `metadata`, `classification`, and `extractor`) are kept separate to support future repository/versioning work without implementing it now.
