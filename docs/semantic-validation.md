# Semantic validation

Phase 5 checks a local RDF dataset against the Phase 4 repository model. SQLite remains authoritative. Validation uses read-only SQLite connections and runs locally without HTTP access, ontology inference, or RDF repair.

## Run validation

From the repository root, install `requirements.txt` in a Python 3.12 environment. The Phase 5 dependency is pySHACL 0.31.x. The suite was verified with pySHACL 0.31.0, RDFLib 7.6.0 and PyLD 3.3.0. Shapes use the W3C SHACL Core Recommendation. [W3C SHACL Recommendation](https://www.w3.org/TR/shacl/), [pySHACL project](https://github.com/RDFLib/pySHACL)

```bash
python -m src.semantic_validation_cli validate --input /path/to/repository.trig
python -m src.semantic_validation_cli validate --input /path/to/repository.nq \
  --report-json /path/to/new-validation.json --shacl-report /path/to/new-shacl.ttl
```

A file-only check runs SHACL and repository integrity checks. To compare the source RDF as well, supply the SQLite database and the context manifest fingerprint that you explicitly approved for the export. Use the same resource selection, if any, as the original export.

```bash
python -m src.semantic_validation_cli \
  --database /path/to/repository.sqlite3 \
  --context-snapshot /path/to/pinned-contexts \
  --context-sha256 "$APPROVED_CONTEXT_SHA256" \
  validate --input /path/to/repository.trig --against-sqlite \
  --report-json /path/to/new-comparison.json --shacl-report /path/to/new-shacl.ttl
```

Omit both `--input` and `--against-sqlite` to build and validate against one read-only SQLite snapshot. `--resource` accepts a stable identifier or a repository resource IRI for either SQLite-backed mode. Global options precede the subcommand. Use the [Phase 4 configuration variables](../README.md#rdf-export-and-local-sparql) or `--base-iri` and `--vocabulary-iri` if the dataset uses custom namespaces. The configured namespaces must match those of the file or SQLite repository. Missing or mismatched approval, an unreadable database, and unsupported input formats produce an error. Any context needed during a SQLite-backed operation must be present in the verified local snapshot.

Output always includes a human-readable summary. The optional JSON file records each layer's results, graph and quad counts, and diagnostics. The optional Turtle report contains the standard SHACL validation report for the default graph. Both reports can be written for a completed validation that finds violations. File outputs use exclusive creation, and their parent directories must exist. Exit codes are:

| Code | Meaning |
| --- | --- |
| 0 | The executed checks passed. File-only validation reports source fidelity as skipped. |
| 1 | A validation layer failed or the benchmark found a discrepancy. |
| 2 | Usage, input, context approval, or output error. |

## Validation layers

**SHACL Core.** [The shapes graph](../shapes/repository-shapes.ttl) is applied to the independent repository default graph. It targets resources, versions, classifications and containers, including subjects or objects of their defining links. Each named property shape reports counts and type, IRI kind or lexical constraints where applicable.

| Shape | Required properties and constraints |
| --- | --- |
| Resource | One `repo:currentVersion`, one or more `dcterms:hasVersion`, one stable `dcterms:identifier`, at most one `dcterms:isPartOf`. Links point to typed repository nodes. |
| Version | One owning resource, positive integer version number, primary graph and source IRIs, `xsd:dateTime` creation and observation times, classification, a 64-hex metadata hash and a boolean current flag. Source modification time and predecessor are optional single values. |
| Classification | One code from `courses`, `learning_materials`, `unclassified`. |
| Container | One code from `courses`, `learning_materials`. |

**Repository integrity.** The independent Python checks follow reciprocal ownership and version membership, reconcile the current pointer and flags, require a contiguous sequence beginning at Version 1, and detect incorrect predecessors, cross-resource links and cycles. They compare expected persistent IRIs and check graph declarations for unique ownership and version-scoped auxiliary names. They require the exact controlled classification and container nodes, check each version's classification and current container membership, and require unclassified resources to have no container. They check source identity against the stable identifier and flag identity-equivalence assertions in the default graph. When SQLite is supplied, the checks also compare resource and version sets, current versions, historical classifications and reasons, hashes, source and primary graph IRIs, and provenance timestamps against that snapshot. A standalone file can establish internal consistency only.

**Source fidelity.** PyLD interprets every stored source JSON-LD object with the approved, immutable context bytes, an isolated resolver and the same processing options as export. The reference uses PyLD's N-Quads writer and RDFLib's reader, independently of Phase 4's direct PyLD-to-RDFLib term mapping. Compare the source default graph to the version primary graph and compare each additional source graph using its original name from `repo:sourceGraphName`. Compare ground quads and use RDF graph isomorphism to preserve shared blank-node relationships across scopes. The comparison checks predicates, source IRIs, datatypes, language tags, lexical forms and scope. RDF 1.1 simple literals compare as `xsd:string` and language-tag casing is immaterial. Blank-node labels may differ across exports, but a blank node shared by two versions is a fidelity error. The JSON report gives per-version scope and quad counts, missing and extra ground quads, and diagnostic messages for other mismatches. It does not attempt to propose a minimal blank-node edit.

TriG can carry an explicitly empty graph and N-Quads carries no record for it. The default graph still declares the graph. If no reference is supplied, the CLI lists unregistered graph declarations separately. With a pinned source reference an omitted graph is an error when its source scope contained statements. Empty registrations may differ in export/reload equivalence, while statements, graph names and relationships must still agree.

The pinned snapshot is an export baseline. The byte sequences and URLs used by earlier ingestion may be unknown. Fidelity means agreement with the stored source interpreted under the approved baseline. Factual accuracy, classification quality and completeness of the original OERSI statements require separate evidence. See the [vocabulary](repository-vocabulary.md) and [Phase 5 evaluation report](../evaluation/PHASE5_EVALUATION_REPORT.md).

## Run the controlled evaluation

```bash
python -m src.semantic_validation_cli evaluate --output-dir /path/to/new-evaluation
```

The command builds a disposable database from the bundled fixtures, runs the three validation layers, compares the ten queries to the independently recorded answer sets, reloads both RDF formats, and verifies the database bytes and historical snapshots. It also runs in-memory mutations to exercise failure diagnostics. The directory receives `report.json`, `shacl-report.ttl`, and `negative-shacl-report.ttl`. It contains no database. For the inputs, gold answers and observed results, see the [benchmark catalogue](../evaluation/competency-questions.md) and [demonstration evidence](../evaluation/results/).
