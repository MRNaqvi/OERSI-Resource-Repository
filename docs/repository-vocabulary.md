# OERSI repository vocabulary

The default RDF graph describes the repository's stable resources, immutable metadata snapshots, and provenance. Per-version named graphs hold the RDF interpretation of the original OERSI JSON-LD. Source predicates and types come from JSON-LD 1.1 processing with the AMB context. The repository vocabulary is a project-defined namespace for the terms listed below. [JSON-LD 1.1 RDF processing](https://www.w3.org/TR/json-ld11-api/#deserialize-json-ld-to-rdf-algorithm) · [RDF datasets](https://www.w3.org/TR/rdf11-concepts/#section-dataset)

## Identity and graph scope

| Entity | IRI |
| --- | --- |
| Stable resource | `{base}/resource/{resource_id}` |
| Metadata snapshot | `{base}/resource/{resource_id}/version/{number}` |
| Version's primary source graph | `{base}/graph/resource/{resource_id}/version/{number}` |
| Additional source graph | `{primary_graph_iri}/source-graph/{sha256 of processed source graph name}` |
| Current container | `{base}/container/{courses or learning_materials}` |
| Classification value | `{base}/classification/{courses, learning_materials, or unclassified}` |

The resource IRI remains stable across metadata updates, classification decisions, and content reversions. Each version IRI and its primary graph IRI identify a single snapshot. Version 1 is the first state recorded by the repository. The original source entity keeps its own `id`, which remains in the source JSON-LD and appears as the source entity's IRI in RDF. `dcterms:source` links a version to that IRI. The link records provenance and makes no identity-equivalence assertion.

The primary graph contains the triples from the source JSON-LD default graph. A source document can declare additional graphs with IRI or blank-node names. Each receives its own graph within the version's scope, and the default graph maps that scoped graph back to the original graph name. A blank graph name is represented as a blank node shared with any references to it in the version's other source graphs. Blank-node labels are local to the dataset. The derived graph name for a source blank graph is based on PyLD's processed label and carries no cross-export identity guarantee. The primary graph IRI is always the stored SQLite IRI.

The `repo:` namespace defaults to `{base}/vocab/`. Set `OERSI_REPOSITORY_VOCABULARY_IRI` or pass `--vocabulary-iri` to configure it separately. Opening SQLite uses the saved base and vocabulary namespaces. An exported dataset retains their full IRIs.

## Context provenance

RDF export uses an explicitly approved JSON-LD context baseline. Its local manifest records the original context URLs, final document URLs after redirects, acquisition time, and SHA-256 hashes of the saved response bodies. PyLD uses those document URLs as the base for remote dependencies. It resolves nested contexts and imports offline from the same verified snapshot during validation and RDF conversion. Resource data keeps its original JSON-LD base rules.

The export command reports the approved manifest digest. Keep that digest and the context snapshot with the RDF files as separate export provenance. The snapshot identifies the context bytes used for an export. It does not represent a new metadata version in SQLite or change the existing RDF vocabulary. Approving another baseline is an explicit selection of a separately reviewed directory and fingerprint. [Capture and approval workflow](../README.md#capture-and-approve-a-context-baseline)

Phase 1–3 did not preserve the context bytes used at ingestion, so the approved export baseline cannot establish an earlier interpretation. Exports made with different approved baselines can differ while their resource, version, source, and primary graph IRIs stay the same. Preserve the baseline and processing software for reproducible exports. [JSON-LD context processing](https://www.w3.org/TR/json-ld11-api/#context-processing-algorithm)

## Standard RDF relationships

| Predicate | Subject | Object |
| --- | --- | --- |
| `rdf:type` | Resource, version, container, or classification | Its repository class |
| `dcterms:hasVersion` | Resource | One of its versions |
| `dcterms:isVersionOf` | Version | Its resource |
| `dcterms:isPartOf` | Resource | Current container, when classified |
| `dcterms:source` | Version | The original source entity IRI |
| `prov:wasRevisionOf` | Version 2 or later | Its immediate predecessor |
| `prov:generatedAtTime` | Version | UTC repository creation time as `xsd:dateTime` |
| `dcterms:identifier` | Resource, container, or classification | Stable ID or category code as a string |
| `dcterms:title` | Resource | Current normalized title, when present |
| `rdfs:label` | Container or classification | Display name |
| `dcterms:description` | Container | Description |
| `dcterms:created` | Container | UTC creation time as `xsd:dateTime` |

Version 1 has no predecessor. The source graphs use the vocabularies selected by the original JSON-LD. The repository default graph uses [RDF and RDFS](https://www.w3.org/TR/rdf-schema/), [Dublin Core Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/), [PROV-O](https://www.w3.org/TR/prov-o/), and [XML Schema Datatypes](https://www.w3.org/TR/xmlschema11-2/).

## Custom classes

| Class | Meaning |
| --- | --- |
| `repo:Resource` | Stable repository identity spanning all accepted snapshots of a source record |
| `repo:Version` | Immutable snapshot with provenance and one primary named source graph |
| `repo:Container` | Courses or Learning Materials, assigned to a resource in its current state |
| `repo:Classification` | Controlled category value used on a version, including Unclassified |

## Custom predicates

| Predicate | Intended domain | Intended range | Semantics |
| --- | --- | --- | --- |
| `repo:currentVersion` | `repo:Resource` | `repo:Version` | The resource's single currently selected version |
| `repo:graphIRI` | `repo:Version` | IRI | Primary graph name for this version's source triples |
| `repo:versionNumber` | `repo:Version` | `xsd:integer` | Sequential number within the resource, starting at 1 |
| `repo:isCurrent` | `repo:Version` | `xsd:boolean` | Whether this version was current in the exported SQLite snapshot |
| `repo:classification` | `repo:Version` | `repo:Classification` | Category stored in this version's normalized metadata |
| `repo:classificationReason` | `repo:Version` | `xsd:string` | Recorded explanation for this version's classification |
| `repo:observedAt` | `repo:Version` | `xsd:dateTime` | UTC time at which the metadata was observed |
| `repo:sourceModifiedAt` | `repo:Version` | `xsd:dateTime` | UTC source-reported modification instant, when available |
| `repo:metadataHash` | `repo:Version` | `xsd:string` | SHA-256 of canonical source JSON used by the repository |
| `repo:sourceGraph` | `repo:Version` | IRI | Named graph produced from an explicitly named source graph |
| `repo:sourceGraphName` | Scoped source graph IRI | IRI or blank node | Original graph name in the processed source dataset |

## Historical interpretation

`dcterms:isPartOf` describes membership at export time. Version classification remains available for earlier snapshots after an accepted review changes the current container. Unclassified snapshots have a classification value and their current resources have no container membership.

The observation-time query selects the highest version observed by the requested instant. It can reflect review decisions made after that observation. The repository-time query selects the highest version created by the instant using `prov:generatedAtTime`. That timestamp is recorded inside the write transaction and does not capture exact commit visibility. Migrated Phase 2 snapshots use creation time as their observation boundary. Source date-only values and values without a time zone stay in the source JSON-LD and do not produce a repository modification instant.

The metadata hash compares JSON content. RDF dataset equivalence depends on triples, their graph scopes, and blank-node relationships. The sample RDF version comparison reports ground triples added or removed from primary graphs. SQLite remains authoritative, and this phase evaluates SPARQL locally without a deployed public service.
