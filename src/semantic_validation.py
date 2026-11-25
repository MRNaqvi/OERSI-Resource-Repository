"""SHACL and independent structural validation for the repository dataset."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path

from pyshacl import validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, SH, XSD

from src.metadata import stable_resource_id
from src.repository import CONTAINERS, observation_time

ROOT = Path(__file__).resolve().parents[1]
SHAPE_VOCAB = "https://example.org/oersi/vocab/"
CATEGORIES = (*CONTAINERS, "unclassified")


def validate_shapes(default: Graph, vocabulary_iri: str):
    """Validate the default graph alone using the checked-in SHACL Core shapes."""
    shapes = Graph().parse(data=(ROOT / "shapes/repository-shapes.ttl").read_bytes(), format="turtle")
    if vocabulary_iri != SHAPE_VOCAB:
        adjusted = Graph()
        for triple in shapes:
            adjusted.add(tuple(
                URIRef(vocabulary_iri + term[len(SHAPE_VOCAB):])
                if isinstance(term, URIRef) and term.startswith(SHAPE_VOCAB) else term
                for term in triple
            ))
        shapes = adjusted

    # Canonicalize any blank nodes before pySHACL formats focus nodes in messages.
    data = to_canonical_graph(default) if any(isinstance(term, BNode) for triple in default for term in triple) else default
    conforms, report, _ = validate(
        data, shacl_graph=shapes, inference="none", advanced=False,
        inplace=False, do_owl_imports=False, js=False, sparql_mode=False,
    )
    report = to_canonical_graph(report)
    results = []
    fields = {
        "focus": SH.focusNode, "path": SH.resultPath, "shape": SH.sourceShape,
        "constraint": SH.sourceConstraintComponent, "value": SH.value,
        "severity": SH.resultSeverity,
    }
    for result in report.subjects(RDF.type, SH.ValidationResult):
        entry = {
            name: value.n3()
            for name, predicate in fields.items()
            if (value := report.value(result, predicate)) is not None
        }
        entry["messages"] = sorted(str(item) for item in report.objects(result, SH.resultMessage))
        results.append(entry)
    results.sort(key=lambda row: tuple(str(row.get(name, "")) for name in (*fields, "messages")))
    return {"conforms": bool(conforms), "violations": results}, report


def _one(graph, subject, predicate):
    values = set(graph.objects(subject, predicate))
    return next(iter(values)) if len(values) == 1 else None


def _version_number(term):
    if isinstance(term, Literal) and term.datatype == XSD.integer and type(term.value) is int:
        return term.value
    return None


def _text(term):
    return "_:blank-node" if isinstance(term, BNode) else str(term)


def check_integrity(dataset, *, base_iri: str, vocabulary_iri: str, snapshot=None, required_graphs=()):
    default = dataset.default_graph
    repo = Namespace(vocabulary_iri)
    issues = []
    declarations = defaultdict(list)

    def issue(code, focus, message):
        issues.append({"code": code, "focus": _text(focus), "message": message})

    resources = set(default.subjects(RDF.type, repo.Resource))
    resources.update(default.subjects(repo.currentVersion, None))
    resources.update(default.subjects(DCTERMS.hasVersion, None))
    versions = set(default.subjects(RDF.type, repo.Version))
    versions.update(default.subjects(DCTERMS.isVersionOf, None))
    versions.update(default.subjects(repo.graphIRI, None))
    versions.update(default.objects(None, DCTERMS.hasVersion))
    versions.update(default.objects(None, repo.currentVersion))

    containers = {code: URIRef(f"{base_iri}/container/{code}") for code in CONTAINERS}
    categories = {code: URIRef(f"{base_iri}/classification/{code}") for code in CATEGORIES}
    for expected, type_, code in (
        (containers, repo.Container, "container_set"),
        (categories, repo.Classification, "classification_set"),
    ):
        actual = set(default.subjects(RDF.type, type_))
        for name, term in expected.items():
            if term not in actual or set(default.objects(term, DCTERMS.identifier)) != {Literal(name)}:
                issue(code, term, f"Expected the {name!r} node with its canonical identifier")
        for term in actual - set(expected.values()):
            issue(code, term, "Unexpected node outside the repository's controlled vocabulary")

    predecessors = {}
    for version in versions:
        owner = _one(default, version, DCTERMS.isVersionOf)
        linked_by = set(default.subjects(DCTERMS.hasVersion, version))
        if owner not in resources or linked_by != {owner}:
            issue("version_ownership", version, "Version ownership disagrees with reciprocal hasVersion links")
        if _one(default, version, repo.classification) not in categories.values():
            issue("version_classification", version, "Expected one canonical historical classification")
        predecessor = _one(default, version, PROV.wasRevisionOf)
        if predecessor is not None:
            predecessors[version] = predecessor
        primary = _one(default, version, repo.graphIRI)
        for graph in default.objects(version, repo.graphIRI):
            declarations[graph].append(version)
        for graph in default.objects(version, repo.sourceGraph):
            declarations[graph].append(version)
            name = _one(default, graph, repo.sourceGraphName)
            prefix = f"{primary}/source-graph/"
            if not isinstance(graph, URIRef) or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", str(graph)):
                issue("auxiliary_graph_scope", version, f"Source graph {_text(graph)} is outside its version scope")
            if not isinstance(name, (URIRef, BNode)):
                issue("source_graph_name", version, f"Source graph {_text(graph)} needs one original graph name")
            elif isinstance(name, URIRef) and str(graph) != prefix + hashlib.sha256(str(name).encode()).hexdigest():
                issue("source_graph_name", version, f"Source graph {_text(graph)} disagrees with its original name")

    repository_nodes = resources | versions | set(declarations) | set(containers.values()) | set(categories.values())
    for subject in set(default.subjects()) - repository_nodes:
        issue("unexpected_default_subject", subject, "Only repository and graph-mapping subjects belong in the default graph")
    for subject, target in default.subject_objects(OWL.sameAs):
        issue("unsupported_same_as", subject, f"Repository default graph asserts identity with {_text(target)}")
    for resource in resources:
        identifier = _one(default, resource, DCTERMS.identifier)
        iri = f"{base_iri}/resource/{identifier}"
        if str(resource) != iri:
            issue("resource_iri", resource, "Resource IRI disagrees with the repository base and stable ID")
        members = set(default.objects(resource, DCTERMS.hasVersion))
        current = _one(default, resource, repo.currentVersion)
        if current not in members:
            issue("current_membership", resource, "Exactly one current version must belong to this resource")
        flags = {v for v in members if (v, repo.isCurrent, Literal(True)) in default}
        if flags != {current}:
            issue("current_flags", resource, "Exactly the current pointer's version must be marked current")
        numbers = defaultdict(list)
        for version in members:
            number = _version_number(_one(default, version, repo.versionNumber))
            if number is not None:
                numbers[number].append(version)
                if str(version) != f"{iri}/version/{number}":
                    issue("version_iri", version, "Version IRI disagrees with its resource and number")
                graph = _one(default, version, repo.graphIRI)
                if str(graph) != f"{base_iri}/graph/resource/{identifier}/version/{number}":
                    issue("primary_graph_iri", version, "Primary graph IRI disagrees with its resource and number")
            source = _one(default, version, DCTERMS.source)
            if isinstance(source, URIRef):
                if source in repository_nodes or stable_resource_id(str(source)) != str(identifier):
                    issue("source_identity", version, f"Source {source} must be distinct and match the stable ID")
                if any(default.triples((source, None, None))):
                    issue("source_in_default_graph", source, "Source RDF must remain in its named graph")
        if len(numbers) != len(members) or set(numbers) != set(range(1, len(members) + 1)):
            issue("version_sequence", resource, "Version numbers must be unique and contiguous from 1")
        if numbers and current is not None and current not in numbers[max(numbers)]:
            issue("current_not_latest", resource, "The current pointer must target the highest version number")
        for number, at_number in numbers.items():
            expected = set(numbers.get(number - 1, [])) if number > 1 else set()
            for version in at_number:
                links = set(default.objects(version, PROV.wasRevisionOf))
                if links != expected or (number > 1 and len(expected) != 1):
                    issue("predecessor", version, "Version 1 has no predecessor. Later versions link to N-1")
                if any(_one(default, previous, DCTERMS.isVersionOf) != resource for previous in links):
                    issue("cross_resource_predecessor", version, "A predecessor must belong to the same resource")
        if current is not None:
            classification = _one(default, current, repo.classification)
            code = next((key for key, value in categories.items() if value == classification), None)
            actual = set(default.objects(resource, DCTERMS.isPartOf))
            expected = {containers[code]} if code in containers else set()
            if code is None or actual != expected:
                issue("current_container", resource, "Current container membership disagrees with current classification")

    visited = set()
    for version in sorted(predecessors, key=str):
        path = set()
        while version in predecessors and version not in visited and version not in path:
            path.add(version)
            version = predecessors[version]
        if version in path:
            issue("predecessor_cycle", version, "A predecessor cycle was detected")
        visited.update(path)

    for graph, owners in declarations.items():
        if len(owners) != 1:
            issue("graph_reused", graph, "A named graph must be assigned to exactly one version and role")
    known_graphs = {graph.identifier for graph in dataset.graphs()} - {default.identifier}
    for graph in known_graphs - declarations.keys():
        issue("undeclared_graph", graph, "Named graph is not declared by a repository version")
    for graph in declarations.keys() & {default.identifier}:
        issue("default_as_named_graph", graph, "The repository default graph cannot be the source graph")
    for graph in set(required_graphs) - known_graphs:
        issue("missing_nonempty_graph", graph, "Pinned source RDF contains statements for this absent graph")

    if snapshot is not None:
        _compare_snapshot(default, repo, snapshot, base_iri, resources, versions, issue)

    # An absent registration alone cannot distinguish an omitted empty graph from data loss.
    missing = sorted(_text(graph) for graph in declarations.keys() - known_graphs)
    issues = sorted(
        {tuple(item.values()) for item in issues},
    )
    details = [dict(zip(("code", "focus", "message"), row, strict=True)) for row in issues]
    return {
        "conforms": not details,
        "violation_count": len(details),
        "by_code": dict(sorted(Counter(item["code"] for item in details).items())),
        "violations": details,
        "unregistered_declared_graphs": missing,
    }


def _compare_snapshot(default, repo, snapshot, base_iri, resources, versions, issue):
    def compare(subject, predicate, values, code):
        if set(default.objects(subject, predicate)) != values:
            issue(code, subject, f"Values of {predicate} disagree with the SQLite snapshot")

    for resource in snapshot["resources"]:
        iri = URIRef(resource["resource_iri"])
        compare(iri, repo.currentVersion, {URIRef(resource["current_version_iri"])}, "sqlite_current_version")
        compare(iri, DCTERMS.hasVersion, {URIRef(v["version_iri"]) for v in resource["versions"]}, "sqlite_version_set")
        for version in resource["versions"]:
            version_iri = URIRef(version["version_iri"])
            classification = version["normalized_metadata"]["classification"]
            compare(version_iri, DCTERMS.source, {URIRef(resource["source_iri"])}, "sqlite_source")
            compare(version_iri, repo.graphIRI, {URIRef(version["graph_iri"])}, "sqlite_primary_graph")
            compare(version_iri, repo.metadataHash, {Literal(version["metadata_hash"])}, "sqlite_hash")
            compare(
                version_iri, repo.classification, {URIRef(f"{base_iri}/classification/{classification['container']}")},
                "sqlite_classification",
            )
            compare(version_iri, repo.classificationReason, {Literal(classification["reason"])}, "sqlite_classification")
            for predicate, key in (
                (repo.observedAt, "observed_at"), (PROV.generatedAtTime, "created_at"),
                (repo.sourceModifiedAt, "source_modified_at"),
            ):
                expected = version[key]
                actual = set(default.objects(version_iri, predicate))
                try:
                    equivalent = len(actual) == 1 and observation_time(str(next(iter(actual)))) == observation_time(expected)
                except ValueError:
                    equivalent = False
                if (expected is None and actual) or (expected is not None and not equivalent):
                    issue("sqlite_timestamp", version_iri, f"Values of {predicate} disagree with the SQLite snapshot")
    expected_resources = {URIRef(row["resource_iri"]) for row in snapshot["resources"]}
    expected_versions = {URIRef(v["version_iri"]) for row in snapshot["resources"] for v in row["versions"]}
    for actual, expected, kind in (
        (resources, expected_resources, "resource"), (versions, expected_versions, "version"),
    ):
        for term in expected - actual:
            issue(f"sqlite_{kind}_set", term, f"Expected {kind} is absent")
        for term in actual - expected:
            issue(f"sqlite_{kind}_set", term, f"Unexpected {kind} outside the SQLite selection")


def validate_semantics(dataset, *, base_iri: str, vocabulary_iri: str, snapshot=None, contexts=None):
    """Return independently inspectable layer results and the standard SHACL RDF report."""
    from src.rdf_export import dataset_summary
    from src.rdf_fidelity import check_fidelity

    if (snapshot is None) != (contexts is None):
        raise ValueError("Source fidelity requires both a SQLite snapshot and approved contexts")
    shacl, shacl_graph = validate_shapes(dataset.default_graph, vocabulary_iri)
    if snapshot is None:
        fidelity = {"checked": False, "reason": "No SQLite source snapshot supplied"}
        required_graphs = ()
    else:
        fidelity, required_graphs = check_fidelity(dataset, snapshot, contexts, vocabulary_iri)
        fidelity.update(checked=True, context_snapshot_sha256=contexts.sha256)
    integrity = check_integrity(
        dataset, base_iri=base_iri, vocabulary_iri=vocabulary_iri,
        snapshot=snapshot, required_graphs=required_graphs,
    )
    return {
        "conforms": shacl["conforms"] and integrity["conforms"] and fidelity.get("conforms", True),
        "dataset": dataset_summary(dataset),
        "shacl": shacl,
        "integrity": integrity,
        "fidelity": fidelity,
    }, shacl_graph
