# Reserved OERSI Repository Vocabulary

This document describes repository-level semantics separately from the original OERSI JSON-LD/AMB vocabulary. Phase 2 stores source JSON-LD unchanged and persists normalized application metadata and identities in SQLite. It does not emit RDF triples or provide a SPARQL endpoint.

## Namespace

- Prefix: `repo:`
- Default namespace IRI: `<OERSI_REPOSITORY_BASE_IRI>/vocab/` (for example `https://example.org/oersi/vocab/` when using the example base)
- `repo:currentVersion`: relates a stable repository resource to its current immutable metadata snapshot. The IRI target is the current `version_iri`.

The example namespace is a development/example namespace. Deployments should configure a base namespace they control using `OERSI_REPOSITORY_BASE_IRI`. The vocabulary namespace defaults to the base plus `/vocab/`, or may be configured independently using `OERSI_REPOSITORY_VOCABULARY_IRI` / `--vocabulary-iri`. It is stored in the repository configuration and must match when reopening the database; a mismatch is rejected rather than silently changing the vocabulary namespace.

## Existing vocabulary terms reserved for future RDF export

- `dcterms:hasVersion` — stable resource to a version snapshot.
- `dcterms:isVersionOf` — version snapshot to stable resource.
- `prov:wasRevisionOf` — a version to its previous repository version; omitted for Version 1.
- `prov:generatedAtTime` — the time the repository generated/stored a snapshot.

Phase 2 creates a named-graph IRI for each Version 1 record and stores it in SQLite for future use, but it does not create or populate a named RDF graph. Stable resource identity and version-specific metadata remain distinct. The `resource_iri` stays constant while each version IRI denotes one immutable snapshot.
