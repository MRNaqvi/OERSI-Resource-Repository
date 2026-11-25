"""Offline competency questions evaluated against independently specified benchmark answers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

from rdflib import Dataset, Literal, Namespace, URIRef
from rdflib.namespace import PROV, XSD

from src.classification import classify_resource
from src.context_snapshot import load_context_snapshot
from src.metadata import normalize_metadata
from src.rdf_export import FORMATS, dataset_from_snapshot, load_dataset, save_dataset
from src.rdf_fidelity import dataset_equivalent
from src.rdf_query import execute_read_query, prepare_read_query
from src.repository import Repository
from src.semantic_validation import ROOT, check_integrity, validate_semantics, validate_shapes

EVALUATION = ROOT / "evaluation"
FIXTURES = EVALUATION / "fixtures"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _benchmark_time(day):
    return f"2026-01-{day:02}T12:00:00.000000+00:00"


def seed_benchmark(database: Path):
    """Populate a new scratch database using the regular import and review APIs."""
    if database.exists() or database.is_symlink():
        raise FileExistsError(database)
    spec = _read_json(FIXTURES / "benchmark.json")
    contexts = load_context_snapshot(FIXTURES / "amb-snapshot", spec["approved_context_sha256"])
    repository = Repository(database, context_loader=contexts.loader())
    resources = {key: _read_json(FIXTURES / item["file"]) for key, item in spec["resources"].items()}
    clock = spec["initial_created_at"]

    def observe(source, at):
        normalized = normalize_metadata(source)
        category, reason = classify_resource(source)
        normalized["classification"] = {"container": category, "reason": reason}
        return repository.import_resource(source, normalized, observed_at=at)

    with patch("src.repository.utc_now", lambda: clock):
        repository.initialize()
        for source in resources.values():
            observe(source, spec["initial_observed_at"])
        for event in spec["course_events"]:
            clock = _benchmark_time(event["repository_day"])
            source = copy.deepcopy(resources["course"])
            if "description" in event:
                source["description"] = event["description"]
            if "classification" in event:
                source["learningResourceType"] = [{"id": "https://w3id.org/kim/hcrt/" + event["classification"]}]
            observe(source, _benchmark_time(event["observed_day"]))
            if "accept_day" in event:
                decision, = repository.list_pending_updates()
                clock = _benchmark_time(event["accept_day"])
                repository.accept_update(decision["update_id"])
    return repository, contexts


def _binding(spec):
    kind, value = spec["type"], spec["value"]
    if kind == "iri":
        return URIRef(value)
    if kind == "integer":
        return Literal(value, datatype=XSD.integer)
    if kind == "time":
        return Literal(value, datatype=XSD.dateTime, normalize=False)
    raise ValueError(f"Unsupported competency question binding type {kind!r}")


def _answer_value(row, column):
    var, separator, field = column.partition("@")
    return row.get(var, {}).get(field if separator else "value")


def _row_key(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def evaluate_questions(dataset, vocabulary_iri: str, *, catalogue=None, queries_dir=ROOT / "queries"):
    """Compare sets of documented SPARQL projections with a static gold file."""
    if catalogue is None:
        catalogue = _read_json(EVALUATION / "expected-answers.json")
    evidence = []
    for question in catalogue:
        result = {"id": question["id"], "query": question["query"], "executed": False, "correct": False}
        evidence.append(result)
        try:
            query = prepare_read_query(
                (queries_dir / question["query"]).read_text(encoding="utf-8"), vocabulary_iri=vocabulary_iri,
            )
            bindings = {name: _binding(spec) for name, spec in question.get("bind", {}).items()}
            response = execute_read_query(dataset, query, bindings=bindings)
            columns = question["columns"]
            variables = {column.partition("@")[0] for column in columns} | question.get("filter", {}).keys()
            if not isinstance(response, dict) or not variables <= set(response["head"]["vars"]):
                raise ValueError("Query does not return the SELECT variables required by the benchmark projection")
            rows = response["results"]["bindings"]
            filters = question.get("filter", {})
            actual = {
                _row_key({column: _answer_value(row, column) for column in columns})
                for row in rows if all(_answer_value(row, name) == value for name, value in filters.items())
            }
            expected = {_row_key(row) for row in question["expected"]}
            result.update(
                executed=True, correct=actual == expected, bindings=question.get("bind", {}), filters=filters,
                expected=[json.loads(row) for row in sorted(expected)],
                actual=[json.loads(row) for row in sorted(actual)],
                missing=[json.loads(row) for row in sorted(expected - actual)],
                unexpected=[json.loads(row) for row in sorted(actual - expected)],
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
    correct = sum(row["correct"] for row in evidence)
    return {
        "question_count": len(evidence),
        "executed_successfully": sum(row["executed"] for row in evidence),
        "matches_expected": correct,
        "query_answer_correctness": correct / len(evidence) if evidence else None,
        "questions": evidence,
    }


def _copy_dataset(dataset):
    cloned = Dataset(default_union=False)
    for graph in dataset.graphs():
        target = cloned.default_graph if graph.identifier == dataset.default_graph.identifier else cloned.graph(graph.identifier)
        for triple in graph:
            target.add(triple)
    return cloned


def negative_examples(dataset, repository, snapshot, contexts):
    """Corrupt disposable RDF copies. No database changes are involved."""
    repo = Namespace(repository.vocabulary_iri)
    resource = next(row for row in snapshot["resources"] if len(row["versions"]) > 1)
    resource_iri = URIRef(resource["resource_iri"])
    current = URIRef(resource["current_version_iri"])
    broken = _copy_dataset(dataset)
    broken.default_graph.remove((resource_iri, repo.currentVersion, current))
    shacl, shacl_rdf = validate_shapes(broken.default_graph, repository.vocabulary_iri)
    current_question = next(
        question for question in _read_json(EVALUATION / "expected-answers.json") if question["id"] == "CQ3"
    )
    query_result, = evaluate_questions(
        broken, repository.vocabulary_iri, catalogue=[current_question],
    )["questions"]

    broken = _copy_dataset(dataset)
    broken.default_graph.remove((current, PROV.wasRevisionOf, None))
    integrity = check_integrity(broken, base_iri=repository.base_iri, vocabulary_iri=repository.vocabulary_iri)

    broken = _copy_dataset(dataset)
    first = resource["versions"][0]
    graph = broken.graph(URIRef(first["graph_iri"]))
    graph.remove((URIRef(resource["source_iri"]), URIRef("http://schema.org/description"), None))
    fidelity = validate_semantics(
        broken, base_iri=repository.base_iri, vocabulary_iri=repository.vocabulary_iri,
        snapshot=snapshot, contexts=contexts,
    )[0]["fidelity"]
    version_result = next(row for row in fidelity["versions"] if row["version_iri"] == first["version_iri"])
    cases = [
        {"case": "missing current pointer", "layer": "SHACL", "detected": not shacl["conforms"],
         "violations": shacl["violations"]},
        {"case": "broken predecessor", "layer": "repository integrity", "detected": not integrity["conforms"],
         "violations": integrity["violations"]},
        {"case": "missing description quad", "layer": "source fidelity", "detected": not version_result["equivalent"],
         "violations": version_result},
        {"case": "wrong answer after removing current pointer", "layer": "competency questions",
         "detected": query_result["executed"] and not query_result["correct"], "violations": query_result},
    ]
    return cases, shacl_rdf


def run_benchmark(scratch: Path):
    database = scratch / "benchmark.sqlite3"
    repository, contexts = seed_benchmark(database)
    before = database.read_bytes()
    snapshot = repository.export_snapshot()
    namespaces = {"base_iri": repository.base_iri, "vocabulary_iri": repository.vocabulary_iri}
    dataset = dataset_from_snapshot(snapshot, contexts=contexts, **namespaces)
    validation, shacl = validate_semantics(dataset, snapshot=snapshot, contexts=contexts, **namespaces)
    questions = evaluate_questions(dataset, repository.vocabulary_iri)
    round_trips = {}
    for format, suffix in FORMATS.items():
        path = scratch / ("benchmark" + suffix)
        save_dataset(dataset, path, format=format)
        reloaded = load_dataset(path)
        reloaded_validation = validate_semantics(reloaded, snapshot=snapshot, contexts=contexts, **namespaces)[0]
        round_trips[format] = {
            "equivalent": dataset_equivalent(dataset, reloaded),
            "query_answers_preserved": evaluate_questions(reloaded, repository.vocabulary_iri) == questions,
            "conforms": reloaded_validation["conforms"],
        }
    negative, negative_shacl = negative_examples(dataset, repository, snapshot, contexts)
    unchanged = {
        "sqlite_bytes_unchanged": database.read_bytes() == before,
        "historical_snapshots_unchanged": repository.export_snapshot() == snapshot,
    }
    report = {
        **validation, "competency_questions": questions, "round_trips": round_trips, "negative_examples": negative,
        "read_only_checks": unchanged,
        "benchmark": {
            "resources": len(snapshot["resources"]),
            "versions": sum(len(resource["versions"]) for resource in snapshot["resources"]),
            "review_outcomes": [
                {key: row[key] for key in ("resource_id", "status", "observed_at", "decided_at", "accepted_version_iri")}
                for row in repository.list_pending_updates(include_decided=True)
            ],
        },
    }
    report["passed"] = (
        report["conforms"] and questions["matches_expected"] == questions["question_count"] >= 10
        and all(all(result.values()) for result in round_trips.values())
        and all(case["detected"] for case in negative) and all(unchanged.values())
    )
    return report, shacl, negative_shacl
