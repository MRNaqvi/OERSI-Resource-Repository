# Phase 5 negative-test coverage

The matrix covers each Phase 5 pytest case that introduces invalid input or asserts a failure path, plus the four mutations recorded in the demonstration. The primary detector is the result asserted by the named test. A mutation can trigger additional checks. The inherited Phase 1–4 tests run in the full suite.

## SHACL default-graph mutations

Each ID below belongs to `test_shacl_negative_cases` in [test_semantic_validation.py](../tests/test_semantic_validation.py#L61). The tests assert the focus IRI, the property shape, the constraint component and a diagnostic message. Shape names use `urn:oersi:shapes:`; constraint component names use the W3C SHACL namespace.

| Parametrized test ID | Mutation | Shape | Constraint component |
| --- | --- | --- | --- |
| `CurrentVersion-0` | Remove the resource current pointer | `CurrentVersion` | `MinCountConstraintComponent` |
| `CurrentVersion-1` | Add a second current pointer | `CurrentVersion` | `MaxCountConstraintComponent` |
| `HasVersion-2` | Remove all version membership links | `HasVersion` | `MinCountConstraintComponent` |
| `RepositoryIdentifier-3` | Replace the stable identifier with invalid text | `RepositoryIdentifier` | `PatternConstraintComponent` |
| `ContainerMembership-4` | Add a second container membership | `ContainerMembership` | `MaxCountConstraintComponent` |
| `VersionOwner-5` | Remove the version's owner link | `VersionOwner` | `MinCountConstraintComponent` |
| `VersionNumber-6` | Use an untyped string for the version number | `VersionNumber` | `DatatypeConstraintComponent` |
| `VersionNumber-7` | Set the version number to zero | `VersionNumber` | `MinInclusiveConstraintComponent` |
| `PrimaryGraph-8` | Use a literal for the primary graph IRI | `PrimaryGraph` | `NodeKindConstraintComponent` |
| `OriginalSource-9` | Use a literal for the source IRI | `OriginalSource` | `NodeKindConstraintComponent` |
| `CreatedAt-10` | Remove required generation time | `CreatedAt` | `MinCountConstraintComponent` |
| `ObservedAt-11` | Use a string for observation time | `ObservedAt` | `DatatypeConstraintComponent` |
| `SourceModifiedAt-12` | Use a string for optional source modification time | `SourceModifiedAt` | `DatatypeConstraintComponent` |
| `VersionClassification-13` | Assign an untyped classification IRI | `VersionClassification` | `ClassConstraintComponent` |
| `MetadataHash-14` | Replace the metadata hash with invalid text | `MetadataHash` | `PatternConstraintComponent` |
| `PreviousVersion-15` | Add a second predecessor | `PreviousVersion` | `MaxCountConstraintComponent` |
| `IsCurrent-16` | Use a string for the current flag | `IsCurrent` | `DatatypeConstraintComponent` |
| `ClassificationCode-17` | Set the classification code to `video` | `ClassificationCode` | `InConstraintComponent` |
| `ContainerCode-18` | Set a container code to `unclassified` | `ContainerCode` | `InConstraintComponent` |

## Repository integrity mutations

Unless named otherwise, the case ID belongs to `test_integrity_negative_cases` in [test_semantic_validation.py](../tests/test_semantic_validation.py#L106). The tests assert the reported code and a diagnostic.

| Test ID | Mutation | Integrity check |
| --- | --- | --- |
| `broken_chain` | Remove Version 2's predecessor | `predecessor` |
| `chain_cycle` | Link Version 1 back to Version 2 | `predecessor_cycle` |
| `cross_owner` | Link a predecessor owned by another resource | `cross_resource_predecessor` |
| `wrong_current` | Point at another resource's version | `current_membership` |
| `duplicate_number` | Give two versions the same number | `version_sequence` |
| `wrong_graph_owner` | Assign another resource's primary graph | `primary_graph_iri` |
| `reused_graph` | Share a primary graph between versions | `graph_reused` |
| `undeclared_graph` | Add data under an undeclared graph IRI | `undeclared_graph` |
| `container_mismatch` | Add the wrong current container | `current_container` |
| `wrong_source` | Substitute an unrelated source IRI | `source_identity` |
| `same_as` | Assert `owl:sameAs` in the repository default graph | `unsupported_same_as` |
| `historical_classification` | Substitute a valid classification code inconsistent with the stored version | `sqlite_classification` |
| `nondefault_data` | Place source-like data in the repository default graph | `unexpected_default_subject` |
| `test_nonempty_graph_absence_requires_fidelity_reference` | Drop a primary graph known from pinned JSON-LD to contain statements | `missing_nonempty_graph`; a standalone check records the absent registration separately |

`test_empty_source_scope_is_not_reported_as_lost_triples` covers the companion false-positive case. It checks that legitimately empty source graph registrations can disappear in N-Quads without a fidelity or integrity violation.

## RDF fidelity mutations

Each ID below belongs to `test_fidelity_negative_cases` in [test_semantic_validation.py](../tests/test_semantic_validation.py#L209). Every case asserts failed per-version equivalence. The ground cases also require a missing or unexpected quad diagnostic.

| Parametrized test ID | Mutation | Fidelity check |
| --- | --- | --- |
| `missing` | Remove a source quad | Missing ground quad |
| `extra` | Add an unsupported quad | Unexpected ground quad |
| `predicate` | Replace a source predicate | Ground predicate equality |
| `iri` | Replace a source object IRI | Ground IRI equality |
| `datatype` | Convert a typed integer literal to a string | Literal datatype equality |
| `language` | Replace `fr` with `de` | Literal language equality |
| `lexical` | Replace integer lexical form `07` with `7` | Typed literal lexical equality |
| `blank` | Replace a shared blank-node subject to break a cross-graph reference | Blank-node-aware dataset isomorphism |
| `scope` | Move a ground quad from the primary to an auxiliary graph | Quad graph-scope equality |
| `mixed_version` | Reuse a blank node from another version | Inter-version blank-node isolation; diagnostic explicitly asserted |

## CLI, evaluation, and demonstration failure paths

| Test or demonstration | Failure exercised | Asserted detector or result |
| --- | --- | --- |
| `test_invalid_dataset_has_structured_and_human_violations` | Remove current pointer and validate TriG | SHACL `CurrentVersion / MinCountConstraintComponent`, report nonconformance, readable diagnostic and CLI exit 1 |
| `test_cli_requires_explicit_matching_context_approval` | Absent approval; separate run with a mismatched SHA-256 | Context gate rejects both before SQLite-backed validation, with diagnostic and exit 2 |
| `test_validate_cli_from_local_file_and_sqlite[trig-.trig]` | Rerun with report paths already present | Output protection, exit 2 |
| `test_validate_cli_from_local_file_and_sqlite[nquads-.nq]` | Rerun with report paths already present | Output protection, exit 2 |
| `test_benchmark_passes_offline_and_is_deterministic` | Reuse an evaluation output directory | Output protection, exit 2 |
| `test_incorrect_and_failed_competency_answers` | Execute CQ1 against a different fixture; separate missing query file | Exact gold comparison reports missing and unexpected rows; unavailable query recorded as unexecuted with `FileNotFoundError` |
| Demonstration, missing current pointer | Remove the genuine course's current pointer | SHACL `CurrentVersion / MinCountConstraintComponent` |
| Demonstration, broken predecessor | Remove Version 4's predecessor | Integrity `predecessor` |
| Demonstration, missing description quad | Remove the original Version 1 description | Fidelity failure with the removed ground quad |
| Demonstration, incorrect CQ3 answer | Query after removing the course's current pointer | Executed with an empty result; the Version 4 gold row is missing |

## Coverage gaps

All 14 failure categories explicitly listed in the Phase 5 specification have a direct mutation and assertion above. The suite exercises the main failure families. The following variants lack focused negative assertions.

| Area | Variants without a focused assertion |
| --- | --- |
| Current version and chain | An otherwise valid same-resource pointer to an older version, inconsistent or multiple validly typed current flags, a gap rather than a duplicate version number, and a same-resource predecessor that skips a version without forming a cycle. |
| Repository vocabulary and history | A missing canonical container or classification node; an extra typed node; a current unclassified resource assigned a container; a well-typed timestamp or validly formatted hash that disagrees with SQLite; complete removal of a required resource or version. The benchmark covers valid unclassified membership and SQLite agreement. |
| Named-graph mappings | Declaring the dataset default graph as a named version graph, supplying a wrong per-version auxiliary graph IRI, or omitting, duplicating, or altering an auxiliary graph's original name. Existing tests cover the placement of quads within validly declared scopes. |
| SHACL constraint variants | Individual unasserted cardinality and node-kind branches, including multiple optional source-modification timestamps and missing individual properties such as `repo:metadataHash` or `dcterms:source`. The suite asserts representative required, optional, and maximum-count failures and incorrect types and values. |
| RDF and context error paths | Pinned reconstruction encountering an unresolvable context during file-based fidelity comparison, or a tampered auxiliary graph-name mapping reported by fidelity. Approval mismatch is tested in Phase 5; Phase 4 tests exercise missing and modified pins during export. |

The matrix records coverage of the existing suite. The benchmark and tests exercise repository and RDF behavior on finite fixtures. Source factual accuracy and the context bytes used by past ingestion are separate validation questions.
