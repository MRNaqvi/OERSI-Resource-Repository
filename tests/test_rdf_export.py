from __future__ import annotations

import sqlite3

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, XSD

from src.context_snapshot import ContextDocument, load_context_snapshot, write_context_snapshot
from src.rdf_export import build_dataset, dataset_summary, load_dataset, save_dataset
from src.repository import Repository
from tests.jsonld_fixtures import LocalAmbContextLoader
from tests.test_repository import make_record, make_repository
from tests.test_repository_migration import phase2_database
from tests.test_version_management import MATERIAL, when, with_normalized

SCHEMA = Namespace("http://schema.org/")
EX = Namespace("https://example.test/source-vocab/")


def _version_ids(repository, resource_id):
    versions = repository.list_versions(resource_id)
    return [(URIRef(row["version_iri"]), URIRef(row["graph_iri"])) for row in versions]


def _dataset_as_graph(dataset):
    """Represent quads as RDF, retaining graph identity and cross-graph blank-node sharing."""
    represented = Graph()
    for subject, predicate, obj, context in dataset.quads():
        quad = BNode()
        represented.add((quad, RDF.subject, subject))
        represented.add((quad, RDF.predicate, predicate))
        represented.add((quad, RDF.object, obj))
        represented.add((quad, EX.graph, context or EX.default))
    return represented


def test_default_graph_uses_configured_namespaces_and_version_provenance(tmp_path, approved_contexts):
    repository = Repository(
        tmp_path / "repo.sqlite",
        base_iri="https://repo.example/catalog/",
        vocabulary_iri="https://vocab.example/releases/",
        context_loader=LocalAmbContextLoader(),
    )
    repository.initialize()
    source, normalized = make_record()
    course = with_normalized(source, dateModified="2025-12-30T10:00:00-05:00")
    repository.import_resource(*course, observed_at=when(1))
    repository.import_resource(*with_normalized(course[0], description="revised"), observed_at=when(2))
    identifier = normalized["stable_id"]
    before = repository.database_path.read_bytes()
    dataset = build_dataset(repository, contexts=approved_contexts)
    default = dataset.default_graph
    repo = Namespace(repository.vocabulary_iri)
    resource = URIRef(repository.get_resource(identifier)["resource_iri"])
    original = URIRef(source["id"])
    (v1, g1), (v2, g2) = _version_ids(repository, identifier)
    assert not dataset.default_union
    assert str(resource).startswith("https://repo.example/catalog/resource/")
    assert str(g2) == f"https://repo.example/catalog/graph/resource/{identifier}/version/2"
    assert (resource, RDF.type, repo.Resource) in default
    assert (resource, DCTERMS.isPartOf, URIRef("https://repo.example/catalog/container/courses")) in default
    assert set(default.objects(resource, DCTERMS.hasVersion)) == {v1, v2}
    assert set(default.objects(resource, repo.currentVersion)) == {v2}
    assert (v1, DCTERMS.isVersionOf, resource) in default
    assert (v2, PROV.wasRevisionOf, v1) in default
    assert not list(default.objects(v1, PROV.wasRevisionOf))
    assert (v2, DCTERMS.source, original) in default and resource != original
    assert (v1, repo.graphIRI, g1) in default and (v2, repo.graphIRI, g2) in default
    assert (v1, repo.isCurrent, Literal(False)) in default
    assert (v2, repo.isCurrent, Literal(True)) in default
    assert default.value(v2, repo.observedAt).datatype == XSD.dateTime
    assert str(default.value(v2, PROV.generatedAtTime)) == repository.get_current_version(identifier)["created_at"]
    assert str(default.value(v2, repo.sourceModifiedAt)) == "2025-12-30T15:00:00.000000+00:00"
    assert default.value(v2, repo.metadataHash) == Literal(repository.get_current_version(identifier)["metadata_hash"])
    assert not list(default.triples((None, OWL.sameAs, None)))
    assert not list(default.triples((original, None, None)))
    assert (original, RDF.type, SCHEMA.LearningResource) in dataset.graph(g1)
    assert not list(dataset.graph(g1).triples((resource, None, None)))
    assert repository.database_path.read_bytes() == before


def test_three_versions_and_unclassified_membership_remain_separate(tmp_path, approved_contexts):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="edited"), observed_at=when(2))
    repository.import_resource(
        *with_normalized(source, learningResourceType=[], description="unclassified after review"),
        observed_at=when(3),
    )
    pending, = repository.list_pending_updates()
    repository.accept_update(pending["update_id"])
    dataset = build_dataset(repository, contexts=approved_contexts)
    repo = Namespace(repository.vocabulary_iri)
    default = dataset.default_graph
    resource = URIRef(repository.get_resource(identifier)["resource_iri"])
    original = URIRef(source["id"])
    (v1, g1), (v2, g2), (v3, g3) = _version_ids(repository, identifier)
    assert dataset_summary(dataset)["named_graphs"] == 3
    assert not list(default.objects(resource, DCTERMS.isPartOf))
    assert default.value(resource, repo.currentVersion) == v3
    assert default.value(v1, repo.classification) == URIRef(f"{repository.base_iri}/classification/courses")
    assert default.value(v2, repo.classification) == default.value(v1, repo.classification)
    assert default.value(v3, repo.classification) == URIRef(f"{repository.base_iri}/classification/unclassified")
    assert (v3, PROV.wasRevisionOf, v2) in default
    assert default.value(v1, repo.sourceModifiedAt) is None
    assert [
        str(dataset.graph(graph).value(original, SCHEMA.description)) for graph in (g1, g2, g3)
    ] == [source["description"], "edited", "unclassified after review"]


@pytest.mark.parametrize(("format", "extension"), [("trig", "trig"), ("nquads", "nq")])
def test_nested_graphs_typed_literals_and_shared_blank_nodes_survive_round_trip(tmp_path, approved_contexts, format, extension):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    original = URIRef(source["id"])
    source_graph = URIRef("https://example.test/source-graph")
    named_object = {
        "@id": str(source_graph),
        "@graph": [{"id": "_:shared", "ex:note": {"@value": "im graph", "@language": "de"}}],
    }
    blank_named_object = {
        "@id": "_:graph",
        "@graph": [{"id": "_:shared", "ex:flag": True}],
    }
    source = {
        **source,
        "@context": [*source["@context"], {"ex": str(EX), "xsd": str(XSD)}],
        "@graph": [{"id": "_:shared", "ex:note": "explicit root graph"}],
        "ex:nested": named_object,
        "ex:blankGraph": blank_named_object,
        "ex:reference": {"id": "_:shared"},
        "ex:word": {"@value": "م", "@language": "ar", "@direction": "rtl"},
        "ex:number": [{"@value": "07", "@type": "xsd:integer"}, {"@value": "7", "@type": "xsd:integer"}],
    }
    repository.import_resource(*with_normalized(source), observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="changed"), observed_at=when(2))
    dataset = build_dataset(repository, contexts=approved_contexts)
    repo = Namespace(repository.vocabulary_iri)
    (v1, g1), (v2, g2) = _version_ids(repository, normalized["stable_id"])
    default = dataset.default_graph
    shared = dataset.graph(g1).value(original, EX.reference)
    assert isinstance(shared, BNode)
    assert dataset.graph(g2).value(original, EX.reference) != shared
    assert (original, EX.nested, source_graph) in dataset.graph(g1)
    assert {str(default.value(graph, repo.sourceGraphName)) for graph in default.objects(v1, repo.sourceGraph)} >= {
        str(source_graph), str(original),
    }

    by_name = {default.value(graph, repo.sourceGraphName): graph for graph in default.objects(v1, repo.sourceGraph)}
    assert len(by_name) == 3
    assert (shared, EX.note, Literal("im graph", lang="de")) in dataset.graph(by_name[source_graph])
    assert any(str(obj) == "explicit root graph" for obj in dataset.graph(by_name[original]).objects(shared, EX.note))
    blank_graph_name = dataset.graph(g1).value(original, EX.blankGraph)
    assert isinstance(blank_graph_name, BNode)
    assert (shared, EX.flag, Literal(True)) in dataset.graph(by_name[blank_graph_name])
    assert set(default.objects(v1, repo.sourceGraph)).isdisjoint(default.objects(v2, repo.sourceGraph))
    literal = dataset.graph(g1).value(original, EX.word)
    assert literal.datatype == URIRef("https://www.w3.org/ns/i18n#ar_rtl")
    assert {str(obj) for obj in dataset.graph(g1).objects(original, EX.number)} == {"07", "7"}

    target = tmp_path / f"dataset.{extension}"
    save_dataset(dataset, target, format=format)
    reloaded = load_dataset(target)
    assert dataset_summary(reloaded) == dataset_summary(dataset)
    assert set(graph.identifier for graph in reloaded.graphs()) == set(graph.identifier for graph in dataset.graphs())
    assert isomorphic(_dataset_as_graph(dataset), _dataset_as_graph(reloaded))
    if format == "nquads":
        assert b"<urn:x-rdflib:default>" not in target.read_bytes()


def test_empty_repository_and_empty_named_graph_formats(tmp_path, approved_contexts):
    repository = make_repository(tmp_path / "repo.sqlite")
    dataset = build_dataset(repository, contexts=approved_contexts)
    repo = Namespace(repository.vocabulary_iri)
    assert len(list(dataset.default_graph.subjects(RDF.type, repo.Container))) == 2
    assert dataset_summary(dataset)["named_graphs"] == 0
    empty = URIRef("https://example.test/empty-graph")
    dataset.graph(empty)
    dataset.default_graph.add((EX.version, repo.graphIRI, empty))
    for format, extension in (("trig", "trig"), ("nquads", "nq")):
        target = tmp_path / f"empty.{extension}"
        save_dataset(dataset, target, format=format)
        reloaded = load_dataset(target)
        assert (EX.version, repo.graphIRI, empty) in reloaded.default_graph
        assert len(reloaded.graph(empty)) == 0
        if format == "trig":
            assert f"<{empty}> {{\n}}".encode() in target.read_bytes()


def test_read_snapshot_can_select_by_id_or_iri_and_release_before_expansion(tmp_path, approved_contexts, monkeypatch):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*make_record("https://example.test/other"), observed_at=when(1))
    iri = repository.get_resource(identifier)["resource_iri"]
    for selection in (identifier, iri):
        assert dataset_summary(build_dataset(repository, selection, contexts=approved_contexts))["named_graphs"] == 1
    with pytest.raises(LookupError, match="Resource not found"):
        build_dataset(repository, "missing", contexts=approved_contexts)

    remote_document = ContextDocument.remote_document
    wrote_after_read = False

    def concurrent_update(document):
        nonlocal wrote_after_read
        if not wrote_after_read:
            wrote_after_read = True
            writer = Repository(repository.database_path, context_loader=LocalAmbContextLoader())
            writer.import_resource(*with_normalized(source, description="concurrent edit"), observed_at=when(2))
        return remote_document(document)

    monkeypatch.setattr(ContextDocument, "remote_document", concurrent_update)
    dataset = build_dataset(repository, identifier, contexts=approved_contexts)
    assert wrote_after_read and len(repository.list_versions(identifier)) == 2
    assert dataset_summary(dataset)["named_graphs"] == 1
    assert dataset.default_graph.value(URIRef(iri), Namespace(repository.vocabulary_iri).currentVersion) == (
        URIRef(f"{iri}/version/1")
    )


def test_legacy_observation_fallback_is_exported(tmp_path, approved_contexts):
    path = tmp_path / "phase2.sqlite"
    phase2_database(path)
    repository = Repository(path, context_loader=LocalAmbContextLoader())
    repository.migrate(tmp_path / "backup.sqlite")
    dataset = build_dataset(repository, contexts=approved_contexts)
    repo = Namespace(repository.vocabulary_iri)
    versions = list(dataset.default_graph.subjects(RDF.type, repo.Version))
    assert len(versions) == 2
    for version in versions:
        assert dataset.default_graph.value(version, repo.observedAt) == dataset.default_graph.value(
            version, PROV.generatedAtTime,
        )


def test_export_reports_invalid_context_without_touching_sqlite(tmp_path):
    repository = make_repository(tmp_path / "repo.sqlite")
    repository.import_resource(*make_record())
    before = repository.database_path.read_bytes()
    path = tmp_path / "empty-pins"
    manifest = write_context_snapshot(path, [])
    contexts = load_context_snapshot(path, manifest["manifest_sha256"])
    with pytest.raises(ValueError, match="JSON-LD validation failed.*version/1.*No pinned JSON-LD context"):
        build_dataset(repository, contexts=contexts)
    assert repository.database_path.read_bytes() == before


def test_blank_node_predicates_fail_explicitly_instead_of_disappearing(tmp_path, approved_contexts):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, _ = make_record()
    source = {**source, "_:predicate": {"@value": "generalized RDF term"}}
    repository.import_resource(*with_normalized(source))
    with pytest.raises(ValueError, match="Non-IRI RDF predicate"):
        build_dataset(repository, contexts=approved_contexts)


def test_load_and_save_failures_protect_existing_files(tmp_path):
    dataset = Dataset(default_union=False)
    existing = tmp_path / "existing.trig"
    existing.write_text("existing artifact")
    with pytest.raises(FileExistsError, match="already exists"):
        save_dataset(dataset, existing, format="trig")
    assert existing.read_text() == "existing artifact"
    with pytest.raises(ValueError, match="Invalid trig dataset"):
        load_dataset(existing)
    malformed_nq = tmp_path / "bad.nq"
    malformed_nq.write_text("<urn:subject> invalid")
    with pytest.raises(ValueError, match="Invalid nquads dataset"):
        load_dataset(malformed_nq)
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "missing.nq")
    with pytest.raises(ValueError, match="local .trig or .nq"):
        load_dataset(tmp_path / "data.jsonld")
    with pytest.raises(ValueError, match="Use .trig"):
        save_dataset(dataset, tmp_path / "wrong.nq", format="trig")
    assert not (tmp_path / "wrong.nq").exists()


def test_export_does_not_create_a_missing_database(tmp_path, approved_contexts):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        build_dataset(Repository(path), contexts=approved_contexts)
    assert not path.exists()
