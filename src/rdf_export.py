"""Build and serialize RDF datasets from read-only SQLite repository snapshots."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import rdflib
import requests
from pyld import jsonld
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, XSD

from src.context_snapshot import ContextCapture, ContextSnapshot, write_context_snapshot
from src.jsonld_validation import OFFICIAL_AMB_CONTEXT_URL, jsonld_processing_options, validate_jsonld_document
from src.repository import Repository, observation_time

FORMATS = {"trig": ".trig", "nquads": ".nq"}
_PARSE_LOCK = Lock()


def _reference(value: str, blank_nodes: dict[str, BNode]) -> URIRef | BNode:
    if value.startswith("_:"):
        if value not in blank_nodes:
            blank_nodes[value] = BNode()
        return blank_nodes[value]
    return URIRef(value)


def _object(term: dict[str, str], blank_nodes: dict[str, BNode]) -> URIRef | BNode | Literal:
    if term["type"] != "literal":
        return _reference(term["value"], blank_nodes)
    if language := term.get("language"):
        return Literal(term["value"], lang=language, normalize=False)
    return Literal(term["value"], datatype=URIRef(term["datatype"]), normalize=False)


def _source_rdf(source: dict[str, Any], source_id: str, label: str, loader: Any) -> dict[str, Any]:
    validated = validate_jsonld_document(
        source, expected_id=source_id, document_loader=loader, isolate_contexts=True,
    )
    if not validated["valid"]:
        details = validated["errors"] or validated["context_resolution_errors"]
        raise ValueError(f"JSON-LD validation failed for {label}: {details}")

    failure_count = len(loader.failures)
    try:
        return jsonld.to_rdf(
            source,
            options={
                **jsonld_processing_options(loader),
                "rdfDirection": "i18n-datatype",
                # Detect unsupported generalized predicates instead of discarding them.
                "produceGeneralizedRdf": True,
            },
        )
    except jsonld.JsonLdError as exc:
        details = loader.failures[failure_count:] or str(exc)
        raise ValueError(f"JSON-LD to RDF conversion failed for {label}: {details}") from exc


def acquire_contexts(
    repository: Repository, directory: Path | str, identifier: str | None = None,
) -> dict[str, Any]:
    """Capture the contexts requested by validation and conversion of the selected SQLite snapshot."""
    destination = Path(directory)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Context snapshot destination already exists: {destination}")
    snapshot = repository.export_snapshot(identifier)
    with requests.Session() as session:
        capture = ContextCapture(session)
        if not snapshot["resources"]:
            probe = {
                "@context": OFFICIAL_AMB_CONTEXT_URL,
                "@id": "urn:oersi:context-probe",
                "@type": "urn:oersi:ContextProbe",
            }
            _source_rdf(probe, probe["@id"], OFFICIAL_AMB_CONTEXT_URL, capture)
        for resource in snapshot["resources"]:
            for version in resource["versions"]:
                _source_rdf(version["original_jsonld"], resource["source_iri"], version["version_iri"], capture)
    return write_context_snapshot(destination, capture.documents.values())


def _add_source_graphs(
    dataset: Dataset, repo: Namespace, version: dict[str, Any], source_id: str, loader: Any,
) -> None:
    version_iri = URIRef(version["version_iri"])
    graph_iri = URIRef(version["graph_iri"])
    graphs = _source_rdf(version["original_jsonld"], source_id, str(version_iri), loader)

    # Labels from a single JSON-LD conversion are shared even across its graphs.
    # A new map for each version keeps independent documents' blank nodes apart.
    blank_nodes: dict[str, BNode] = {}
    default = dataset.default_graph
    primary = dataset.graph(graph_iri)
    for name, triples in graphs.items():
        if name == "@default":
            target = primary
        else:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
            scoped = URIRef(f"{graph_iri}/source-graph/{digest}")
            default.add((version_iri, repo.sourceGraph, scoped))
            default.add((scoped, repo.sourceGraphName, _reference(name, blank_nodes)))
            target = dataset.graph(scoped)
        for triple in triples:
            predicate = triple["predicate"]
            if predicate["type"] != "IRI":
                raise ValueError(f"Non-IRI RDF predicate in {version_iri}; generalized RDF cannot be exported")
            target.add((
                _reference(triple["subject"]["value"], blank_nodes),
                URIRef(predicate["value"]),
                _object(triple["object"], blank_nodes),
            ))


def _timestamp(value: str) -> Literal:
    return Literal(observation_time(value), datatype=XSD.dateTime, normalize=False)


def _add_resource(
    dataset: Dataset,
    repo: Namespace,
    containers: dict[str, URIRef],
    classifications: dict[str, URIRef],
    row: dict[str, Any],
    loader: Any,
) -> None:
    default = dataset.default_graph
    resource = URIRef(row["resource_iri"])
    source_id = row["source_iri"]
    if not urlsplit(source_id).scheme or any(char.isspace() for char in source_id):
        raise ValueError(f"Resource {resource} has a source identifier that cannot be represented as an absolute IRI")
    current = URIRef(row["current_version_iri"])
    default.add((resource, RDF.type, repo.Resource))
    default.add((resource, DCTERMS.identifier, Literal(row["resource_id"])))
    default.add((resource, repo.currentVersion, current))
    if row["container_id"] is not None:
        default.add((resource, DCTERMS.isPartOf, containers[row["container_id"]]))
    if row["title"] is not None:
        default.add((resource, DCTERMS.title, Literal(row["title"])))

    for version in row["versions"]:
        snapshot = URIRef(version["version_iri"])
        classification = version["normalized_metadata"]["classification"]
        statements = (
            (resource, DCTERMS.hasVersion, snapshot),
            (snapshot, RDF.type, repo.Version),
            (snapshot, DCTERMS.isVersionOf, resource),
            (snapshot, DCTERMS.source, URIRef(source_id)),
            (snapshot, PROV.generatedAtTime, _timestamp(version["created_at"])),
            (snapshot, repo.observedAt, _timestamp(version["observed_at"])),
            (snapshot, repo.graphIRI, URIRef(version["graph_iri"])),
            (snapshot, repo.classification, classifications[classification["container"]]),
            (snapshot, repo.classificationReason, Literal(classification["reason"])),
            (snapshot, repo.metadataHash, Literal(version["metadata_hash"])),
            (snapshot, repo.versionNumber, Literal(version["version_number"])),
            (snapshot, repo.isCurrent, Literal(snapshot == current)),
        )
        for statement in statements:
            default.add(statement)
        if version["source_modified_at"] is not None:
            default.add((snapshot, repo.sourceModifiedAt, _timestamp(version["source_modified_at"])))
        if version["previous_version_iri"] is not None:
            default.add((snapshot, PROV.wasRevisionOf, URIRef(version["previous_version_iri"])))
        _add_source_graphs(dataset, repo, version, source_id, loader)


def build_dataset(repository: Repository, identifier: str | None = None, *, contexts: ContextSnapshot) -> Dataset:
    """Materialize per-version source graphs using only an approved snapshot."""
    return dataset_from_snapshot(
        repository.export_snapshot(identifier), base_iri=repository.base_iri,
        vocabulary_iri=repository.vocabulary_iri, contexts=contexts,
    )


def dataset_from_snapshot(
    snapshot: dict[str, Any], *, base_iri: str, vocabulary_iri: str, contexts: ContextSnapshot,
) -> Dataset:
    """Use an already-read snapshot for export and validation of the same rows."""
    loader = contexts.loader()
    dataset = Dataset(default_union=False)
    repo = Namespace(vocabulary_iri)
    for prefix, namespace in (
        ("repo", repo), ("rdf", RDF), ("rdfs", RDFS), ("dcterms", DCTERMS), ("prov", PROV), ("xsd", XSD),
    ):
        dataset.bind(prefix, namespace, replace=True)
    default = dataset.default_graph
    containers = {}
    classifications = {}
    for row in snapshot["containers"]:
        category = row["container_id"]
        container = containers[category] = URIRef(row["container_iri"])
        default.add((container, RDF.type, repo.Container))
        default.add((container, DCTERMS.identifier, Literal(category)))
        default.add((container, RDFS.label, Literal(row["name"])))
        default.add((container, DCTERMS.description, Literal(row["description"])))
        default.add((container, DCTERMS.created, _timestamp(row["created_at"])))
    labels = {row["container_id"]: row["name"] for row in snapshot["containers"]}
    labels["unclassified"] = "Unclassified"
    for category, label in labels.items():
        classification = classifications[category] = URIRef(f"{base_iri}/classification/{category}")
        default.add((classification, RDF.type, repo.Classification))
        default.add((classification, DCTERMS.identifier, Literal(category)))
        default.add((classification, RDFS.label, Literal(label)))
    for row in snapshot["resources"]:
        _add_resource(dataset, repo, containers, classifications, row, loader)
    return dataset


def _named_graphs(dataset: Dataset) -> list[Graph]:
    return sorted(
        (graph for graph in dataset.graphs() if graph.identifier != dataset.default_graph.identifier),
        key=lambda graph: str(graph.identifier),
    )


def dataset_summary(dataset: Dataset) -> dict[str, int]:
    graphs = _named_graphs(dataset)
    default_count = len(dataset.default_graph)
    return {
        "default_graph_triples": default_count,
        "named_graphs": len(graphs),
        "total_quads": default_count + sum(len(graph) for graph in graphs),
    }


def save_dataset(dataset: Dataset, output: Path | str, *, format: str) -> None:
    """Write a complete dataset file without replacing an existing path."""
    path = Path(output)
    if format not in FORMATS or path.suffix.lower() != FORMATS[format]:
        raise ValueError("Use .trig for TriG or .nq for N-Quads output")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"RDF output already exists: {path}")

    if format == "trig":
        # Explicit triples prevent compact Turtle syntax from changing lexical
        # forms or introducing anonymous nodes shared by multiple graphs.
        sections = [dataset.default_graph.serialize(format="nt", encoding="utf-8")]
        for graph in _named_graphs(dataset):
            sections.extend((
                f"{graph.identifier.n3()} {{\n".encode("utf-8"),
                graph.serialize(format="nt", encoding="utf-8"),
                b"}\n",
            ))
        data = b"".join(sections)
    else:
        data = dataset.serialize(format="nquads", encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".oersi-rdf-", dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_bytes(data)
        os.link(temporary, path)


def load_dataset(input_path: Path | str) -> Dataset:
    """Parse an explicitly local TriG or N-Quads file without dataset union semantics."""
    path = Path(input_path)
    formats = {extension: name for name, extension in FORMATS.items()}
    format = formats.get(path.suffix.lower())
    if format is None:
        raise ValueError("RDF input must be a local .trig or .nq file")
    data = path.read_bytes()
    try:
        return parse_dataset(data, format=format, publicID=path.resolve().as_uri())
    except Exception as exc:
        raise ValueError(f"Invalid {format} dataset {path}: {exc}") from exc


def parse_dataset(data: bytes | str, *, format: str, **options: Any) -> Dataset:
    """Parse local dataset syntax while retaining literal lexical forms."""
    if format not in FORMATS:
        raise ValueError("Only TriG and N-Quads dataset syntax is supported")
    dataset = Dataset(default_union=False)
    # RDFLib exposes its typed-literal normalization switch at module scope.
    # Preserve lexical forms written by the exporter and restore the caller's setting.
    with _PARSE_LOCK:
        previous = rdflib.NORMALIZE_LITERALS
        rdflib.NORMALIZE_LITERALS = False
        try:
            dataset.parse(data=data, format=format, **options)
        finally:
            rdflib.NORMALIZE_LITERALS = previous
    return dataset
