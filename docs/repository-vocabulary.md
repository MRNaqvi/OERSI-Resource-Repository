# OERSI repository vocabulary

The repository namespace represents stable resources, immutable metadata snapshots, and their provenance. The original OERSI JSON-LD continues to use its source vocabulary and AMB context. Repository IRIs and relationships are stored in SQLite and are ready for a future RDF export.

## Resource and version identifiers

With configured base `B`, stable resource ID `R`, and sequential version number `N`:

| Entity | IRI |
| --- | --- |
| Stable resource | `B/resource/R` |
| Metadata snapshot | `B/resource/R/version/N` |
| Reserved named graph | `B/graph/resource/R/version/N` |

The resource IRI remains stable across metadata changes, classification decisions, and content reversions. Each version IRI and its reserved graph IRI identify one immutable snapshot. Version 1 is the first state recorded by this repository. The source's own `id` remains in the snapshot and is stored separately as `source_iri`.

The `repo:` vocabulary namespace defaults to `B/vocab/`. Set `OERSI_REPOSITORY_VOCABULARY_IRI` or use `--vocabulary-iri` to configure it independently. Reopen the database with the same saved base and vocabulary namespaces.

## Semantic relationships

| Predicate | Subject | Object |
| --- | --- | --- |
| `dcterms:hasVersion` | Stable resource | Any of its immutable versions |
| `dcterms:isVersionOf` | Immutable version | Its stable resource |
| `prov:wasRevisionOf` | Version 2 or later | Its immediately preceding version |
| `prov:generatedAtTime` | Immutable version | UTC repository version creation time, `created_at` |
| `repo:currentVersion` | Stable resource | Its single current immutable version |

Version 1 has a null predecessor. An accepted review and an automatic metadata revision both append to the same version chain. An observation that reverts to earlier metadata gets a new IRI and links to the preceding current version.

## Time and content provenance

- `observed_at` identifies when the source metadata was observed, based on the extraction manifest. The default `at-time` query uses this timestamp. It can show a review accepted later at the time the candidate was observed.
- `created_at` identifies when the repository wrote the version. `at-time --basis repository` uses this timestamp to find the highest version accepted or automatically created by the requested instant. New review acceptances record the same timestamp on the decision and its version. This is a recorded transaction time. Separate commit visibility timestamps are unavailable.
- `source_modified_at` carries a source-reported modification instant normalized to UTC, when the source provides a time zone. The original source value stays in the JSON-LD.
- Migrated Phase 2 Version 1 snapshots use their recorded repository creation time as the observation boundary because Phase 2 did not record extraction instants on versions.

Snapshots store the source JSON-LD structure and normalized application metadata separately. The SHA-256 content hash tracks canonical JSON equality and preserves differences such as array order. RDF graph equivalence would require separate canonicalization. Named graph IRIs are identifiers reserved for later storage and query support.
