"""Service layer for the local Streamlit repository workbench."""

from __future__ import annotations

import csv
import io
import os
import pickle
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from types import SimpleNamespace

from rdflib import BNode, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF

from src.context_snapshot import load_context_snapshot
from src.rdf_cli import _query_bindings
from src.rdf_export import dataset_summary, dataset_from_snapshot
from src.repository import Repository, SCHEMA_VERSION
from src.repository_schema import check_integrity, has_current_version_guard
from src.semantic_validation import ROOT
from src.workbench_worker import (
    MAX_BYTES, MAX_GRAPHS, MAX_QUERY_BYTES, MAX_SHAPES_BYTES, enforce_size, write_worker_message,
)

WORKER_TIMEOUT = 16
SAMPLE_SIZE = 30
REPORT_LIMIT = 8


@dataclass(frozen=True)
class Workbench:
    database: Path
    base_iri: str
    vocabulary_iri: str | None = None
    context_snapshot: Path | None = None
    context_sha256: str | None = None
    backup_dir: Path | None = None

    @classmethod
    def configured(cls):
        return cls(
            Path(os.getenv("OERSI_REPOSITORY_DATABASE", "data/repository.sqlite3")),
            os.getenv("OERSI_REPOSITORY_BASE_IRI", "https://example.org/oersi/"),
            os.getenv("OERSI_REPOSITORY_VOCABULARY_IRI"),
            Path(value) if (value := os.getenv("OERSI_RDF_CONTEXT_SNAPSHOT")) else None,
            os.getenv("OERSI_RDF_CONTEXT_SHA256"),
            Path(value) if (value := os.getenv("OERSI_WORKBENCH_BACKUP_DIR")) else None,
        )

    def contexts(self):
        if self.context_snapshot is None or not self.context_sha256:
            raise ValueError("Configure a local context snapshot and its approved SHA-256 in Settings")
        return load_context_snapshot(self.context_snapshot, self.context_sha256)

    def namespaces(self):
        repository = Repository(self.database, self.base_iri, vocabulary_iri=self.vocabulary_iri)
        return repository.base_iri, repository.vocabulary_iri

    def repository(self, *, pinned=False):
        if not self.database.is_file():
            raise FileNotFoundError(f"Configured SQLite repository does not exist: {self.database}")
        contexts = self.contexts() if pinned else None
        return Repository(
            self.database, self.base_iri, vocabulary_iri=self.vocabulary_iri,
            context_loader=contexts.loader() if contexts else None,
        )

    def dataset(self, identifier=None, *, with_snapshot=False):
        contexts = self.contexts()
        repository = self.repository()
        if identifier is None and repository.stats()["resource_versions"] > MAX_GRAPHS:
            raise ValueError("The full repository exceeds the graph limit. Select one resource by ID or IRI.")
        snapshot = repository.export_snapshot(identifier)
        dataset = dataset_from_snapshot(
            snapshot, base_iri=repository.base_iri, vocabulary_iri=repository.vocabulary_iri, contexts=contexts,
        )
        enforce_size(dataset)
        return (dataset, snapshot) if with_snapshot else dataset

    def resources(self):
        repository = self.repository()
        with repository._connection() as connection:
            repository._ensure_initialized(connection)
            return [
                {**dict(row), "classification": row["container_id"] or "unclassified"}
                for row in connection.execute("SELECT * FROM resources ORDER BY container_id IS NULL, container_id, resource_id")
            ]

    def migration_preview(self):
        repository = self.repository()
        with repository._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schema = None
            if "repository_config" in tables:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(repository_config)")}
                if {"config_key", "config_value"} <= columns:
                    schema = connection.execute(
                        "SELECT config_value FROM repository_config WHERE config_key = 'schema_version'"
                    ).fetchone()
            version = schema[0] if schema else None
            preview = {"schema_version": version, "target_version": SCHEMA_VERSION, "supported": False,
                       "status": "Unsupported repository schema", "changes": []}
            try:
                repository._ensure_initialized(connection, migrating=True)
                check_integrity(connection, repository.base_iri)
            except (RuntimeError, ValueError) as exc:
                return {**preview, "status": str(exc)}
            if version == SCHEMA_VERSION and has_current_version_guard(connection):
                return {**preview, "status": "Reviewed schema, migration unnecessary"}
            changes = {
                "2": ["Add snapshot observation and source-modification provenance",
                      "Preserve existing snapshot identifiers and pending records"],
                SCHEMA_VERSION: ["Upgrade the earlier Phase 3 draft constraints"],
            }[version]
            return {**preview, "supported": True, "status": "Supported migration",
                    "changes": [*changes, "Install current-version and review-state integrity guards",
                                "Validate repository integrity before committing"]}

    def backup_path(self, name):
        if self.backup_dir is None or not self.backup_dir.is_dir():
            raise ValueError("Configure an existing backup directory before migrating")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.sqlite3", name):
            raise ValueError("Enter a new .sqlite3 backup filename using letters, digits, dots, dashes or underscores")
        path = self.backup_dir.resolve() / name
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Backup already exists: {path}")
        return path

    def migrate(self, name, *, confirmed=False):
        if not confirmed:
            raise ValueError("Explicit migration confirmation is required")
        preview = self.migration_preview()
        if not preview["supported"]:
            raise ValueError(preview["status"])
        backup = self.backup_path(name)
        self.repository().migrate(backup)
        return {"backup": str(backup), **self.migration_preview()}

    def decide(self, update_id, decision, *, confirmed=False):
        if not confirmed:
            raise ValueError("Explicit review confirmation is required")
        if decision == "Accept":
            return self.repository(pinned=True).accept_update(update_id)
        if decision == "Reject":
            return self.repository().reject_update(update_id)
        raise ValueError("Choose Accept or Reject")


def _worker(action, *args):
    # Only application-created messages cross these private subprocess pipes.
    request = io.BytesIO()
    write_worker_message(request, (action, args))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.workbench_worker"], cwd=ROOT, input=request.getvalue(),
            capture_output=True, timeout=WORKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"RDF operation exceeded {WORKER_TIMEOUT} seconds") from exc
    except OSError as exc:
        raise ValueError("Could not start the RDF worker") from exc
    if proc.returncode:
        raise ValueError("RDF worker stopped. The operation may exceed the resource limit.")
    try:
        response = pickle.loads(proc.stdout)
    except (EOFError, pickle.UnpicklingError) as exc:
        raise ValueError("RDF worker returned an invalid response") from exc
    if not isinstance(response, tuple) or len(response) != 2 or type(response[0]) is not bool:
        raise ValueError("RDF worker returned an invalid response")
    ok, result = response
    if not ok:
        raise ValueError(result)
    return result


def parse_upload(name, data):
    format = {".trig": "trig", ".nq": "nquads", ".nquads": "nquads"}.get(Path(name).suffix.lower())
    if not format:
        raise ValueError("Supported dataset formats are TriG (.trig) and N-Quads (.nq or .nquads)")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Upload a nonempty RDF dataset up to 4 MiB")
    return format, _worker("parse", data, format)


def graph_choices(dataset):
    return {("Default graph" if graph.identifier == dataset.default_graph.identifier else graph.identifier.n3()):
            graph.identifier for graph in sorted(dataset.graphs(), key=lambda graph: str(graph.identifier))}


def inspect_dataset(dataset, identifier=None):
    graph = dataset.default_graph if identifier is None else dataset.graph(identifier)
    terms = Counter(graph.predicates())
    classes = Counter(obj for obj in graph.objects(None, RDF.type) if isinstance(obj, URIRef))
    namespaces = {}
    for term in {*terms, *classes}:
        namespace = str(term).rsplit("#", 1)[0] + "#" if "#" in str(term) else str(term).rpartition("/")[0] + "/"
        if namespace != "/":
            namespaces[namespace] = ""
    namespaces.update({str(iri): prefix for prefix, iri in graph.namespaces() if str(iri) in namespaces})
    return {
        "summary": dataset_summary(dataset),
        "graphs": [{"Graph": label, "Triples": len(dataset.graph(iri))} for label, iri in graph_choices(dataset).items()],
        "namespaces": [{"Prefix": prefix, "Namespace": iri} for iri, prefix in sorted(namespaces.items())],
        "classes": [{"Class": str(term), "Statements": count} for term, count in classes.most_common(100)],
        "properties": [{"Property": str(term), "Statements": count} for term, count in terms.most_common(100)],
        "resources": sorted({str(subject) for subject in graph.subjects() if isinstance(subject, URIRef)})[:100],
        "statements": [{"Subject": s.n3(), "Predicate": p.n3(), "Object": o.n3()}
                       for s, p, o in islice(graph, SAMPLE_SIZE)],
    }


def general_validation(dataset, identifier=None, *, shapes=None):
    if shapes is None:
        return {"syntax": "Passed", "dataset": dataset_summary(dataset), "shacl": {"status": "Not evaluated"}}, None
    if not shapes or len(shapes) > MAX_SHAPES_BYTES:
        raise ValueError("The Turtle shapes file must be nonempty and at most 256 KiB")
    report, turtle = _worker("shapes", dataset, identifier, shapes)
    return {"syntax": "Passed", "dataset": dataset_summary(dataset),
            "shacl": {"status": "Passed" if report["conforms"] else "Failed", **report}}, turtle


def repository_validation(dataset, settings, *, against_sqlite=False, identifier=None, supplied_snapshot=None):
    base, vocabulary = settings.namespaces()
    repo = Namespace(vocabulary)
    default = dataset.default_graph
    recognized = any(
        subject.startswith(f"{base}/container/") if isinstance(subject, URIRef) else False
        for subject in default.subjects(RDF.type, repo.Container)
    ) or any(
        any(default.subjects(RDF.type, kind)) for kind in (repo.Resource, repo.Version, repo.Classification)
    )
    if not recognized:
        return {
            "recognized": False, "conforms": False, "diagnostic": "Dataset has no repository entities in the configured default-graph vocabulary",
            "shacl": {"status": "Not evaluated"}, "integrity": {"status": "Not evaluated"},
            "fidelity": {"status": "Not evaluated"}, "sqlite_completeness": {"status": "Not evaluated"},
        }, None
    data = contexts = None
    unavailable = None
    if against_sqlite:
        try:
            contexts = settings.contexts()
            data = supplied_snapshot if supplied_snapshot is not None else settings.repository().export_snapshot(identifier)
        except (OSError, ValueError, LookupError, RuntimeError) as exc:
            unavailable, contexts = str(exc), None
    report, rdf = _worker("repository", dataset, base, vocabulary, data, contexts)
    report["recognized"] = True
    report["scope"] = "SQLite-backed validation" if data is not None else "RDF structural conformance"
    report["shacl"]["status"] = "Passed" if report["shacl"]["conforms"] else "Failed"
    report["integrity"]["status"] = "Passed" if report["integrity"]["conforms"] else "Failed"
    report["fidelity"]["status"] = ("Passed" if report["fidelity"]["conforms"] else "Failed") if data is not None else "Not evaluated"
    report["sqlite_completeness"] = (
        {"status": "Failed" if any(row["code"].startswith("sqlite_") for row in report["integrity"]["violations"])
         else "Passed"}
        if data is not None else {"status": "Not evaluated"}
    )
    if unavailable:
        report["fidelity"]["reason"] = unavailable
        report["sqlite_completeness"]["reason"] = unavailable
    return report, rdf


def run_query(dataset, source, vocabulary, *, iri="", integer="", time=""):
    if not source.strip() or len(source.encode()) > MAX_QUERY_BYTES:
        raise ValueError("Enter a SPARQL query of up to 32 KiB")
    if sum(len(text) for text in (iri, integer, time)) > MAX_QUERY_BYTES:
        raise ValueError("Variable bindings exceed the 32 KiB limit")
    bindings = _query_bindings(SimpleNamespace(
        bind_iri=iri.split(), bind_integer=integer.split(), bind_time=time.split(),
    ))
    return _worker("query", dataset, source, vocabulary, bindings)


def query_table(result):
    if result["kind"] == "ASK":
        return ["answer"], [{"answer": str(result["result"]).lower()}]
    cols = result["result"]["head"]["vars"]
    rows = result["result"]["results"]["bindings"]
    return cols, [
        {name: term["value"] + (f'@{term["xml:lang"]}' if "xml:lang" in term else
                                f'^^<{term["datatype"]}>' if "datatype" in term else "")
         for name, term in row.items()}
        for row in rows
    ]


def as_csv(columns, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    # Avoid evaluating untrusted cells as spreadsheet formulas.
    writer.writerows({key: "'" + value if value.lstrip().startswith(("=", "+", "-", "@"))
                      or value.startswith(("\t", "\r")) else value
                      for key, value in row.items()} for row in rows)
    return buffer.getvalue()


def mapping_preview(dataset):
    schema = Namespace("http://schema.org/")
    schema_https = Namespace("https://schema.org/")
    known = {
        DCTERMS.title: "Potential title", schema.name: "Potential title", schema_https.name: "Potential title",
        schema.description: "Potential description", schema_https.description: "Potential description",
        DCTERMS.source: "Candidate source identity, requires review",
        schema.learningResourceType: "Candidate classification evidence, requires review",
        schema_https.learningResourceType: "Candidate classification evidence, requires review",
    }
    rows = Counter((pred, scope, obj if pred == RDF.type else None) for _, pred, obj, scope in dataset.quads())
    resource_types = {schema.LearningResource, schema_https.LearningResource, schema.Course, schema_https.Course}
    return [
        {"Graph": str(scope), "Predicate": str(pred), "Input class": str(class_) if class_ is not None else "",
         "Statements": count,
         "Potential field": (
             "Candidate learning resource class, requires review" if class_ in resource_types
             else known.get(pred, "Unmapped")
         )}
        for (pred, scope, class_), count in rows.most_common(150)
    ]


def neighborhood(graph, center, *, depth=2, node_limit=35, edge_limit=75):
    levels = {center: 0}
    edges = set()
    frontier = [center]
    for node in frontier:
        for triple in (*islice(graph.triples((node, None, None)), edge_limit),
                       *islice(graph.triples((None, None, node)), edge_limit)):
            subject, _, obj = triple
            if isinstance(obj, Literal):
                continue
            other = obj if subject == node else subject
            if not isinstance(other, (URIRef, BNode)):
                continue
            if other not in levels:
                if len(levels) == node_limit:
                    continue
                levels[other] = levels[node] + 1
                if levels[other] < depth:
                    frontier.append(other)
            edges.add(triple)
            if len(edges) >= edge_limit:
                return levels, sorted(edges, key=lambda t: tuple(map(str, t)))
    return levels, sorted(edges, key=lambda t: tuple(map(str, t)))
