"""Short-lived worker for bounded, local RDF parsing, querying, and validation."""

from __future__ import annotations

import copyreg
import pickle
import sys
import warnings
from pathlib import Path

from pyshacl import validate
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, SH
from rdflib.plugins import sparql  # noqa: F401  # Register the local evaluator up front.

from src import rdf_fidelity  # noqa: F401  # Load the Phase 5 fidelity checker before I/O restrictions.
from src.rdf_export import dataset_summary, parse_dataset
from src.rdf_query import execute_bounded_read_query, prepare_read_query
from src.semantic_validation import ROOT, validate_semantics

UPLOAD_BASE = "https://example.invalid/oersi/upload/"
MAX_BYTES = 4 * 1024 * 1024
MAX_QUADS = 25_000
MAX_GRAPHS = 500
MAX_SHAPES_BYTES = 256 * 1024
MAX_SHAPES_TRIPLES = 1500
MAX_QUERY_BYTES = 32 * 1024
MAX_ROWS = 1000
SHAPE_FILE = ROOT / "shapes/repository-shapes.ttl"

# Only Core vocabulary is accepted from uploaded shape graphs. Reject unknown
# SH terms even when the active pySHACL version happens to ignore them.
CORE_PROPERTIES = frozenset(
    """targetClass targetNode targetSubjectsOf targetObjectsOf property path
    deactivated severity message name description order group defaultValue
    minCount maxCount minExclusive minInclusive maxExclusive maxInclusive
    minLength maxLength pattern flags languageIn uniqueLang equals disjoint
    lessThan lessThanOrEquals class datatype nodeKind node not and or xone in
    hasValue closed ignoredProperties qualifiedValueShape qualifiedMinCount
    qualifiedMaxCount qualifiedValueShapesDisjoint inversePath alternativePath
    zeroOrMorePath oneOrMorePath zeroOrOnePath""".split()
)
CORE_TYPES = {SH.NodeShape, SH.PropertyShape, SH.PropertyGroup}


def _reduce_literal(term: Literal):
    # RDFLib's default reducer reconstructs typed literals with normalization.
    return Literal, (str(term), term.language, term.datatype, False)


def write_worker_message(pipe, message):
    """Write a trusted IPC frame while retaining RDF literal lexical forms."""
    pickler = pickle.Pickler(pipe)
    pickler.dispatch_table = {**copyreg.dispatch_table, Literal: _reduce_literal}
    pickler.dump(message)


def enforce_size(dataset: Dataset) -> None:
    counts = dataset_summary(dataset)
    if counts["total_quads"] > MAX_QUADS or counts["named_graphs"] > MAX_GRAPHS:
        raise ValueError(f"Dataset exceeds the workbench limit of {MAX_QUADS:,} quads or {MAX_GRAPHS} named graphs")


def _graph(dataset, identifier):
    if identifier is None:
        return dataset.default_graph
    return next(graph for graph in dataset.graphs() if graph.identifier == identifier)


def _parse(data: bytes, format: str) -> Dataset:
    if format not in {"trig", "nquads"} or not data or len(data) > MAX_BYTES:
        raise ValueError("Upload a nonempty TriG or N-Quads file within the 4 MiB limit")
    dataset = parse_dataset(data, format=format, publicID=UPLOAD_BASE)
    enforce_size(dataset)
    return dataset


def _custom_shapes(dataset, identifier, data):
    if not data or len(data) > MAX_SHAPES_BYTES:
        raise ValueError("Upload a nonempty Turtle shapes file within the 256 KiB limit")
    shapes = Graph().parse(data=data, format="turtle", publicID=UPLOAD_BASE)
    if len(shapes) > MAX_SHAPES_TRIPLES:
        raise ValueError(f"A shapes graph may contain at most {MAX_SHAPES_TRIPLES:,} triples")
    for subject, predicate, obj in shapes:
        if predicate == OWL.imports or (
            str(predicate).startswith(str(SH)) and str(predicate)[len(str(SH)):] not in CORE_PROPERTIES
        ) or (predicate == RDF.type and str(obj).startswith(str(SH)) and obj not in CORE_TYPES):
            raise ValueError(f"Uploaded shapes must use SHACL Core. Unsupported directive: {predicate} {obj}")
    if not any(
        predicate == RDF.type and obj in CORE_TYPES - {SH.PropertyGroup}
        or predicate in {SH.targetClass, SH.targetNode, SH.targetSubjectsOf, SH.targetObjectsOf}
        for _, predicate, obj in shapes
    ):
        raise ValueError("The shapes graph must declare a SHACL Core shape or target")
    conforms, report, _ = validate(
        _graph(dataset, identifier), shacl_graph=shapes, inference="none", inplace=False,
        advanced=False, js=False, do_owl_imports=False, sparql_mode=False, max_validation_depth=12,
    )
    violations = [
        {
            str(predicate).removeprefix(str(SH)): sorted(item.n3() for item in report.objects(node, predicate))
            for predicate in (SH.focusNode, SH.resultPath, SH.value, SH.resultMessage, SH.sourceShape,
                              SH.sourceConstraintComponent, SH.resultSeverity)
        }
        for node in report.subjects(RDF.type, SH.ValidationResult)
    ]
    return {"conforms": bool(conforms), "violations": violations}, report.serialize(format="turtle")


def _repository_validation(dataset, base_iri, vocabulary_iri, snapshot, contexts):
    report, rdf = validate_semantics(
        dataset, base_iri=base_iri, vocabulary_iri=vocabulary_iri, snapshot=snapshot, contexts=contexts,
    )
    return report, rdf.serialize(format="turtle")


def _query(dataset, source, vocabulary, bindings):
    if not source.strip() or len(source.encode()) > MAX_QUERY_BYTES:
        raise ValueError("Enter a SPARQL query of up to 32 KiB")
    query = prepare_read_query(source, vocabulary_iri=vocabulary)
    result, truncated = execute_bounded_read_query(dataset, query, max_rows=MAX_ROWS, bindings=bindings)
    return {"kind": "ASK" if isinstance(result, bool) else "SELECT", "result": result, "truncated": truncated}


def _restrict_io(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, int) or path == str(SHAPE_FILE) and args[1] == "r":
            return
        raise PermissionError("The local RDF worker blocks filesystem access")
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event == "os.system":
        raise PermissionError("The local RDF worker blocks network and external process access")
    if event in {"os.listdir", "os.scandir", "os.chdir", "os.remove", "os.rename", "os.link", "os.symlink"}:
        raise PermissionError("The local RDF worker blocks filesystem access")


def _apply_linux_limits():
    if not sys.platform.startswith("linux"):
        return
    import resource

    memory = 768 * 1024 * 1024
    for kind, ceiling in ((resource.RLIMIT_AS, (memory, memory)), (resource.RLIMIT_CPU, (12, 13))):
        limits = tuple(
            cap if inherited == resource.RLIM_INFINITY else min(cap, inherited)
            for inherited, cap in zip(resource.getrlimit(kind), ceiling, strict=True)
        )
        resource.setrlimit(kind, limits)


def main():
    _apply_linux_limits()
    # Load all selected parsers and serializers before the worker disables I/O.
    empty = parse_dataset(b"", format="trig")
    parse_dataset(b"", format="nquads")
    Graph().parse(data="", format="turtle")
    Graph().serialize(format="turtle")
    _query(empty, "SELECT ?s WHERE { ?s ?p ?o }", None, {})
    validate(Graph(), shacl_graph=Graph(), inference="none", do_owl_imports=False)
    run = {"parse": _parse, "shapes": _custom_shapes, "repository": _repository_validation, "query": _query}
    # The trusted parent builds the pickle on a private pipe. Browser inputs
    # are encoded as data fields in that object, never used as pickle streams.
    action, arguments = pickle.load(sys.stdin.buffer)
    # These hooks provide best-effort checks for Python-mediated I/O.
    warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"rdflib(\.|$)")
    sys.addaudithook(_restrict_io)
    try:
        result = (True, run[action](*arguments))
    except Exception as exc:
        result = (False, f"{type(exc).__name__}: {exc}"[:1800])
    write_worker_message(sys.stdout.buffer, result)


if __name__ == "__main__":
    main()
