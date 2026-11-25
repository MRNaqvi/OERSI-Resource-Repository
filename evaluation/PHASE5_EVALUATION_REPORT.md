# Phase 5 semantic validation evaluation

## Research objective

Evaluate the published RDF model against four independent questions. Does the repository default graph satisfy its declared constraints? Do resource, version, and graph relationships agree internally and with the SQLite snapshots? Does each exported version preserve the RDF interpretation of the original source under the approved context baseline? Do the local SPARQL queries return the manually specified answers for a controlled history?

Automated tests measure software behavior on specific positive and negative inputs. SHACL measures structural conformance to declared shapes. Integrity checks measure repository consistency. Fidelity measures agreement with the RDF interpretation of the source. Establishing the factual accuracy, completeness, or relevance of the source statements would require other evidence.

## Model and methods

SQLite supplies immutable JSON-LD, resource and version identities, classifications, and provenance. The RDF default graph carries Dublin Core relationships, PROV-O timestamps and revision links, and the repository vocabulary. Each immutable version has its own primary graph. If the source contains named graphs, the version declares additional graphs with mappings back to their source names. See the [repository vocabulary](../docs/repository-vocabulary.md).

[SHACL Core shapes](../shapes/repository-shapes.ttl) check the counts and types of resource and version attributes, the repository identifier and metadata hash forms, and the exact classification and container codes. pySHACL applies them to the default graph without inference. [W3C SHACL Recommendation](https://www.w3.org/TR/shacl/)

The independent [repository checks](../src/semantic_validation.py) test reciprocal ownership, current-version membership and flags, numerically contiguous histories, immediate predecessors, cross-resource links, cycles, graph ownership and scoping, container consistency, source identity and unsupported identity-equivalence assertions. With a pinned source reference they distinguish an omitted nonempty graph from a legitimately empty source graph. With SQLite they check the exported versions, provenance and historical classifications against the selected snapshot.

[Source fidelity](../src/rdf_fidelity.py) reconstructs the expected RDF from every stored JSON-LD object. It uses the approved context baseline, PyLD's writer and RDFLib's N-Quads parser. The export uses a separate direct term mapper. Version graph scopes are compared by original graph name, with blank-node isomorphism across scopes and separation between versions. Each output includes missing and unexpected ground quads where available. TriG and N-Quads equivalence covers the full dataset, including the default graph.

[The competency-question catalogue](competency-questions.md) defines each query, inputs, expected projection, and limitations. [Static gold sets](expected-answers.json) were specified from the original fixtures, known resource identifiers and simulated event timeline. The evaluator reuses existing queries and compares projected binding sets. Missing and unexpected rows are both recorded. An exact set match counts as one correct answer. The denominator is the complete catalogue, including questions that fail to execute.

## Dataset provenance and gold answers

Two [fixture records](fixtures/) are byte-identical copies of previously extracted genuine public OERSI metadata. Their resource identities and author and license metadata are retained as supplied. The listed licenses are declarations on the source resources.

| Record | Original source IRI | Resource license as supplied |
| --- | --- | --- |
| Biology 1510: Biological Principles (Open Course) | `http://bio1510.biology.gatech.edu/` | CC BY-NC-SA 4.0 |
| Geographic Information System Basics | `http://2012books.lardbucket.org/books/geographic-information-system-basics/index.html` | CC BY-NC-SA 3.0 |

The other two fixtures use reserved `example.test` IRIs and are labeled simulated. All observation and repository dates, the description replacement, the identical follow-up observation, the reversion, and the reviewed textbook reclassification are controlled simulations. The benchmark writes these events to a new temporary database using the existing import and approval APIs.

The [context manifest](fixtures/amb-snapshot/manifest.json) records the official AMB context, its redirected document URL, acquisition on 2026-11-25, and the document SHA-256. The [benchmark specification](fixtures/benchmark.json) fixes the approved manifest fingerprint at `f9101d0d7707a4a89ff67d0498dfe4156ca9e1c9b2606fcfbd019b3ef5360993`. This capture establishes the benchmark's export baseline. The original ingestion context bytes were unrecorded. [Official AMB project](https://github.com/dini-ag-kim/amb)

| Gold question | Expected result and observed result |
| --- | --- |
| CQ1 Current courses | One row, the simulated course at Version 1 |
| CQ2 Current learning materials | Two rows, the genuine course at Version 4 and the genuine learning material at Version 1 |
| CQ3 Current course version | Version 4 and its primary graph |
| CQ4 Historical metadata | Version 1's original English description |
| CQ5 Versions of the genuine course | Versions 1–4 with only Version 4 marked current |
| CQ6 Consecutive changes | One removed and one added description triple between Versions 1 and 2 |
| CQ7 Classification history | Version 3 → 4, Courses → Learning Materials |
| CQ8 Observation cutoff | At 2026-01-15T12:00:00Z, Version 4, observed January 5 |
| CQ9 Repository cutoff | At the same cutoff, Version 3, created January 13 |
| CQ10 Source and provenance | Original course IRI, Version 1 created January 11 and observed January 1, source modification unbound |

The classification proposal was entered January 14 and approved January 17. The original description returned in Version 3 after the simulation restored the original content. The identical January 3 observation left the version count unchanged.

## Observed results

| Measure | Result |
| --- | --- |
| Corpus | 4 resources, 7 versions, 7 named graphs |
| Dataset size | 125 default-graph triples, 522 total quads |
| SHACL | Conforms, zero validation results |
| Dataset integrity | Zero violations across all check types |
| Source fidelity | 7 of 7 versions equivalent under the declared baseline |
| Competency questions | 10 specified, 10 executed, 10 matched |
| Exact answer-set correctness | 10/10 (100%) on the documented benchmark projections |
| TriG and N-Quads reloads | Both equivalent, with conformance and all answer sets preserved |
| Read-only checks | Temporary SQLite bytes and the complete historical snapshot set matched before and after evaluation |

See the [JSON evidence](results/report.json), the [conforming SHACL report](results/shacl-report.ttl), and the [nonconforming SHACL report](results/negative-shacl-report.ttl). The JSON records the per-version fidelity and scope counts, exact expected and actual projections with set differences, an accepted review record, and diagnostic evidence from the negative cases.

The benchmark detected all four deliberately induced failures.

| Simulated corruption or error | Expected detector | Observed diagnostic |
| --- | --- | --- |
| Remove the genuine course's current pointer | SHACL `shape:CurrentVersion` | `sh:MinCountConstraintComponent`, with the affected resource IRI |
| Remove Version 4's predecessor | Repository integrity | `predecessor`, with the affected version IRI |
| Remove the original description quad | Source fidelity | Version 1 failed equivalence and its missing ground quad was reported |
| Execute CQ3 after removing the current pointer | Gold answer comparison | Query executed, returned an empty answer set, and reported the missing Version 4 row |

The [automated mutation tests](../tests/test_semantic_validation.py) also exercise multiple current pointers, version-owner and predecessor violations, version cycles and duplicate numbers, malformed timestamps and classification codes, missing provenance, shared or misassigned graphs, inconsistent container membership, incorrect source identity, unsupported equivalence, wrong predicates and extra triples, literal datatypes and language mistakes, blank-node changes, graph-scope mistakes, and cross-version contamination. The tests create invalid RDF in memory and preserve their SQLite sources.

## Reproduction

From the repository root on Python 3.12:

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m src.semantic_validation_cli evaluate --output-dir /path/to/new-evaluation
```

The reproduction directory must be new. Fixture files and approved context bytes supply everything needed for evaluation after dependency installation. The offline tests block socket connections and DNS resolution while the CLI executes. The benchmark uses fixed clock values, and independent runs can be compared using the JSON report. Compare RDF reports by graph equivalence when using different parser versions.

The full local suite passed **200 tests**, including all 147 Phase 1–4 tests, on Python 3.12.3 with pytest 8.4.2, pySHACL 0.31.0, RDFLib 7.6.0, and PyLD 3.3.0. The run used `python -m pytest -q -p no:cacheprovider` with a temporary pytest directory and bytecode disabled to preserve existing local caches. The 8,112 warnings were RDFLib deprecations from its internal use of `Dataset.default_context`, `Dataset.contexts`, and `ConjunctiveGraph` in parsers, serialization and SPARQL. The N-Quads parser reports one of these warnings for each default-graph statement it reads, so converting and reloading multiple versions repeats the warning. The test run kept warnings enabled.

## Threats to validity and limits

- The corpus contains two genuine records and two synthetic records. Its scenario coverage supports the stated benchmark only. Generalization to varied OERSI providers and context patterns would require a larger independently reviewed sample.
- The reference and export both use PyLD and rely on the same approved context semantics. A defect shared by the processor, or a problem in the pinned context itself, can pass fidelity checks. An independent JSON-LD processor would provide another comparison.
- The query score reflects selected projections and exact set equality. It says nothing about ranking, duplicate rows, untested historical fields, or the correctness of source statements. CQ6 covers ground triples in primary graphs. Fidelity checks cover auxiliary graphs and blank-node relationships.
- A serialized dataset on its own cannot establish that an entire resource or an empty declared graph is missing. SQLite and the pinned source reference allow verification of expected resources and statements. N-Quads omits empty graph registrations.
- Observation-time and repository-time queries evaluate accepted versions. They cannot recover discarded source states, undocumented earlier JSON-LD contexts, or unknown ingestion timestamps. Migrated Phase 2 snapshots may use the initial import time as an observation fallback.
- Materializing datasets and comparing blank-node structures uses memory proportional to the data being validated, and highly symmetric blank-node graphs can be expensive to compare. The evaluation did not measure whole-index performance.
