# Competency questions and expected results

The ten questions exercise the existing Phase 4 SPARQL files. The query runner supplies `repo:` from the configured vocabulary. [The gold file](expected-answers.json) records the literal expected projections, parameters and filters. The outcomes were specified from the fixture records, the known IRI scheme and the timeline below, before comparison with the queries. Only the benchmark parses the stored question definitions. The corresponding stand-alone query files can return more rows and columns.

## Controlled timeline

| Source fixture | Starting classification | End state |
| --- | --- | --- |
| Genuine Biology 1510 course, `oersi-fb246966e56e7e24cec9` | Courses | Learning Materials, Version 4 |
| Genuine Geographic Information System Basics, `oersi-fa2ea2f5cb82b1d01dc4` | Learning Materials | Learning Materials, Version 1 |
| Simulated benchmark course, `oersi-9026a0c99f04446f6160` | Courses | Courses, Version 1 |
| Simulated benchmark unclassified record, `oersi-349567d2493e21e02d56` | Unclassified | Unclassified, Version 1 and no container |

Every initial observation is simulated at noon UTC on 2026-01-01, with initial repository insertion at noon on 2026-01-11. Only the genuine course undergoes subsequent simulation. The source files are unchanged.

| Simulated course event | Observed in January 2026 | Recorded or accepted in January 2026 | Expected history |
| --- | --- | --- | --- |
| Original description and classification | 1 | 11 | Version 1 |
| Replace the description with `[SIMULATED PHASE 5] Revised description` | 2 | 12 | Version 2 |
| Observe exactly the same change again | 3 | 12 | Still Version 2 |
| Revert to the original JSON-LD | 4 | 13 | Version 3, original content |
| Propose textbook classification | 5 | Pending on 14, accepted on 17 | Version 4 after approval |

Times use the same noon UTC clock throughout. The genuine course has no top-level `dateModified`. Its nested source entry carries a date that is outside the repository's optional source-modification field.

## Questions

In the table, **course** refers to the genuine Biology 1510 resource IRI. A version IRI appends `/version/N` to the resource IRI. A primary graph IRI is `{base}/graph/resource/{stable-id}/version/N`. Temporal questions bind the same cutoff, **2026-01-15T12:00:00Z**.

| Question | Query and inputs | Predicates and expected answer structure | Assumptions and scope |
| --- | --- | --- | --- |
| **CQ1. Which resources are currently Courses?** | [list-courses.rq](../queries/list-courses.rq), no inputs | Container `dcterms:identifier`, resource `dcterms:isPartOf`, `repo:currentVersion` and title. One row for the simulated course, title and Version 1. | Uses current published membership and optional title. |
| **CQ2. Which resources are currently Learning Materials?** | [list-learning-materials.rq](../queries/list-learning-materials.rq), no inputs | Same projection as CQ1. Two rows with titles, genuine course Version 4 and genuine material Version 1. | An accepted reclassification updates current membership. Unclassified resources have no container. |
| **CQ3. What is the current metadata version of a resource?** | [current-metadata.rq](../queries/current-metadata.rq), `resource` = course IRI | `repo:currentVersion` and `repo:graphIRI` or `repo:sourceGraph`. One distinct version and graph pair for Version 4. | The sample query traverses each nonempty graph and returns triples. The benchmark projects version and graph pairs. An entirely empty version graph requires querying the pointer directly. |
| **CQ4. What metadata was stored in a historical version?** | [historical-metadata.rq](../queries/historical-metadata.rq), course IRI and integer `versionNumber=1` | `dcterms:hasVersion`, `repo:versionNumber` and the source graph links. Expect the course's original description literal tagged `en`. | Tests an independently known source subject and `schema:description` predicate. The sample query can retrieve all triples. Complete graph fidelity is evaluated separately. |
| **CQ5. Which versions belong to the same resource?** | [list-versions.rq](../queries/list-versions.rq), course IRI | `dcterms:hasVersion`, `repo:versionNumber`, `repo:graphIRI`, `repo:isCurrent`. Four rows for Versions 1–4. Only Version 4 is current. | The identical January 3 observation creates no version. The January 4 reversion creates Version 3. |
| **CQ6. What changes occurred between consecutive versions?** | [compare-version-triples.rq](../queries/compare-version-triples.rq), course IRI, `fromVersion=1`, `toVersion=2` | `dcterms:hasVersion`, `repo:versionNumber`, `repo:graphIRI`. One removed and one added `schema:description` row with course source subject, exact text and `en` tag. | The sample compares ground triples in primary graphs. It omits blank-node edits and auxiliary-graph differences. |
| **CQ7. Which resources changed classification over time?** | [classification-changes.rq](../queries/classification-changes.rq), no inputs | `dcterms:hasVersion`, `repo:versionNumber`, `prov:wasRevisionOf`, and `repo:classification/dcterms:identifier`. Course Version 3 → 4, `courses` → `learning_materials`. | Includes accepted versions. The published RDF omits unaccepted proposals and review decisions. |
| **CQ8. Which metadata version was observed by a specified time?** | [metadata-at-time.rq](../queries/metadata-at-time.rq), course IRI, `at` typed `xsd:dateTime`, select the observation row | `repo:observedAt` among linked versions. Version 4, its graph and January 5 observation time. | Evaluated over currently accepted history. A version accepted later may be visible under its earlier observed time. Unaccepted observations have no version row. |
| **CQ9. Which metadata version had been accepted by a specified repository time?** | [metadata-at-time.rq](../queries/metadata-at-time.rq), same course IRI and cutoff, select the repository row | `prov:generatedAtTime` among linked versions. Version 3, its graph and January 13 creation time. | Each mode selects the highest version number whose relevant timestamp is at or before the cutoff. The benchmark supplies a fixed cutoff instead of using query-time `NOW()`. |
| **CQ10. What source and provenance times are associated with a version?** | [source-provenance.rq](../queries/source-provenance.rq), course IRI and Version 1 IRI | `dcterms:source`, `prov:generatedAtTime`, `repo:observedAt`, optional `repo:sourceModifiedAt`. The original course URL, January 11 creation, January 1 observation and an absent source-modified binding. | Repository creation, source observation, and provider modification are distinct timestamps. Phase 2 migrations may use the original import time as the Version 1 observation fallback. |

For each question the runner compares the observed and expected sets of projected bindings, including language where the gold specifies it and null for an absent optional binding. Ordering and duplicate rows do not affect this benchmark's correctness score. A wrong answer lists missing and unexpected rows. A query error counts as an unanswered question. Correctness is matching questions divided by all catalogue questions. No precision, recall or F1 are reported.
