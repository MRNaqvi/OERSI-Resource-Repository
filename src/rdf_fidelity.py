"""Compare each export's RDF with an independently reconstructed source dataset."""

from __future__ import annotations

import hashlib

from pyld import jsonld
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.exceptions import ParserError
from rdflib.namespace import RDF, XSD

from src.jsonld_validation import jsonld_processing_options
from src.rdf_export import parse_dataset

CHECK = Namespace("urn:oersi:fidelity:")


def _rdf_term(term):
    # RDF 1.1 simple literals denote xsd:string, including the compact N-Quads form.
    if isinstance(term, Literal) and term.datatype is None and term.language is None:
        return Literal(str(term), datatype=XSD.string, normalize=False)
    if isinstance(term, Literal) and term.language and term.language != term.language.lower():
        return Literal(str(term), lang=term.language.lower(), normalize=False)
    return term


def _blank_statements(scopes) -> Graph:
    """Encode scope membership for statements that require blank-node matching."""
    encoded = Graph()
    for name, statements in scopes:
        scope = BNode()
        encoded.add((scope, RDF.type, CHECK.SourceDefault if name is None else CHECK.SourceNamed))
        if name is not None:
            encoded.add((scope, CHECK.name, name))
        for subject, predicate, obj in statements:
            if not any(isinstance(term, BNode) for term in (name, subject, obj)):
                continue
            item = BNode()
            encoded.add((scope, CHECK.statement, item))
            encoded.add((item, RDF.subject, subject))
            encoded.add((item, RDF.predicate, predicate))
            encoded.add((item, RDF.object, _rdf_term(obj)))
    return encoded


def _equivalent_scopes(expected, actual) -> bool:
    return _ground_quads(expected) == _ground_quads(actual) and isomorphic(
        _blank_statements(expected), _blank_statements(actual),
    )


def dataset_equivalent(left, right) -> bool:
    def scopes(dataset):
        yield None, dataset.default_graph
        for graph in dataset.graphs():
            if graph.identifier != dataset.default_graph.identifier and len(graph):
                yield graph.identifier, graph

    return _equivalent_scopes(list(scopes(left)), list(scopes(right)))


def _reference_scopes(source, loader):
    # Use PyLD's own N-Quads serializer/parser path rather than the Phase 4 term mapper.
    # A label table allows empty blank-named graphs to share their name with other triples.
    failure_count = len(loader.failures)
    try:
        raw = jsonld.to_rdf(
            source, options={
                **jsonld_processing_options(loader), "rdfDirection": "i18n-datatype",
                "produceGeneralizedRdf": True,
            },
        )
        if any(quad["predicate"]["type"] != "IRI" for quads in raw.values() for quad in quads):
            raise ValueError("The source requires generalized RDF predicates")
        named_scopes = {
            name: f"urn:oersi:reference:scope:{index}"
            for index, name in enumerate(sorted(raw.keys() - {"@default"}))
        }
        # The parser reserves an internal default-graph IRI. Parse named graphs
        # under temporary labels so that a source graph with that IRI stays named.
        parse_input = {named_scopes.get(name, name): quads for name, quads in raw.items()}
        blank_names = {name[2:]: BNode() for name in named_scopes if name.startswith("_:")}
        expected = parse_dataset(
            jsonld.JsonLdProcessor.to_nquads(parse_input), format="nquads", bnode_context=blank_names,
        )
    except jsonld.JsonLdError as exc:
        raise ValueError(loader.failures[failure_count:] or str(exc)) from exc
    scopes = [(None, expected.default_graph)]
    for name, temporary_name in named_scopes.items():
        term = blank_names[name[2:]] if name.startswith("_:") else URIRef(name)
        scopes.append((term, expected.get_context(URIRef(temporary_name))))
    return scopes, {name for name, quads in raw.items() if quads}


def _ground_quads(scopes):
    return {
        " ".join(item.n3() for item in (s, p, _rdf_term(o))) + (" " + name.n3() if name is not None else "") + " ."
        for name, graph in scopes if not isinstance(name, BNode)
        for s, p, o in graph if not isinstance(s, BNode) and not isinstance(o, BNode)
    }


def check_fidelity(dataset, snapshot, contexts, vocabulary_iri: str):
    repo = Namespace(vocabulary_iri)
    default = dataset.default_graph
    loader = contexts.loader()
    results = []
    required_graphs = set()
    scopes_seen = {}
    for row in snapshot["resources"]:
        for version in row["versions"]:
            iri = URIRef(version["version_iri"])
            primary = URIRef(version["graph_iri"])
            details = {"version_iri": str(iri), "resource_iri": row["resource_iri"], "primary_graph_iri": str(primary)}
            try:
                expected, nonempty = _reference_scopes(version["original_jsonld"], loader)
            except (ValueError, ParserError) as exc:
                results.append({**details, "equivalent": False, "error": str(exc)})
                continue
            required_graphs.update(
                primary if name == "@default" else URIRef(
                    f"{primary}/source-graph/{hashlib.sha256(name.encode()).hexdigest()}"
                )
                for name in nonempty
            )
            actual = [(None, dataset.get_context(primary))]
            errors = []
            if set(default.objects(iri, repo.graphIRI)) != {primary}:
                errors.append("Primary graph declaration disagrees with the stored version graph IRI")
            source_graphs = {primary, *default.objects(iri, repo.sourceGraph)}
            blank_nodes = {
                term for graph in source_graphs if isinstance(graph, URIRef)
                for triple in dataset.get_context(graph) for term in triple if isinstance(term, BNode)
            }
            for graph in sorted(source_graphs - {primary}, key=str):
                if not isinstance(graph, URIRef):
                    errors.append("An auxiliary graph declaration is not an IRI")
                    continue
                names = set(default.objects(graph, repo.sourceGraphName))
                if len(names) != 1:
                    errors.append(f"Auxiliary graph {graph} must have one original graph name")
                    continue
                name = next(iter(names))
                if not isinstance(name, (URIRef, BNode)):
                    errors.append(f"Auxiliary graph {graph} has an invalid original graph name")
                    continue
                if isinstance(name, BNode):
                    blank_nodes.add(name)
                actual.append((name, dataset.get_context(graph)))
            for term in blank_nodes:
                if term in scopes_seen:
                    errors.append(f"A blank node is shared with a different version, {scopes_seen[term]}")
                else:
                    scopes_seen[term] = iri
            equivalent = _equivalent_scopes(expected, actual)
            expected_ground, actual_ground = _ground_quads(expected), _ground_quads(actual)
            results.append({
                **details,
                "equivalent": equivalent and not errors,
                "expected_graph_scopes": len(expected), "actual_graph_scopes": len(actual),
                "expected_quads": sum(len(graph) for _, graph in expected),
                "actual_quads": sum(len(graph) for _, graph in actual),
                "missing_ground_quads": sorted(expected_ground - actual_ground),
                "unexpected_ground_quads": sorted(actual_ground - expected_ground),
                "scope_errors": sorted(set(errors)),
                **({"error": "RDF terms or blank-node relationships differ across the version's graph scopes"}
                   if not equivalent else {}),
            })
    results.sort(key=lambda row: row["version_iri"])
    return {
        "conforms": all(row["equivalent"] for row in results),
        "checked_versions": len(results),
        "equivalent_versions": sum(row["equivalent"] for row in results),
        "versions": results,
    }, required_graphs
