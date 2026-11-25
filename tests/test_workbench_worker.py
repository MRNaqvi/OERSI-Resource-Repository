from __future__ import annotations

import pickle
import subprocess
import sys
from types import SimpleNamespace

import pytest
from rdflib import BNode, Dataset, Literal, Namespace
from rdflib.namespace import XSD
from rdflib.plugins.sparql import evaluate
from rdflib.plugins.sparql.results.jsonresults import JSONResultSerializer

from src import workbench, workbench_worker
from src.rdf_query import execute_bounded_read_query, execute_read_query, prepare_read_query

EX = Namespace("urn:workbench:test:")
MAX_ROWS = workbench_worker.MAX_ROWS
SELECT = "SELECT ?s ?value WHERE { ?s <urn:workbench:test:predicate> ?value }"


@pytest.fixture(scope="module")
def numeric_dataset():
    dataset = Dataset()
    for number in range(6500):
        dataset.default_graph.add((EX[str(number)], EX.predicate, Literal(number)))
    return dataset


@pytest.mark.parametrize("matches,expected_rows,truncated", [
    (0, 0, False), (1, 1, False), (1000, 1000, False), (6500, 1000, True),
])
def test_select_is_bounded_at_json_serializer(numeric_dataset, monkeypatch, matches, expected_rows, truncated):
    original = JSONResultSerializer.serialize
    calls = []

    def inspect(serializer, stream, encoding=None):
        # Guard the serialization boundary, where the old code received all rows.
        assert len(serializer.result.bindings) == expected_rows
        calls.append(serializer.result.vars)
        return original(serializer, stream, encoding=encoding)

    monkeypatch.setattr(JSONResultSerializer, "serialize", inspect)
    result = workbench_worker._query(
        numeric_dataset, SELECT[:-1] + f" FILTER (?value < {matches}) }} ORDER BY ?value", None, {},
    )
    assert len(calls) == 1
    assert result["kind"] == "SELECT" and result["truncated"] is truncated
    assert result["result"]["head"]["vars"] == ["s", "value"]
    assert [int(row["value"]["value"]) for row in result["result"]["results"]["bindings"]] == list(range(expected_rows))


def test_streamable_select_reads_only_one_extra_solution(numeric_dataset, monkeypatch):
    seen = 0
    original = evaluate.evalBGP

    def count_solutions(ctx, patterns):
        nonlocal seen
        for row in original(ctx, patterns):
            if patterns:
                seen += 1
                assert seen <= MAX_ROWS + 1, "The worker exhausted the unbounded selection"
            yield row

    monkeypatch.setattr(evaluate, "evalBGP", count_solutions)
    result = workbench_worker._query(numeric_dataset, SELECT, None, {})
    assert result["truncated"] and seen == MAX_ROWS + 1
    assert len(result["result"]["results"]["bindings"]) == MAX_ROWS


def test_worker_preserves_order_user_limit_offset_and_unbounded_caller(numeric_dataset):
    query = prepare_read_query(SELECT + " ORDER BY DESC(?value) LIMIT 1002 OFFSET 2000")
    bounded, truncated = execute_bounded_read_query(numeric_dataset, query, max_rows=MAX_ROWS)
    assert truncated
    assert [int(row["value"]["value"]) for row in bounded["results"]["bindings"]] == list(range(4499, 3499, -1))
    original = execute_read_query(numeric_dataset, query)
    assert len(original["results"]["bindings"]) == 1002
    exact = workbench.run_query(numeric_dataset, SELECT + " ORDER BY DESC(?value) LIMIT 1000 OFFSET 2000", None)
    assert not exact["truncated"] and exact["result"] == bounded


def test_worker_preserves_rdf_terms_columns_initial_and_empty_bindings():
    dataset = Dataset()
    blank = BNode()
    graph = dataset.default_graph
    graph.add((EX.subject, EX.link, blank))
    graph.add((blank, EX.label, Literal("Bonjour", lang="fr")))
    graph.add((blank, EX.count_, Literal("0007", datatype=XSD.integer, normalize=False)))
    graph.add((EX.unrelated, EX.link, blank))
    query = """
        SELECT ?s ?node ?label ?number ?missing WHERE {
            ?s <urn:workbench:test:link> ?node .
            ?node <urn:workbench:test:label> ?label ;
                <urn:workbench:test:count_> ?number .
        }
    """
    reply = workbench.run_query(dataset, query, None, iri=f"s={EX.subject}")
    result = reply["result"]
    assert reply["kind"] == "SELECT" and not reply["truncated"]
    assert result["head"]["vars"] == ["s", "node", "label", "number", "missing"]
    row, = result["results"]["bindings"]
    assert row == {
        "s": {"type": "uri", "value": str(EX.subject)},
        "node": {"type": "bnode", "value": str(blank)},
        "label": {"type": "literal", "value": "Bonjour", "xml:lang": "fr"},
        "number": {"type": "literal", "value": "0007", "datatype": str(XSD.integer)},
    }
    unbound = workbench.run_query(dataset, "SELECT ?unbound WHERE {}", None)
    assert unbound == {"kind": "SELECT", "truncated": False,
                       "result": {"head": {"vars": ["unbound"]}, "results": {"bindings": [{}]}}}


def test_uploaded_literals_retain_distinct_lexical_forms_across_the_worker_boundary():
    data = f'<{EX.subject}> <{EX.predicate}> "0007"^^<{XSD.integer}> .\n'.encode()
    data += f'<{EX.subject}> <{EX.predicate}> "7"^^<{XSD.integer}> .\n'.encode()
    _, dataset = workbench.parse_upload("numbers.nq", data)
    assert {str(term) for term in dataset.default_graph.objects(EX.subject, EX.predicate)} == {"0007", "7"}
    result = workbench.run_query(dataset, SELECT, None)["result"]
    assert {row["value"]["value"] for row in result["results"]["bindings"]} == {"0007", "7"}


@pytest.mark.parametrize("present", [True, False])
def test_ask_returns_boolean_without_sparql_json_serialization(monkeypatch, present):
    def unexpected_serialization(*args, **kwargs):
        pytest.fail("The worker should return the ASK boolean directly")

    monkeypatch.setattr(JSONResultSerializer, "serialize", unexpected_serialization)
    dataset = Dataset()
    if present:
        dataset.default_graph.add((EX.subject, EX.predicate, Literal(1)))
    result = workbench_worker._query(dataset, "ASK { ?s ?p ?o }", None, {})
    assert result == {"kind": "ASK", "result": present, "truncated": False}


@pytest.mark.parametrize("count,named,diagnostic", [
    (workbench_worker.MAX_QUADS + 1, False, "25,000 quads"),
    (workbench_worker.MAX_GRAPHS + 1, True, "500 named graphs"),
])
def test_worker_rejects_excess_quads_and_named_graphs(count, named, diagnostic):
    data = "".join(
        f"<urn:s:{number}> <urn:p> <urn:o>" + (f" <urn:g:{number}>" if named else "") + " .\n"
        for number in range(count)
    ).encode()
    assert len(data) < workbench_worker.MAX_BYTES
    with pytest.raises(ValueError, match=diagnostic):
        workbench.parse_upload("large.nq", data)


def test_shacl_file_limit_at_parent_and_worker():
    data = b"#" * (workbench_worker.MAX_SHAPES_BYTES + 1)
    with pytest.raises(ValueError, match="at most 256 KiB"):
        workbench.general_validation(Dataset(), shapes=data)
    with pytest.raises(ValueError, match="within the 256 KiB limit"):
        workbench._worker("shapes", Dataset(), None, data)


def test_shacl_triple_limit_in_worker():
    data = "".join(
        f"<urn:shape:{number}> <urn:p> <urn:o> .\n"
        for number in range(workbench_worker.MAX_SHAPES_TRIPLES + 1)
    ).encode()
    assert len(data) < workbench_worker.MAX_SHAPES_BYTES
    with pytest.raises(ValueError, match="at most 1,500 triples"):
        workbench.general_validation(Dataset(), shapes=data)


def test_query_utf8_limit_at_parent_and_worker():
    query = "ASK {} # " + "é" * (workbench_worker.MAX_QUERY_BYTES // 2)
    assert len(query) < workbench_worker.MAX_QUERY_BYTES < len(query.encode("utf-8"))
    with pytest.raises(ValueError, match="up to 32 KiB"):
        workbench.run_query(Dataset(), query, None)
    with pytest.raises(ValueError, match="up to 32 KiB"):
        workbench._worker("query", Dataset(), query, None, {})


@pytest.mark.parametrize("declaration", [
    "a sh:NodeShape ; sh:targetNode <urn:s> ; sh:rule []",
    "a sh:NodeShape ; sh:target []",
    "a sh:SPARQLFunction",
])
def test_worker_rejects_non_core_shacl_rules_targets_and_types(declaration):
    shapes = f"@prefix sh: <http://www.w3.org/ns/shacl#> . <urn:shape> {declaration} .".encode()
    with pytest.raises(ValueError, match="Unsupported directive"):
        workbench.general_validation(Dataset(), shapes=shapes)


def test_browser_file_bytes_stay_data_inside_internal_pickle(monkeypatch):
    data = pickle.dumps(("ordinary uploaded content",))

    def capture(command, *, cwd, input, capture_output, timeout):
        assert command == [sys.executable, "-m", "src.workbench_worker"]
        assert cwd == workbench.ROOT and capture_output and timeout == workbench.WORKER_TIMEOUT
        assert pickle.loads(input) == ("parse", (data, "trig"))
        return subprocess.CompletedProcess(command, 0, stdout=pickle.dumps((True, Dataset())))

    monkeypatch.setattr(workbench.subprocess, "run", capture)
    format_, dataset = workbench.parse_upload("data.trig", data)
    assert format_ == "trig" and isinstance(dataset, Dataset)


@pytest.mark.parametrize("failure,message", [
    ("timeout", f"RDF operation exceeded {workbench.WORKER_TIMEOUT} seconds"),
    ("launch", "Could not start the RDF worker"),
    ("exit", "RDF worker stopped"),
    ("empty", "RDF worker returned an invalid response"),
    ("corrupt", "RDF worker returned an invalid response"),
    ("envelope", "RDF worker returned an invalid response"),
    ("worker_error", "Invalid RDF example"),
])
def test_worker_process_failure_diagnostics(monkeypatch, failure, message):
    def run(command, *, timeout, **kwargs):
        assert timeout == workbench.WORKER_TIMEOUT
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if failure == "launch":
            raise OSError("Cannot launch process")
        if failure == "exit":
            return subprocess.CompletedProcess(command, 1, stderr=b"test process exit")
        frames = {
            "empty": b"",
            "corrupt": b"not a worker frame",
            "envelope": pickle.dumps(("yes", None)),
            "worker_error": pickle.dumps((False, "Invalid RDF example")),
        }
        return subprocess.CompletedProcess(command, 0, stdout=frames[failure])

    monkeypatch.setattr(workbench.subprocess, "run", run)
    with pytest.raises(ValueError, match=message):
        workbench.parse_upload("file.trig", b"<urn:s> <urn:p> <urn:o> .")


def test_linux_limits_keep_stricter_inherited_limits(monkeypatch):
    memory = 768 * 1024 * 1024
    changes = []
    mock = SimpleNamespace(
        RLIMIT_AS=1, RLIMIT_CPU=2, RLIM_INFINITY=-1,
        getrlimit={1: (-1, -1), 2: (6, 9)}.__getitem__,
        setrlimit=lambda kind, limits: changes.append((kind, limits)),
    )
    monkeypatch.setattr(workbench_worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "resource", mock)
    workbench_worker._apply_linux_limits()
    assert changes == [(1, (memory, memory)), (2, (6, 9))]
    changes.clear()
    mock.getrlimit = {1: (memory // 2, memory), 2: (-1, -1)}.__getitem__
    workbench_worker._apply_linux_limits()
    assert changes == [(1, (memory // 2, memory)), (2, (12, 13))]


def test_worker_applies_limits_before_reading_private_message(monkeypatch):
    class StopAtMessage(Exception):
        pass

    events = []

    def read_message(pipe):
        assert events == ["limits"]
        raise StopAtMessage

    monkeypatch.setattr(workbench_worker, "_apply_linux_limits", lambda: events.append("limits"))
    monkeypatch.setattr(workbench_worker, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=object())))
    monkeypatch.setattr(workbench_worker, "pickle", SimpleNamespace(load=read_message))
    with pytest.raises(StopAtMessage):
        workbench_worker.main()
