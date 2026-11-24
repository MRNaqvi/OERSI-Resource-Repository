from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from copy import deepcopy

import pytest
import requests
from pyld import jsonld
from rdflib import BNode, Literal, Namespace, URIRef
from rdflib.compare import isomorphic

import src.rdf_cli as cli
from src.context_snapshot import (
    ContextCapture,
    ContextDocument,
    inspect_context_snapshot,
    load_context_snapshot,
    write_context_snapshot,
)
from src.jsonld_validation import OFFICIAL_AMB_CONTEXT_URL
from src.rdf_export import acquire_contexts, build_dataset, load_dataset, save_dataset
from src.repository import Repository
from tests.jsonld_fixtures import AMB_CONTEXT_DOCUMENT
from tests.test_rdf_export import _dataset_as_graph
from tests.test_repository import make_record, make_repository
from tests.test_version_management import when, with_normalized

AMB = OFFICIAL_AMB_CONTEXT_URL
REDIRECTED_AMB = "https://contexts.example/releases/1/amb.jsonld"
MAPPING = "https://contexts.example/releases/1/support/mapping.jsonld"
ADDITIONS = "https://contexts.example/releases/1/import/additions.jsonld"
SCOPED = "https://contexts.example/releases/1/support/scoped/terms.jsonld"
EXTRA = "https://provider.example/context"
EX = Namespace("https://example.edu/vocab/")


class ContextResponse:
    def __init__(self, url, content):
        self.url = url
        self.content = content

    def raise_for_status(self):
        pass


class OfflineHttpFixture:
    def __init__(self, documents):
        self.documents = deepcopy(documents)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.documents[url]


def response(url, context):
    return ContextResponse(url, (json.dumps({"@context": context}, indent=2) + "\n").encode())


@pytest.fixture
def redirected_contexts(monkeypatch):
    session = OfflineHttpFixture({
        AMB: response(REDIRECTED_AMB, ["./support/mapping.jsonld", {"@import": "./import/additions.jsonld"}]),
        MAPPING: response(MAPPING, {
            **AMB_CONTEXT_DOCUMENT["@context"],
            "scope": {"@id": str(EX.scope), "@context": "./scoped/terms.jsonld"},
        }),
        ADDITIONS: response(ADDITIONS, {"termFromImport": str(EX.annotation)}),
        SCOPED: response(SCOPED, {"scopedName": str(EX.scopedName)}),
        EXTRA: response("https://provider.example/releases/context.jsonld", {
            "link": {"@id": str(EX.link), "@type": "@id"},
            "ref": str(EX.ref), "group": str(EX.group),
        }),
    })
    monkeypatch.setattr(requests, "Session", lambda: session)
    return session


@pytest.fixture
def course_history(tmp_path, redirected_contexts):
    repository = Repository(tmp_path / "repo.sqlite", context_loader=ContextCapture(redirected_contexts))
    repository.initialize()
    original, _ = make_record()
    original["@context"] = [AMB, EXTRA, {"@base": "https://example.edu/source/", "@language": "en"}]
    original["termFromImport"] = "captured import"
    original["link"] = "./asset"
    original["scope"] = {"scopedName": "nested context"}
    original["ref"] = {"@id": "_:shared"}
    original["group"] = {
        "@id": "./nested",
        "@graph": [{"@id": "_:shared", "termFromImport": "nested graph"}],
    }
    source, normalized = with_normalized(original)
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="revised"), observed_at=when(2))
    redirected_contexts.calls.clear()
    return repository, source, normalized["stable_id"]


def test_acquisition_captures_requested_and_redirect_urls_and_relative_contexts(
    tmp_path, course_history, redirected_contexts,
):
    repository, source, identifier = course_history
    path = tmp_path / "candidate"
    manifest = acquire_contexts(repository, path)
    assert Counter(redirected_contexts.calls) == {url: 1 for url in redirected_contexts.documents}
    assert manifest == inspect_context_snapshot(path)
    assert manifest["manifest_sha256"] == hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
    for entry in manifest["contexts"]:
        served = redirected_contexts.documents[entry["url"]]
        assert entry["document_url"] == served.url
        assert (path / "documents" / f"{entry['sha256']}.jsonld").read_bytes() == served.content

    approved = load_context_snapshot(path, manifest["manifest_sha256"])
    dataset = build_dataset(repository, contexts=approved)
    ns = Namespace(repository.vocabulary_iri)
    v1 = repository.get_version(identifier, 1)
    g1 = dataset.graph(URIRef(v1["graph_iri"]))
    root = URIRef(source["id"])
    assert (root, EX.annotation, Literal("captured import", lang="en")) in g1
    assert (root, EX.link, URIRef("https://example.edu/source/asset")) in g1
    scoped = g1.value(root, EX.scope)
    assert isinstance(scoped, BNode)
    assert (scoped, EX.scopedName, Literal("nested context", lang="en")) in g1
    nested_graph, = dataset.default_graph.objects(URIRef(v1["version_iri"]), ns.sourceGraph)
    assert dataset.default_graph.value(nested_graph, ns.sourceGraphName) == URIRef("https://example.edu/source/nested")
    shared = g1.value(root, EX.ref)
    assert (shared, EX.annotation, Literal("nested graph", lang="en")) in dataset.graph(nested_graph)
    other_graph = dataset.graph(URIRef(repository.get_version(identifier, 2)["graph_iri"]))
    assert shared != other_graph.value(root, EX.ref)


def test_repeated_exports_are_offline_and_equivalent_with_approved_pins(
    tmp_path, course_history, redirected_contexts, monkeypatch,
):
    repository, _, _ = course_history
    path = tmp_path / "candidate"
    approved = acquire_contexts(repository, path)["manifest_sha256"]
    before = repository.database_path.read_bytes()
    stored = repository.export_snapshot()

    def offline(*args, **kwargs):
        pytest.fail("RDF export must not initialize an HTTP session or use the ingestion loader")

    monkeypatch.setattr(requests, "Session", offline)
    repository.context_loader = offline
    first = build_dataset(repository, contexts=load_context_snapshot(path, approved))
    second = build_dataset(repository, contexts=load_context_snapshot(path, approved))
    reference = _dataset_as_graph(first)
    assert isomorphic(reference, _dataset_as_graph(second))
    for format, ext in (("trig", "trig"), ("nquads", "nq")):
        file = tmp_path / f"data.{ext}"
        save_dataset(second, file, format=format)
        assert isomorphic(reference, _dataset_as_graph(load_dataset(file)))
    assert repository.database_path.read_bytes() == before
    assert repository.export_snapshot() == stored


def test_remote_revisions_require_explicit_new_path_and_changed_fingerprint(
    tmp_path, course_history, redirected_contexts,
):
    repository, source, identifier = course_history
    path = tmp_path / "first"
    manifest = acquire_contexts(repository, path)
    approved = manifest["manifest_sha256"]
    original_manifest = (path / "manifest.json").read_bytes()
    redirected_contexts.documents[ADDITIONS] = response(ADDITIONS, {"termFromImport": str(EX.different)})
    redirected_contexts.calls.clear()
    with pytest.raises(FileExistsError, match="already exists"):
        acquire_contexts(repository, path)
    assert (path / "manifest.json").read_bytes() == original_manifest
    original = build_dataset(repository, contexts=load_context_snapshot(path, approved))
    assert not redirected_contexts.calls
    graph = URIRef(repository.get_version(identifier, 1)["graph_iri"])
    subject = URIRef(source["id"])
    assert original.graph(graph).value(subject, EX.annotation) == Literal("captured import", lang="en")
    assert original.graph(graph).value(subject, EX.different) is None

    next_path = tmp_path / "next"
    updated = acquire_contexts(repository, next_path)
    assert updated["manifest_sha256"] != approved
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        load_context_snapshot(next_path, approved)
    alternate = build_dataset(
        repository, contexts=load_context_snapshot(next_path, updated["manifest_sha256"]),
    )
    assert alternate.graph(graph).value(subject, EX.annotation) is None
    assert alternate.graph(graph).value(subject, EX.different) == Literal("captured import", lang="en")
    again = build_dataset(repository, contexts=load_context_snapshot(path, approved))
    assert isomorphic(_dataset_as_graph(original), _dataset_as_graph(again))


@pytest.mark.parametrize("missing_url", [AMB, ADDITIONS, SCOPED, EXTRA])
def test_missing_direct_and_nested_pins_fail_without_fetching(
    tmp_path, course_history, redirected_contexts, missing_url,
):
    repository, _, _ = course_history
    path = tmp_path / "partial"
    documents = [
        ContextDocument(url, served.url, served.content)
        for url, served in redirected_contexts.documents.items() if url != missing_url
    ]
    candidate = write_context_snapshot(path, documents)
    before = repository.database_path.read_bytes()
    with pytest.raises(ValueError, match="No pinned JSON-LD context") as error:
        build_dataset(repository, contexts=load_context_snapshot(path, candidate["manifest_sha256"]))
    assert missing_url in str(error.value)
    assert not redirected_contexts.calls
    assert repository.database_path.read_bytes() == before


def test_pin_and_manifest_integrity_are_verified_on_load(tmp_path, approved_contexts):
    path = tmp_path / "amb-snapshot"
    candidate = inspect_context_snapshot(path)
    pin = candidate["contexts"][0]
    body = path / "documents" / f"{pin['sha256']}.jsonld"
    original = body.read_bytes()
    body.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="context SHA-256 mismatch"):
        load_context_snapshot(path, approved_contexts.sha256)
    body.unlink()
    with pytest.raises(ValueError, match="Cannot read pinned JSON-LD context"):
        load_context_snapshot(path, approved_contexts.sha256)
    body.write_bytes(original)

    manifest = path / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        load_context_snapshot(path, approved_contexts.sha256)


def test_shared_pyld_cache_cannot_satisfy_a_missing_pin(tmp_path, monkeypatch):
    repository = make_repository(tmp_path / "repo.sqlite")
    source, normalized = make_record()
    repository.import_resource(source, normalized)
    shared = {}
    monkeypatch.setattr(jsonld, "_resolved_context_cache", shared)

    def previously_loaded(url, options):
        assert url == AMB
        return {
            "document": deepcopy(AMB_CONTEXT_DOCUMENT), "documentUrl": url,
            "contextUrl": None, "tag": "static",
        }

    jsonld.expand(source, options={"documentLoader": previously_loaded})
    assert AMB in shared
    path = tmp_path / "missing-amb"
    manifest = write_context_snapshot(path, [])
    pins = load_context_snapshot(path, manifest["manifest_sha256"])
    with pytest.raises(ValueError, match="No pinned JSON-LD context"):
        build_dataset(repository, contexts=pins)


def test_cli_requires_reviewed_checksum_and_reports_it(course_history, tmp_path, monkeypatch, capsys):
    repository, _, _ = course_history
    monkeypatch.delenv("OERSI_RDF_CONTEXT_SNAPSHOT", raising=False)
    monkeypatch.delenv("OERSI_RDF_CONTEXT_SHA256", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "Repository", lambda *args, **kwargs: repository)

    def call(*arguments):
        monkeypatch.setattr(sys, "argv", ["rdf_cli", *arguments])
        code = cli.main()
        return code, json.loads(capsys.readouterr().out)

    path = tmp_path / "review"
    code, candidate = call("acquire-contexts", "--output", str(path))
    assert code == 0
    assert call("inspect-contexts", "--input", str(path)) == (0, candidate)
    target = tmp_path / "export.trig"
    flags = ["--context-snapshot", str(path)]
    export = ["export", "--format", "trig", "--output", str(target)]
    code, error = call(*flags, *export)
    assert code == 1 and "both --context-snapshot and --context-sha256" in error["error"]
    assert not target.exists()
    code, error = call(*flags, "--context-sha256", "0" * 64, *export)
    assert code == 1 and "manifest SHA-256 mismatch" in error["error"]
    assert not target.exists()
    code, exported = call(*flags, "--context-sha256", candidate["manifest_sha256"], *export)
    assert code == 0 and exported["context_snapshot_sha256"] == candidate["manifest_sha256"]
    assert target.is_file()


def test_acquire_can_capture_amb_for_an_empty_repository(tmp_path, redirected_contexts):
    repository = make_repository(tmp_path / "empty.sqlite")
    report = acquire_contexts(repository, tmp_path / "empty-candidate")
    assert {entry["url"] for entry in report["contexts"]} == {AMB, MAPPING, SCOPED, ADDITIONS}
