from __future__ import annotations

import copy
import json
import socket

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, SH, XSD

from src.competency_evaluation import EVALUATION, evaluate_questions
from src.rdf_export import build_dataset, load_dataset, save_dataset
from src.rdf_fidelity import check_fidelity, dataset_equivalent
from src.semantic_validation import check_integrity, validate_semantics, validate_shapes
from src.semantic_validation_cli import main
from tests.test_repository import make_record, make_repository
from tests.test_version_management import when, with_normalized

SCHEMA = Namespace("http://schema.org/")
EX = Namespace("https://example.test/semantic/")
SHAPES = Namespace("urn:oersi:shapes:")


def _namespaces(repository):
    return {"base_iri": repository.base_iri, "vocabulary_iri": repository.vocabulary_iri}


@pytest.fixture
def catalogue(tmp_path, approved_contexts):
    repository = make_repository(tmp_path / "repository.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="revised"), observed_at=when(2))
    other_source, other_normalized = make_record(str(EX.other))
    repository.import_resource(other_source, other_normalized, observed_at=when(1))
    snapshot = repository.export_snapshot()
    resource = next(row for row in snapshot["resources"] if row["resource_id"] == normalized["stable_id"])
    other = next(row for row in snapshot["resources"] if row["resource_id"] == other_normalized["stable_id"])
    dataset = build_dataset(repository, contexts=approved_contexts)
    return (
        repository, snapshot, dataset, Namespace(repository.vocabulary_iri),
        URIRef(resource["resource_iri"]), URIRef(other["current_version_iri"]),
        *(URIRef(version["version_iri"]) for version in resource["versions"]),
    )


def test_full_validation_separates_the_three_layers(catalogue, approved_contexts):
    repository, snapshot, rdf, *_ = catalogue
    before = repository.database_path.read_bytes()
    report, shacl = validate_semantics(rdf, **_namespaces(repository), snapshot=snapshot, contexts=approved_contexts)
    assert report["conforms"]
    assert report["shacl"] == {"conforms": True, "violations": []}
    assert (None, RDF.type, SH.ValidationReport) in shacl
    assert report["integrity"]["violation_count"] == 0
    assert report["fidelity"]["checked_versions"] == report["fidelity"]["equivalent_versions"] == 3
    assert report["fidelity"]["context_snapshot_sha256"] == approved_contexts.sha256
    assert repository.database_path.read_bytes() == before
    assert repository.export_snapshot() == snapshot


CASES = (
    # node key, predicate key, mutation, replacement, property shape, component
    ("resource", "currentVersion", "remove", None, "CurrentVersion", SH.MinCountConstraintComponent),
    ("resource", "currentVersion", "add", "v1", "CurrentVersion", SH.MaxCountConstraintComponent),
    ("resource", DCTERMS.hasVersion, "remove", None, "HasVersion", SH.MinCountConstraintComponent),
    ("resource", DCTERMS.identifier, "set", Literal("invalid"), "RepositoryIdentifier", SH.PatternConstraintComponent),
    ("resource", DCTERMS.isPartOf, "add", "material_container", "ContainerMembership", SH.MaxCountConstraintComponent),
    ("v2", DCTERMS.isVersionOf, "remove", None, "VersionOwner", SH.MinCountConstraintComponent),
    ("v2", "versionNumber", "set", Literal("2"), "VersionNumber", SH.DatatypeConstraintComponent),
    ("v2", "versionNumber", "set", Literal(0), "VersionNumber", SH.MinInclusiveConstraintComponent),
    ("v2", "graphIRI", "set", Literal("https://example.test/graph"), "PrimaryGraph", SH.NodeKindConstraintComponent),
    ("v2", DCTERMS.source, "set", Literal("https://example.test/source"), "OriginalSource", SH.NodeKindConstraintComponent),
    ("v2", PROV.generatedAtTime, "remove", None, "CreatedAt", SH.MinCountConstraintComponent),
    ("v2", "observedAt", "set", Literal("2026-01-01"), "ObservedAt", SH.DatatypeConstraintComponent),
    ("v2", "sourceModifiedAt", "set", Literal("2026-01-01"), "SourceModifiedAt", SH.DatatypeConstraintComponent),
    ("v2", "classification", "set", EX.unknown, "VersionClassification", SH.ClassConstraintComponent),
    ("v2", "metadataHash", "set", Literal("abc"), "MetadataHash", SH.PatternConstraintComponent),
    ("v2", PROV.wasRevisionOf, "add", "v2", "PreviousVersion", SH.MaxCountConstraintComponent),
    ("v2", "isCurrent", "set", Literal("true"), "IsCurrent", SH.DatatypeConstraintComponent),
    ("course_classification", DCTERMS.identifier, "set", Literal("video"), "ClassificationCode", SH.InConstraintComponent),
    ("material_container", DCTERMS.identifier, "set", Literal("unclassified"), "ContainerCode", SH.InConstraintComponent),
)


@pytest.mark.parametrize("focus,predicate,mutation,value,shape,component", CASES, ids=[f"{case[4]}-{i}" for i, case in enumerate(CASES)])
def test_shacl_negative_cases(catalogue, focus, predicate, mutation, value, shape, component):
    repository, _, rdf, repo, resource, _, v1, v2 = catalogue
    nodes = {
        "resource": resource, "v1": v1, "v2": v2,
        "course_classification": URIRef(repository.base_iri + "/classification/courses"),
        "material_container": URIRef(repository.base_iri + "/container/learning_materials"),
    }
    graph = rdf.default_graph
    operand = predicate if isinstance(predicate, URIRef) else repo[predicate]
    value = nodes.get(value, value)
    getattr(graph, mutation)((nodes[focus], operand, value))
    report, _ = validate_shapes(graph, str(repo))
    assert not report["conforms"]
    assert any(
        issue["focus"] == nodes[focus].n3() and issue["shape"] == SHAPES[shape].n3()
        and issue["constraint"] == component.n3() and issue["messages"]
        for issue in report["violations"]
    )


@pytest.mark.parametrize("problem,code", [
    ("broken_chain", "predecessor"),
    ("chain_cycle", "predecessor_cycle"),
    ("cross_owner", "cross_resource_predecessor"),
    ("wrong_current", "current_membership"),
    ("duplicate_number", "version_sequence"),
    ("wrong_graph_owner", "primary_graph_iri"),
    ("reused_graph", "graph_reused"),
    ("undeclared_graph", "undeclared_graph"),
    ("container_mismatch", "current_container"),
    ("wrong_source", "source_identity"),
    ("same_as", "unsupported_same_as"),
    ("historical_classification", "sqlite_classification"),
    ("nondefault_data", "unexpected_default_subject"),
])
def test_integrity_negative_cases(catalogue, problem, code):
    repository, snapshot, rdf, repo, resource, other, v1, v2 = catalogue
    default = rdf.default_graph
    other_graph = default.value(other, repo.graphIRI)
    course_graph = default.value(v2, repo.graphIRI)
    source = default.value(v2, DCTERMS.source)
    other_container = URIRef(repository.base_iri + "/container/learning_materials")
    other_class = URIRef(repository.base_iri + "/classification/learning_materials")
    actions = {
        "broken_chain": ("remove", (v2, PROV.wasRevisionOf, None)),
        "chain_cycle": ("set", (v1, PROV.wasRevisionOf, v2)),
        "cross_owner": ("set", (v2, PROV.wasRevisionOf, other)),
        "wrong_current": ("set", (resource, repo.currentVersion, other)),
        "duplicate_number": ("set", (v2, repo.versionNumber, Literal(1))),
        "wrong_graph_owner": ("set", (v2, repo.graphIRI, other_graph)),
        "reused_graph": ("set", (v1, repo.graphIRI, course_graph)),
        "undeclared_graph": ("add", (EX.subject, EX.predicate, Literal("data"))),
        "container_mismatch": ("set", (resource, DCTERMS.isPartOf, other_container)),
        "wrong_source": ("set", (v2, DCTERMS.source, EX.incorrect)),
        "same_as": ("add", (resource, OWL.sameAs, source)),
        "historical_classification": ("set", (v1, repo.classification, other_class)),
        "nondefault_data": ("add", (BNode(), SCHEMA.name, Literal("source author"))),
    }
    operation, triple = actions[problem]
    target = rdf.graph(EX.undeclared) if problem == "undeclared_graph" else default
    getattr(target, operation)(triple)
    report = check_integrity(rdf, **_namespaces(repository), snapshot=snapshot)
    assert report["by_code"][code] >= 1
    assert any(issue["focus"] and issue["message"] for issue in report["violations"] if issue["code"] == code)


@pytest.fixture
def scoped_dataset(tmp_path, approved_contexts):
    repository = make_repository(tmp_path / "scoped.sqlite")
    source, _ = make_record()
    source["@context"] = [*source["@context"], {"ex": str(EX), "xsd": str(XSD)}]
    source["ex:ref"] = {"id": "_:shared"}
    source["ex:count"] = {"@value": "07", "@type": "xsd:integer"}
    source["ex:word"] = {"@value": "mots", "@language": "fr"}
    source["ex:subject"] = {"id": str(EX.object)}
    source["ex:graph"] = {
        "id": str(EX.nested), "@graph": [{"id": "_:shared", "ex:word": {"@value": "Wort", "@language": "de"}}],
    }
    source["ex:blankGraph"] = {"id": "_:graph", "@graph": [{"id": "_:shared", "ex:flag": True}]}
    source["ex:emptyGraph"] = {"id": str(EX.empty), "@graph": []}
    source["ex:reservedGraphName"] = {
        "id": "urn:x-rdflib:default",
        "@graph": [{"id": "_:shared", "ex:word": "Source named graph"}],
    }
    repository.import_resource(*with_normalized(source), observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="changed"), observed_at=when(2))
    dataset = build_dataset(repository, contexts=approved_contexts)
    return repository, dataset, URIRef(source["id"])


@pytest.mark.parametrize("format,suffix", [("trig", ".trig"), ("nquads", ".nq")])
def test_fidelity_preserves_languages_lexical_values_and_cross_graph_blanks(
    scoped_dataset, approved_contexts, tmp_path, format, suffix
):
    repo, rdf, _ = scoped_dataset
    before = repo.database_path.read_bytes()
    snapshot = repo.export_snapshot()
    path = tmp_path / ("scoped" + suffix)
    save_dataset(rdf, path, format=format)
    reloaded = load_dataset(path)
    assert dataset_equivalent(rdf, reloaded)
    report, _ = validate_semantics(reloaded, **_namespaces(repo), snapshot=snapshot, contexts=approved_contexts)
    assert report["conforms"], report
    assert report["fidelity"]["equivalent_versions"] == 2
    assert all(row["expected_graph_scopes"] == 5 for row in report["fidelity"]["versions"])
    assert repo.database_path.read_bytes() == before


def test_empty_source_scope_is_not_reported_as_lost_triples(scoped_dataset, approved_contexts, tmp_path):
    repository, rdf, _ = scoped_dataset
    repo = Namespace(repository.vocabulary_iri)
    empty = list(rdf.default_graph.subjects(repo.sourceGraphName, EX.empty))
    assert len(empty) == 2 and all(not len(rdf.graph(name)) for name in empty)
    path = tmp_path / "empty-graphs.nq"
    save_dataset(rdf, path, format="nquads")
    loaded = load_dataset(path)
    report, _ = validate_semantics(
        loaded, **_namespaces(repository), snapshot=repository.export_snapshot(), contexts=approved_contexts,
    )
    assert report["conforms"]
    assert report["integrity"]["unregistered_declared_graphs"] == sorted(map(str, empty))


@pytest.mark.parametrize("problem", [
    "missing", "extra", "predicate", "iri", "datatype", "language", "lexical", "blank", "scope", "mixed_version",
])
def test_fidelity_negative_cases(scoped_dataset, approved_contexts, problem):
    repo, rdf, origin = scoped_dataset
    ns = Namespace(repo.vocabulary_iri)
    snapshot = repo.export_snapshot()
    first, second = snapshot["resources"][0]["versions"]
    graph = rdf.graph(URIRef(first["graph_iri"]))
    current = rdf.graph(URIRef(second["graph_iri"]))
    nested_id = next(
        identifier for identifier in rdf.default_graph.objects(URIRef(first["version_iri"]), ns.sourceGraph)
        if rdf.default_graph.value(identifier, ns.sourceGraphName) == EX.nested
    )
    nested = rdf.graph(nested_id)
    word = (origin, EX.word, Literal("mots", lang="fr"))
    if problem in {"missing", "predicate", "language", "scope"}:
        graph.remove(word)
    if problem == "extra":
        graph.add((origin, EX.additional, Literal("new")))
    elif problem == "predicate":
        graph.add((origin, EX.wrongPredicate, word[2]))
    elif problem == "iri":
        graph.set((origin, EX.subject, EX.wrongIRI))
    elif problem in {"datatype", "lexical"}:
        literal = Literal("07") if problem == "datatype" else Literal("7", datatype=XSD.integer)
        graph.set((origin, EX["count"], literal))
    elif problem == "language":
        graph.add((origin, EX.word, Literal("mots", lang="de")))
    elif problem == "scope":
        nested.add(word)
    elif problem == "blank":
        subject, predicate, obj = next(iter(nested))
        nested.remove((subject, predicate, obj))
        nested.add((BNode(), predicate, obj))
    elif problem == "mixed_version":
        current.set((origin, EX.ref, graph.value(origin, EX.ref)))
    fidelity, _ = check_fidelity(rdf, snapshot, approved_contexts, str(ns))
    assert not fidelity["conforms"]
    result = next(row for row in fidelity["versions"] if not row["equivalent"])
    if problem not in {"blank", "mixed_version"}:
        assert result["missing_ground_quads"] or result["unexpected_ground_quads"]
    elif problem == "mixed_version":
        assert any("shared with a different version" in error for row in fidelity["versions"] for error in row["scope_errors"])


def test_nonempty_graph_absence_requires_fidelity_reference(catalogue, approved_contexts):
    repository, snapshot, rdf, repo, _, _, _, v2 = catalogue
    missing = rdf.default_graph.value(v2, repo.graphIRI)
    rdf.remove_graph(missing)
    integrity = check_integrity(rdf, **_namespaces(repository))
    assert integrity["conforms"]
    assert str(missing) in integrity["unregistered_declared_graphs"]
    report, _ = validate_semantics(rdf, **_namespaces(repository), snapshot=snapshot, contexts=approved_contexts)
    assert not report["conforms"]
    assert report["integrity"]["by_code"]["missing_nonempty_graph"] == 1


def _deny_network(monkeypatch):
    def unavailable(*_args, **_kwargs):
        pytest.fail("Semantic validation attempted to access the network")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    monkeypatch.setattr(socket.socket, "connect", unavailable)


def test_benchmark_passes_offline_and_is_deterministic(tmp_path, monkeypatch):
    _deny_network(monkeypatch)
    first, second = tmp_path / "first", tmp_path / "second"
    assert main(["evaluate", "--output-dir", str(first)]) == 0
    assert main(["evaluate", "--output-dir", str(second)]) == 0
    report = json.loads((first / "report.json").read_text())
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    assert set(Graph().parse(first / "shacl-report.ttl").objects(None, SH.conforms)) == {Literal(True)}
    assert set(Graph().parse(first / "negative-shacl-report.ttl").objects(None, SH.conforms)) == {Literal(False)}
    questions = report["competency_questions"]
    assert questions["matches_expected"] == questions["question_count"] == questions["executed_successfully"] == 10
    assert report["benchmark"]["versions"] == 7
    assert all(report["read_only_checks"].values())
    assert all(case["detected"] for case in report["negative_examples"])
    assert main(["evaluate", "--output-dir", str(first)]) == 2


def test_incorrect_and_failed_competency_answers(catalogue):
    repository, _, rdf, *_ = catalogue
    questions = json.loads((EVALUATION / "expected-answers.json").read_text())
    expected_courses = questions[0]
    unavailable = {**copy.deepcopy(expected_courses), "id": "CQ-unavailable", "query": "not-present.rq"}
    report = evaluate_questions(rdf, repository.vocabulary_iri, catalogue=[expected_courses, unavailable])
    incorrect, failed = report["questions"]
    assert report["executed_successfully"] == 1
    assert report["matches_expected"] == 0
    assert incorrect["executed"] and not incorrect["correct"]
    assert incorrect["missing"] and incorrect["unexpected"]
    assert not failed["executed"] and "FileNotFoundError" in failed["error"]


@pytest.mark.parametrize("format,suffix", [("trig", ".trig"), ("nquads", ".nq")])
def test_validate_cli_from_local_file_and_sqlite(
    catalogue, approved_contexts, tmp_path, monkeypatch, capsys, format, suffix
):
    _deny_network(monkeypatch)
    repository, snapshot, rdf, _, resource, _, _, _ = catalogue
    database = repository.database_path
    before = database.read_bytes()
    path = tmp_path / ("rdf" + suffix)
    save_dataset(rdf, path, format=format)
    # A local RDF check performs SHACL and integrity without claiming source fidelity.
    initial = ["--base-iri", repository.base_iri, "validate", "--input", str(path)]
    assert main(initial) == 0
    assert "Source fidelity: skipped" in capsys.readouterr().out
    global_args = [
        "--base-iri", repository.base_iri, "--database", str(database),
        "--context-snapshot", str(tmp_path / "amb-snapshot"), "--context-sha256", approved_contexts.sha256,
    ]
    out = tmp_path / "report.json"
    shacl = tmp_path / "report.ttl"
    report_args = ["--report-json", str(out), "--shacl-report", str(shacl)]
    cli = [*global_args, "validate", "--input", str(path), "--against-sqlite", *report_args]
    assert main(cli) == 0
    assert json.loads(out.read_text())["fidelity"]["equivalent_versions"] == 3
    assert main(cli) == 2
    assert "already exists" in capsys.readouterr().err
    assert main([*global_args, "validate", "--resource", str(resource)]) == 0
    assert "2/2 versions equivalent" in capsys.readouterr().out
    assert database.read_bytes() == before
    assert repository.export_snapshot() == snapshot


def test_invalid_dataset_has_structured_and_human_violations(catalogue, tmp_path, capsys):
    repository, _, rdf, repo, resource, _, _, _ = catalogue
    rdf.default_graph.remove((resource, repo.currentVersion, None))
    path = tmp_path / "invalid.trig"
    save_dataset(rdf, path, format="trig")
    json_path, shacl_path = tmp_path / "invalid.json", tmp_path / "invalid.ttl"
    assert main([
        "--base-iri", repository.base_iri, "validate", "--input", str(path),
        "--report-json", str(json_path), "--shacl-report", str(shacl_path),
    ]) == 1
    output = capsys.readouterr().out
    assert str(resource) in output and "MinCountConstraintComponent" in output
    assert not json.loads(json_path.read_text())["conforms"]
    assert set(Graph().parse(shacl_path).objects(None, SH.conforms)) == {Literal(False)}


def test_cli_requires_explicit_matching_context_approval(catalogue, tmp_path, monkeypatch, capsys):
    _deny_network(monkeypatch)
    monkeypatch.delenv("OERSI_RDF_CONTEXT_SHA256", raising=False)
    monkeypatch.delenv("OERSI_RDF_CONTEXT_SNAPSHOT", raising=False)
    monkeypatch.setattr("src.semantic_validation_cli.load_dotenv", lambda: None)
    repository = catalogue[0]
    args = ["--database", str(repository.database_path), "--context-snapshot", str(tmp_path / "amb-snapshot")]
    assert main([*args, "validate"]) == 2
    assert "requires both" in capsys.readouterr().err
    assert main([*args, "--context-sha256", "0" * 64, "validate"]) == 2
    assert "manifest SHA-256 mismatch" in capsys.readouterr().err
