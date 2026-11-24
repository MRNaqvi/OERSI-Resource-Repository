"""Restricted local SPARQL evaluation using RDFLib's parse tree and algebra."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pyparsing import ParseBaseException, ParseResults
from rdflib import Dataset, Namespace
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.sparql import Query
from rdflib.term import Identifier

READ_QUERIES = {"SelectQuery", "AskQuery"}


def _require_local_patterns(node: Any) -> None:
    if isinstance(node, CompValue):
        if node.name == "ServiceGraphPattern":
            raise ValueError("SPARQL SERVICE is unavailable for local queries")
        if node.name == "DatasetClause" or ("datasetClause" in node and node["datasetClause"]):
            raise ValueError("SPARQL FROM and FROM NAMED are unavailable for local queries")
        for child in node.values():
            _require_local_patterns(child)
    elif isinstance(node, (list, tuple, ParseResults)):
        for child in node:
            _require_local_patterns(child)
        if isinstance(node, ParseResults):
            for child in node.values():
                _require_local_patterns(child)


def prepare_read_query(text: str, *, vocabulary_iri: str | None = None) -> Query:
    try:
        parsed = parseQuery(text)
    except ParseBaseException as exc:
        raise ValueError(f"Expected a valid SPARQL SELECT or ASK query: {exc}") from exc
    if parsed[1].name not in READ_QUERIES:
        raise ValueError("Only SPARQL SELECT and ASK queries are supported")
    _require_local_patterns(parsed[1])
    namespaces = {"repo": Namespace(vocabulary_iri)} if vocabulary_iri else None
    return translateQuery(parsed, initNs=namespaces)


def execute_read_query(
    dataset: Dataset, query: Query, *, bindings: Mapping[str, Identifier] | None = None,
) -> dict[str, Any] | bool:
    if query.algebra.name not in READ_QUERIES:
        raise ValueError("Only SPARQL SELECT and ASK queries are supported")
    _require_local_patterns(query.algebra)
    if dataset.default_union:
        raise ValueError("Local queries require a dataset with an independent default graph")
    result = dataset.query(query, initBindings=bindings, use_store_provided=False)
    if result.type == "ASK":
        return bool(result.askAnswer)
    return json.loads(result.serialize(format="json"))
