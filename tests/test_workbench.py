from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import Literal, Namespace, URIRef
from streamlit.testing.v1 import AppTest

from src import repository as repository_module
from src.competency_evaluation import FIXTURES, seed_benchmark
from src.rdf_export import dataset_summary
from src.semantic_validation import ROOT
from src.workbench import (
    Workbench, as_csv, general_validation, inspect_dataset, mapping_preview, neighborhood, parse_upload,
    query_table, repository_validation, run_query,
)
from src.workbench_worker import MAX_BYTES
from tests.test_repository import make_record, make_repository
from tests.test_repository_migration import phase2_database
from tests.test_version_management import MATERIAL, when, with_normalized

BASE = "https://example.org/oersi"
EX = Namespace("https://example.test/")
TRIG = b'<https://example.test/a> <https://example.test/p> "a" . <https://example.test/g> { <https://example.test/b> <https://example.test/p> "b" }'
NQUADS = b'<https://example.test/a> <https://example.test/p> "a" .\n<https://example.test/b> <https://example.test/p> "b" <https://example.test/g> .'
SHAPES = b"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.test/> .
ex:Shape a sh:NodeShape ; sh:targetNode ex:a ;
  sh:property [ sh:path ex:p ; sh:minCount 1 ] .
"""


@pytest.fixture(scope="module")
def uploaded():
    return parse_upload("general.trig", TRIG)[1]


@pytest.fixture
def benchmark(tmp_path):
    repo, context = seed_benchmark(tmp_path / "benchmark.sqlite3")
    return Workbench(repo.database_path, repo.base_iri, repo.vocabulary_iri, FIXTURES/"amb-snapshot", context.sha256)


@pytest.mark.parametrize("name,data", [("general.trig", TRIG), ("general.nq", NQUADS), ("general.nquads", NQUADS)])
def test_upload_keeps_named_graphs_and_uses_in_memory_data(name, data, tmp_path):
    format, dataset = parse_upload(name, data)
    assert format == ("trig" if name.endswith("trig") else "nquads")
    assert set(dataset.default_graph) == {(EX.a, EX.p, Literal("a"))}
    assert set(dataset.graph(EX.g)) == {(EX.b, EX.p, Literal("b"))}
    assert inspect_dataset(dataset, EX.g)["resources"] == [str(EX.b)]
    assert dataset_summary(dataset)["total_quads"] == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name,data,match", [
    ("malformed.trig", b"<urn:a> [", "Syntax|IndexError"),
    ("bad.nq", b"<urn:a> <urn:b> nope .", "ParserError"),
    ("remote.jsonld", b'{"@context":"https://example.test/context"}', "Supported dataset formats"),
    ("rdf.rdf", b"<rdf:RDF/>", "Supported dataset formats"),
    ("large.trig", b"a" * (MAX_BYTES + 1), "up to 4 MiB"),
    ("empty.nq", b"", "nonempty"),
])
def test_disallowed_or_malformed_upload(name, data, match):
    with pytest.raises(ValueError, match=match):
        parse_upload(name, data)


def test_parse_input_with_file_and_remote_iris_without_access(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("this document must not be read")
    _, dataset = parse_upload("local.trig",
        f"<relative> <urn:p> <{marker.as_uri()}> . <urn:s> <urn:p> <https://example.test/remote> .".encode())
    assert (URIRef("https://example.invalid/oersi/upload/relative"), URIRef("urn:p"), URIRef(marker.as_uri())) in dataset.default_graph


def test_worker_io_policy_denies_external_file_and_socket(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("local fixture")
    result = subprocess.run([sys.executable, "-c", """
import socket, sys
from src.workbench_worker import _restrict_io
sys.addaudithook(_restrict_io)
for attempt in (lambda: open(sys.argv[1]), socket.socket):
    try:
        attempt()
    except PermissionError:
        pass
    else:
        raise AssertionError('RDF worker allowed external I/O')
""", str(marker)], cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_general_rdf_is_not_a_repository_and_unavailable_layers_are_explicit(uploaded, tmp_path):
    settings = Workbench(tmp_path / "missing.sqlite3", BASE)
    assert general_validation(uploaded)[0]["shacl"]["status"] == "Not evaluated"
    report, turtle = repository_validation(uploaded, settings, against_sqlite=True)
    assert not report["recognized"] and not report["conforms"] and turtle is None
    assert all(report[key]["status"] == "Not evaluated" for key in ("shacl", "integrity", "fidelity", "sqlite_completeness"))
    assert not settings.database.exists()


def test_custom_shacl_selected_graph_and_report_exports(uploaded):
    passed, turtle = general_validation(uploaded, shapes=SHAPES)
    assert passed["shacl"]["status"] == "Passed" and "ValidationReport" in turtle
    failed, turtle = general_validation(uploaded, EX.g, shapes=SHAPES)
    assert failed["shacl"]["status"] == "Failed"
    assert failed["shacl"]["violations"][0]["focusNode"] == [EX.a.n3()]
    assert "MinCountConstraintComponent" in turtle


@pytest.mark.parametrize("directive", [
    'sh:sparql [ sh:select "SELECT * WHERE { ?s ?p ?o }" ]',
    'sh:js [ sh:jsFunctionName "external" ]',
    '<http://www.w3.org/2002/07/owl#imports> <file:///tmp/example.ttl>',
])
def test_uploaded_shapes_refuse_noncore_and_import_directives(uploaded, directive):
    forbidden = SHAPES.replace(b"sh:targetNode ex:a", f"sh:targetNode ex:a ; {directive}".encode())
    with pytest.raises(ValueError, match="SHACL Core"):
        general_validation(uploaded, shapes=forbidden)


def test_repository_validation_uses_pinned_snapshot_and_detects_damage(benchmark):
    before = hashlib.sha256(benchmark.database.read_bytes()).hexdigest()
    dataset, snapshot = benchmark.dataset(with_snapshot=True)
    valid, _ = repository_validation(dataset, benchmark, against_sqlite=True, supplied_snapshot=snapshot)
    assert valid["conforms"] and valid["recognized"] and valid["fidelity"]["equivalent_versions"] == 7
    repo = Namespace(benchmark.namespaces()[1])
    subject = next(dataset.default_graph.subjects(repo.currentVersion))
    dataset.default_graph.remove((subject, repo.currentVersion, None))
    failed, turtle = repository_validation(dataset, benchmark)
    assert failed["shacl"]["status"] == failed["integrity"]["status"] == "Failed"
    assert "MinCountConstraintComponent" in turtle
    missing_pins = Workbench(benchmark.database, BASE)
    partial, _ = repository_validation(dataset, missing_pins, against_sqlite=True)
    assert partial["fidelity"]["status"] == partial["sqlite_completeness"]["status"] == "Not evaluated"
    assert "approved SHA-256" in partial["fidelity"]["reason"]
    assert hashlib.sha256(benchmark.database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("query,expected", [
    ("SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } }", "https://example.test/b"),
    ('ASK { ?s <https://example.test/p> "a" }', "true"),
])
def test_local_queries_and_exports(uploaded, query, expected):
    columns, rows = query_table(run_query(uploaded, query, BASE + "/vocab/", iri="g=https://example.test/g"))
    assert expected in as_csv(columns, rows)


@pytest.mark.parametrize("query,match", [
    ("SELECT * WHERE { SERVICE <https://example.test/query> { ?s ?p ?o } }", "SERVICE"),
    ("SELECT * FROM <file:///tmp/external.ttl> WHERE { ?s ?p ?o }", "FROM"),
    ("INSERT DATA { <urn:s> <urn:p> <urn:o> }", "SELECT or ASK"),
])
def test_query_worker_rejects_remote_sources_and_update(uploaded, query, match):
    with pytest.raises(ValueError, match=match):
        run_query(uploaded, query, BASE + "/vocab/")


def test_bound_neighborhood_and_mapping_preview(uploaded):
    levels, edges = neighborhood(uploaded.graph(EX.g), EX.b, depth=1, node_limit=1)
    assert set(levels) == {EX.b} and not edges
    assert mapping_preview(uploaded)[0]["Potential field"] == "Unmapped"
    assert "'=SUM" in as_csv(["value"], [{"value": "=SUM(A1:A2)"}])


def test_resource_version_timeline_on_original_snapshot(benchmark):
    before = benchmark.database.read_bytes()
    resources = benchmark.resources()
    assert {row["classification"] for row in resources} == {"courses", "learning_materials", "unclassified"}
    course = next(row for row in resources if row["source_iri"] == "http://bio1510.biology.gatech.edu/")
    repository = benchmark.repository()
    identifier = course["resource_id"]
    assert len(repository.list_versions(identifier)) == 4
    assert repository.compare_versions(identifier, 1, 2)["original_jsonld"]["changed"]["description"]
    assert repository.get_version_at_time(identifier, "2026-01-15T12:00:00Z", basis="observation")["version_number"] == 4
    assert repository.get_version_at_time(identifier, "2026-01-15T12:00:00Z", basis="repository")["version_number"] == 3
    assert benchmark.database.read_bytes() == before


def test_migration_requires_confirmation_and_safe_backup_then_verifies(tmp_path):
    path = tmp_path / "phase2.sqlite3"
    phase2_database(path)
    backups = tmp_path / "backups"
    backups.mkdir()
    settings = Workbench(path, BASE, backup_dir=backups)
    before = path.read_bytes()
    preview = settings.migration_preview()
    assert preview["supported"] and preview["schema_version"] == "2"
    with pytest.raises(ValueError, match="confirmation"):
        settings.migrate("previous.sqlite3")
    assert not (backups / "previous.sqlite3").exists()
    with pytest.raises(ValueError, match="filename"):
        settings.migrate("../escape.sqlite3", confirmed=True)
    (backups / "present.sqlite3").write_bytes(b"existing")
    with pytest.raises(FileExistsError, match="already exists"):
        settings.migrate("present.sqlite3", confirmed=True)
    assert before == path.read_bytes()
    result = settings.migrate("previous.sqlite3", confirmed=True)
    assert result["schema_version"] == "3" and not result["supported"]
    assert Path(result["backup"]).is_file()
    assert (backups / "present.sqlite3").read_bytes() == b"existing"


def _migration_state(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        return (
            connection.execute("SELECT config_value FROM repository_config WHERE config_key='schema_version'").fetchone()[0],
            connection.execute(
                """SELECT resource_id, version_iri, graph_iri, original_jsonld, normalized_metadata,
                          metadata_hash, previous_version_iri
                   FROM resource_versions ORDER BY version_iri"""
            ).fetchall(),
            connection.execute(
                "SELECT update_id, status, resource_id, candidate_jsonld FROM pending_updates ORDER BY update_id"
            ).fetchall(),
        )


def test_workbench_migration_rolls_back_after_schema_changes_and_recovers(tmp_path, monkeypatch):
    path = tmp_path / "phase2.sqlite3"
    phase2_database(path)
    backups = tmp_path / "backups"
    backups.mkdir()
    settings = Workbench(path, BASE, backup_dir=backups)
    before = path.read_bytes()
    source_state = _migration_state(path)
    backup = backups / "before-failure.sqlite3"
    migrate = repository_module.migrate_phase2

    def fail_before_commit(connection):
        migrate(connection)
        assert connection.execute(
            "SELECT config_value FROM repository_config WHERE config_key='schema_version'"
        ).fetchone()[0] == "3"
        raise RuntimeError("Simulated failure after updating the migration schema")

    with monkeypatch.context() as patch:
        patch.setattr(repository_module, "migrate_phase2", fail_before_commit)
        with pytest.raises(RuntimeError, match="Simulated failure after updating"):
            settings.migrate(backup.name, confirmed=True)

    assert path.read_bytes() == before
    assert _migration_state(path) == _migration_state(backup) == source_state
    saved = backup.read_bytes()
    assert settings.migration_preview()["supported"]
    with pytest.raises(FileExistsError, match="already exists"):
        settings.migrate(backup.name, confirmed=True)
    assert path.read_bytes() == before and backup.read_bytes() == saved

    result = settings.migrate("before-retry.sqlite3", confirmed=True)
    assert result["schema_version"] == "3"
    assert _migration_state(path) == ("3", *source_state[1:])
    assert _migration_state(Path(result["backup"])) == source_state
    assert backup.read_bytes() == saved


@pytest.mark.parametrize("decision", ["Accept", "Reject"])
def test_explicit_pending_review_and_snapshot_retention(tmp_path, benchmark, decision):
    repository = benchmark.repository(pinned=True)
    source, normalized = make_record()
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    update, = repository.list_pending_updates()
    before = repository.database_path.read_bytes()
    v1 = repository.get_version(normalized["stable_id"], 1)
    with pytest.raises(ValueError, match="confirmation"):
        benchmark.decide(update["update_id"], decision)
    assert before == repository.database_path.read_bytes()
    result = benchmark.decide(update["update_id"], decision, confirmed=True)
    assert result["status"] == {"Accept": "accepted", "Reject": "rejected"}[decision]
    assert repository.get_version(normalized["stable_id"], 1) == v1
    assert len(repository.list_versions(normalized["stable_id"])) == (2 if decision == "Accept" else 1)


def _configured_app(monkeypatch, benchmark):
    for name, value in {
        "OERSI_REPOSITORY_DATABASE": benchmark.database, "OERSI_REPOSITORY_BASE_IRI": benchmark.base_iri,
        "OERSI_REPOSITORY_VOCABULARY_IRI": benchmark.vocabulary_iri,
        "OERSI_RDF_CONTEXT_SNAPSHOT": benchmark.context_snapshot, "OERSI_RDF_CONTEXT_SHA256": benchmark.context_sha256,
        "OERSI_WORKBENCH_BACKUP_DIR": benchmark.backup_dir,
    }.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))
    return AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=25).run()


def test_app_dashboard_navigation_and_lab(monkeypatch, benchmark):
    data = benchmark.database.read_bytes()
    app = _configured_app(monkeypatch, benchmark)
    assert not app.exception and not app.error
    assert app.title[0].value == "Repository dashboard"
    assert [metric.value for metric in app.metric[:4]] == ["4", "7", "0", "3"]
    app.sidebar.radio[0].set_value("Resources").run()
    assert not app.exception and not app.error
    assert app.title[0].value == "Resource explorer"
    app.sidebar.radio[0].set_value("Upload RDF").run()
    app.file_uploader[0].set_value(("general.trig", TRIG, "application/trig")).run()
    assert not app.exception and not app.error
    assert app.success[0].value == "Syntax parsing passed"
    app.sidebar.radio[0].set_value("Semantic Validation").run()
    next(radio for radio in app.radio if radio.label == "Validation mode").set_value("OERSI repository").run()
    next(button for button in app.button if button.label == "Run validation").click().run()
    assert "no repository entities" in app.error[0].value
    assert benchmark.database.read_bytes() == data


def _widget(app, kind, label):
    return next(widget for widget in getattr(app, kind) if widget.label == label)


def _visit(app, page, heading):
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception
    assert app.title[0].value == heading


def test_app_sparql_examples_bindings_and_results(monkeypatch, benchmark):
    before = benchmark.database.read_bytes()
    app = _configured_app(monkeypatch, benchmark)
    _visit(app, "SPARQL Workbench", "SPARQL workbench")
    _widget(app, "button", "Load repository RDF").click().run()
    _widget(app, "selectbox", "Example competency question").set_value("CQ7").run()
    _widget(app, "button", "Load example").click().run()
    assert "?from ?to" in _widget(app, "text_area", "SPARQL").value
    _widget(app, "button", "Run query").click().run()
    assert not app.exception and not app.error
    changes = app.dataframe[-1].value
    assert changes[["from", "to"]].to_dict("records") == [{"from": "courses", "to": "learning_materials"}]

    values = " ".join(str(number) for number in range(1500))
    _widget(app, "text_area", "SPARQL").set_value(f"SELECT ?value WHERE {{ VALUES ?value {{ {values} }} }}")
    _widget(app, "button", "Run query").click().run()
    assert not app.exception and not app.error
    assert len(app.dataframe[-1].value) == 1000
    assert [warning.value for warning in app.warning] == ["Result limited to the first 1,000 rows."]

    resource = benchmark.resources()[0]["resource_iri"]
    _widget(app, "text_area", "Iri bindings").set_value(f"resource={resource}")
    _widget(app, "text_area", "SPARQL").set_value("ASK { ?resource repo:currentVersion ?version }")
    _widget(app, "button", "Run query").click().run()
    assert not app.exception and not app.error
    assert _widget(app, "metric", "ASK").value == "true"
    assert benchmark.database.read_bytes() == before


def test_app_knowledge_graph_scope_and_node_inspection(monkeypatch, benchmark):
    before = benchmark.database.read_bytes()
    app = _configured_app(monkeypatch, benchmark)
    _visit(app, "Knowledge Graph", "Knowledge graph")
    _widget(app, "button", "Load repository RDF").click().run()
    resource = next(row["resource_iri"] for row in benchmark.resources() if row["source_iri"] == "http://bio1510.biology.gatech.edu/")
    _widget(app, "selectbox", "Neighborhood center").set_value(URIRef(resource)).run()
    version = URIRef(f"{resource}/version/4")
    _widget(app, "selectbox", "Inspect node").set_value(version).run()
    assert not app.exception and not app.error
    assert len(app.get("plotly_chart")) == 1
    statements = app.dataframe[-1].value
    assert (
        (statements["Subject"] == version.n3())
        & (statements["Relationship"] == f"<{BASE}/vocab/graphIRI>")
    ).any()
    assert benchmark.database.read_bytes() == before


def test_app_migration_preview_and_confirmed_backup(monkeypatch, benchmark, tmp_path):
    path = tmp_path / "app-phase2.sqlite3"
    phase2_database(path)
    backups = tmp_path / "backups"
    backups.mkdir()
    before = path.read_bytes()
    original = _migration_state(path)
    app = _configured_app(monkeypatch, replace(benchmark, database=path, backup_dir=backups))
    _visit(app, "Migrations", "Migration manager")
    assert _widget(app, "metric", "Detected schema").value == "2"
    _widget(app, "text_input", "New backup filename").set_value("before-app.sqlite3").run()
    _widget(app, "button", "Apply migration").click().run()
    assert "Explicit migration confirmation is required" in app.error[0].value
    assert path.read_bytes() == before and not list(backups.iterdir())

    _widget(app, "checkbox", "I confirm the backup path and want to migrate the configured database.").check()
    _widget(app, "button", "Apply migration").click().run()
    assert not app.exception and not app.error
    assert "Migration verified" in app.success[0].value
    assert _migration_state(backups / "before-app.sqlite3") == original
    assert _migration_state(path) == ("3", *original[1:])


@pytest.mark.parametrize("decision", ["Accept", "Reject"])
def test_app_pending_review_confirmation_and_audit(monkeypatch, benchmark, decision):
    repo = benchmark.repository(pinned=True)
    source, normalized = make_record()
    repo.import_resource(source, normalized, observed_at=when(1))
    repo.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(2))
    update, = repo.list_pending_updates()
    before = benchmark.database.read_bytes()
    initial = repo.get_version(normalized["stable_id"], 1)

    app = _configured_app(monkeypatch, benchmark)
    _visit(app, "Pending Reviews", "Pending reviews")
    assert any(tab.label == "Proposed metadata" for tab in app.tabs)
    _widget(app, "button", "Record decision").click().run()
    assert "Explicit review confirmation is required" in app.error[0].value
    assert benchmark.database.read_bytes() == before

    _widget(app, "radio", "Decision").set_value(decision)
    _widget(app, "checkbox", "I confirm this decision for the displayed candidate.").check()
    _widget(app, "button", "Record decision").click().run()
    assert not app.exception and not app.error
    assert f"{decision} recorded." in app.success[0].value
    _widget(app, "checkbox", "Show decided updates").check().run()
    status = "accepted" if decision == "Accept" else "rejected"
    audit = app.dataframe[0].value.set_index("update_id")
    assert audit.loc[update["update_id"], "status"] == status
    assert repo.get_version(normalized["stable_id"], 1) == initial
    assert len(repo.list_versions(normalized["stable_id"])) == (2 if status == "accepted" else 1)


def test_app_settings_context_approval(monkeypatch, benchmark):
    before = benchmark.database.read_bytes()
    app = _configured_app(monkeypatch, benchmark)
    _visit(app, "Settings", "Settings")
    configured = app.dataframe[0].value.set_index("Setting")["Configured value"]
    assert configured.loc["SQLite database"] == str(benchmark.database)
    assert configured.loc["Approved context fingerprint"] == benchmark.context_sha256
    _widget(app, "button", "Verify approved context").click().run()
    assert not app.exception and not app.error
    assert app.success[0].value == "Verified 1 local context document(s)"
    assert benchmark.database.read_bytes() == before
