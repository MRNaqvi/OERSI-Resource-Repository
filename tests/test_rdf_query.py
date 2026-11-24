from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from rdflib import Dataset, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery

import src.rdf_cli as cli
import src.repository as repository_module
from src.rdf_export import build_dataset, save_dataset
from src.rdf_query import execute_read_query, prepare_read_query
from src.repository import Repository
from tests.jsonld_fixtures import LocalAmbContextLoader
from tests.test_repository import make_record
from tests.test_version_management import MATERIAL, when, with_normalized

QUERIES = Path(__file__).resolve().parents[1] / "queries"


@pytest.fixture
def history(tmp_path, monkeypatch):
    clock = when(11)
    monkeypatch.setattr(repository_module, "utc_now", lambda: clock)
    repository = Repository(
        tmp_path / "repo.sqlite", base_iri="https://catalog.example/repository",
        vocabulary_iri="https://catalog.example/rdf-terms/", context_loader=LocalAmbContextLoader(),
    )
    repository.initialize()
    original, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(original, normalized, observed_at=when(1))
    other = {}
    for label, source, type_id in (
        ("course", "https://example.edu/other-course", "course"),
        ("material", "https://example.edu/lesson", "textbook"),
        ("unclassified", "https://example.edu/unknown", "other"),
    ):
        metadata = make_record(source, f"https://w3id.org/kim/hcrt/{type_id}")
        repository.import_resource(*metadata, observed_at=when(1))
        other[label] = repository.get_resource(metadata[1]["stable_id"])["resource_iri"]
    clock = when(12)
    revision = with_normalized(
        original, description="New description", dateModified="2025-12-29T10:00:00+02:00",
    )
    repository.import_resource(*revision, observed_at=when(2))
    clock = when(13)
    repository.import_resource(*with_normalized(revision[0], name="Current title"), observed_at=when(3))
    reclassified = with_normalized(revision[0], name="Current title", learningResourceType=MATERIAL)
    repository.import_resource(*reclassified, observed_at=when(4))
    clock = when(14)
    pending, = repository.list_pending_updates()
    repository.accept_update(pending["update_id"])
    resource = repository.get_resource(identifier)["resource_iri"]
    return repository, resource, original, other


def _query(dataset, repository, name, **bindings):
    query = prepare_read_query((QUERIES / name).read_text(), vocabulary_iri=repository.vocabulary_iri)
    result = execute_read_query(dataset, query, bindings=bindings)
    return result["results"]["bindings"]


def _values(rows, key):
    return [row[key]["value"] for row in rows]


def test_all_sample_queries_and_temporal_results(history, approved_contexts):
    repository, resource, original, other = history
    before = repository.database_path.read_bytes()
    dataset = build_dataset(repository, contexts=approved_contexts)
    args = {"resource": URIRef(resource)}

    courses = _query(dataset, repository, "list-courses.rq")
    assert _values(courses, "resource") == [other["course"]]
    materials = _query(dataset, repository, "list-learning-materials.rq")
    assert set(_values(materials, "resource")) == {other["material"], resource}
    assert other["unclassified"] not in _values(courses + materials, "resource")

    list_versions = _query(dataset, repository, "list-versions.rq", **args)
    assert _values(list_versions, "versionNumber") == ["1", "2", "3", "4"]
    assert _values(list_versions, "isCurrent") == ["false", "false", "false", "true"]
    assert len(set(_values(list_versions, "graph"))) == 4

    current = _query(dataset, repository, "current-metadata.rq", **args)
    assert set(_values(current, "version")) == {f"{resource}/version/4"}
    current_root = [
        row for row in current if row["subject"]["value"] == original["id"]
        and row["predicate"]["value"] == "http://schema.org/description"
    ]
    assert current_root[0]["object"] == {"type": "literal", "value": "New description", "xml:lang": "en"}
    historical = _query(dataset, repository, "historical-metadata.rq", versionNumber=Literal(1), **args)
    assert set(_values(historical, "version")) == {f"{resource}/version/1"}
    historical_by_iri = _query(
        dataset, repository, "historical-metadata.rq", version=URIRef(f"{resource}/version/1"), **args,
    )
    assert historical == historical_by_iri
    assert original["description"] in _values(historical, "object")

    revisions = _query(dataset, repository, "version-revisions.rq", **args)
    assert _values(revisions, "previous") == [f"{resource}/version/{number}" for number in (1, 2, 3)]
    changes = _query(dataset, repository, "classification-changes.rq")
    assert len(changes) == 1
    assert changes[0]["from"]["value"] == "courses"
    assert changes[0]["to"]["value"] == "learning_materials"
    assert changes[0]["version"]["value"] == f"{resource}/version/4"

    at = Literal(when(13), datatype=XSD.dateTime)
    temporal = _query(dataset, repository, "metadata-at-time.rq", at=at, **args)
    assert {row["basis"]["value"]: row["versionNumber"]["value"] for row in temporal} == {
        "observation": "4", "repository": "3",
    }
    for row in temporal:
        phase3 = repository.get_version_at_time(resource, when(13), basis=row["basis"]["value"])
        assert row["version"]["value"] == phase3["version_iri"]
    early = _query(
        dataset, repository, "metadata-at-time.rq", at=Literal(when(1), datatype=XSD.dateTime), **args,
    )
    assert [(row["basis"]["value"], row["versionNumber"]["value"]) for row in early] == [("observation", "1")]

    provenance = _query(dataset, repository, "source-provenance.rq", **args)
    assert set(_values(provenance, "source")) == {original["id"]}
    assert "sourceModified" not in provenance[0]
    assert provenance[1]["sourceModified"] == {
        "type": "literal", "value": "2025-12-29T08:00:00.000000+00:00", "datatype": str(XSD.dateTime),
    }

    comparison = _query(
        dataset, repository, "compare-version-triples.rq", fromVersion=Literal(1), toVersion=Literal(3), **args,
    )
    descriptions = [
        (row["change"]["value"], row["object"]["value"]) for row in comparison
        if row["predicate"]["value"] == "http://schema.org/description"
    ]
    assert set(descriptions) == {("removed", original["description"]), ("added", "New description")}
    assert all(row["subject"]["type"] == "uri" and row["object"]["type"] != "bnode" for row in comparison)
    assert repository.database_path.read_bytes() == before
    assert {file.name for file in QUERIES.glob("*.rq")} == {
        "list-courses.rq", "list-learning-materials.rq", "current-metadata.rq", "historical-metadata.rq",
        "list-versions.rq", "version-revisions.rq", "classification-changes.rq", "metadata-at-time.rq",
        "source-provenance.rq", "compare-version-triples.rq",
    }


def test_select_and_ask_keep_default_graph_separate_and_report_rdf_terms(history, approved_contexts):
    repository, resource, original, _ = history
    dataset = build_dataset(repository, contexts=approved_contexts)
    iri = URIRef(resource)
    source = URIRef(original["id"])
    def run(text, **bindings):
        query = prepare_read_query(text, vocabulary_iri=repository.vocabulary_iri)
        return execute_read_query(dataset, query, bindings=bindings)

    assert run("ASK { ?resource repo:currentVersion ?version }", resource=iri) is True
    assert run("ASK { ?source <http://schema.org/description> ?value }", source=source) is False
    assert run("ASK { GRAPH ?g { ?source <http://schema.org/description> ?value } }", source=source) is True
    result = run(
        "SELECT ?entity ?number WHERE { ?entity repo:versionNumber ?number } ORDER BY ?number"
    )
    assert result["head"]["vars"] == ["entity", "number"]
    assert result["results"]["bindings"][0]["number"]["datatype"] == str(XSD.integer)
    result = run("SELECT ?obj WHERE { GRAPH ?graph { ?source <http://schema.org/publisher> ?obj } }", source=source)
    assert result["results"]["bindings"]
    assert all(row["obj"]["type"] == "bnode" for row in result["results"]["bindings"])


@pytest.mark.parametrize(("query", "message"), [
    ("SELECT * FROM <urn:local-test:outside> WHERE { ?s ?p ?o }", "FROM"),
    ("ASK FROM NAMED <urn:local-test:outside> { GRAPH ?graph { ?s ?p ?o } }", "FROM NAMED"),
    ("ASK { SERVICE <urn:local-test:outside> { ?s ?p ?o } }", "SERVICE"),
    ("ASK { OPTIONAL { SeRvIcE SiLeNt ?endpoint { ?s ?p ?o } } }", "SERVICE"),
    ("SELECT * { { SELECT * WHERE { SERVICE <urn:local-test:outside> {} } } }", "SERVICE"),
    ("ASK { FILTER EXISTS { SERVICE <urn:local-test:outside> {} } }", "SERVICE"),
    ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", "Only SPARQL SELECT and ASK"),
    ("DESCRIBE <urn:local-test:entity>", "Only SPARQL SELECT and ASK"),
    ("INSERT DATA { <urn:s> <urn:p> <urn:o> }", "valid SPARQL SELECT or ASK"),
    ("SELECT", "valid SPARQL SELECT or ASK"),
])
def test_restricted_query_structure(query, message):
    with pytest.raises(ValueError, match=message):
        prepare_read_query(query)


def test_algebra_is_checked_even_if_preparation_is_bypassed():
    dataset = Dataset(default_union=False)
    query = translateQuery(parseQuery("ASK { SERVICE <urn:local-test:outside> { ?s ?p ?o } }"))
    with pytest.raises(ValueError, match="SERVICE"):
        execute_read_query(dataset, query)


def test_keywords_in_comments_and_literals_remain_legal():
    text = '# SERVICE FROM\nASK { FILTER(STR("SERVICE FROM") = "SERVICE FROM") }'
    assert execute_read_query(Dataset(default_union=False), prepare_read_query(text)) is True


def test_cli_export_validate_and_query_offline(history, tmp_path, approved_contexts, monkeypatch, capsys):
    repository, resource, _, _ = history
    args = [
        "--database", str(repository.database_path), "--base-iri", repository.base_iri,
        "--vocabulary-iri", repository.vocabulary_iri,
    ]

    def call(*arguments, use_snapshot=False):
        pins = [
            "--context-snapshot", str(tmp_path / "amb-snapshot"), "--context-sha256", approved_contexts.sha256,
        ] if use_snapshot else []
        monkeypatch.setattr(sys, "argv", ["rdf_cli", *args, *pins, *arguments])
        status = cli.main()
        return status, json.loads(capsys.readouterr().out)

    monkeypatch.setattr(cli, "Repository", lambda *args, **kwargs: repository)
    target = tmp_path / "export.trig"
    status, exported = call("export", "--format", "trig", "--output", str(target), "--resource", resource, use_snapshot=True)
    assert status == 0 and exported["named_graphs"] == 4
    assert exported["context_snapshot_sha256"] == approved_contexts.sha256
    assert call("validate", "--input", str(target))[1]["total_quads"] == exported["total_quads"]
    assert call("export", "--format", "trig", "--output", str(target), "--resource", resource, use_snapshot=True)[0] == 1

    def unexpected_sqlite(*args, **kwargs):
        raise AssertionError("CLI should not open a repository for file input or forbidden queries")
    monkeypatch.setattr(cli, "Repository", unexpected_sqlite)
    query = str(QUERIES / "metadata-at-time.rq")
    status, result = call(
        "query", "--input", str(target), "--file", query, "--bind-iri", f"resource={resource}",
        "--bind-time", f"at={when(13)}",
    )
    assert status == 0
    assert _values(result["results"]["bindings"], "versionNumber") == ["4", "3"]

    historical = str(QUERIES / "historical-metadata.rq")
    status, result = call(
        "query", "--input", str(target), "--file", historical, "--bind-integer", "versionNumber=2",
    )
    assert status == 0 and set(_values(result["results"]["bindings"], "versionNumber")) == {"2"}
    status, error = call("query", "--input", str(target), "--file", query, "--bind-time", "at=2026-01-01")
    assert status == 1 and "time zone" in error["error"]
    status, error = call("query", "--input", str(target), "--file", str(tmp_path / "no-file.rq"))
    assert status == 1 and "FileNotFoundError" in error["error"]

    restricted = tmp_path / "restricted.rq"
    restricted.write_text("ASK FROM <urn:local-test:outside> {}")
    status, error = call("query", "--file", str(restricted))
    assert status == 1 and "SPARQL FROM" in error["error"]


def test_nquads_reload_can_query_without_sqlite(history, tmp_path, approved_contexts):
    repository, _, _, _ = history
    dataset = build_dataset(repository, contexts=approved_contexts)
    target = tmp_path / "dataset.nq"
    save_dataset(dataset, target, format="nquads")
    from src.rdf_export import load_dataset
    loaded = load_dataset(target)
    assert _query(loaded, repository, "list-versions.rq") == _query(dataset, repository, "list-versions.rq")
