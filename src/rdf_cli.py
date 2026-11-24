"""Export, inspect, and query the repository's local RDF dataset."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from rdflib import Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Identifier

from src.context_snapshot import ContextSnapshot, inspect_context_snapshot, load_context_snapshot
from src.rdf_export import FORMATS, acquire_contexts, build_dataset, dataset_summary, load_dataset, save_dataset
from src.rdf_query import execute_read_query, prepare_read_query
from src.repository import Repository, normalize_base_iri, normalize_vocabulary_iri, observation_time


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export and query the local OERSI RDF dataset.")
    parser.add_argument(
        "--database", type=Path, default=Path(os.getenv("OERSI_REPOSITORY_DATABASE", "data/repository.sqlite3")),
        help="Existing SQLite repository (default: data/repository.sqlite3).",
    )
    parser.add_argument("--base-iri", default=os.getenv("OERSI_REPOSITORY_BASE_IRI", "https://example.org/oersi/"))
    parser.add_argument("--vocabulary-iri", default=os.getenv("OERSI_REPOSITORY_VOCABULARY_IRI"))
    parser.add_argument(
        "--context-snapshot", type=Path, default=os.getenv("OERSI_RDF_CONTEXT_SNAPSHOT"),
        help="Local context snapshot for SQLite-backed exports and queries.",
    )
    parser.add_argument(
        "--context-sha256", default=os.getenv("OERSI_RDF_CONTEXT_SHA256"),
        help="Approved SHA-256 of the snapshot manifest.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Export a read-only SQLite snapshot as TriG or N-Quads.")
    export.add_argument("--format", choices=FORMATS, required=True)
    export.add_argument("--output", type=Path, required=True, help="New .trig or .nq file.")
    export.add_argument("--resource", help="Select one resource by stable ID or repository IRI.")

    query = commands.add_parser("query", help="Evaluate a local SELECT or ASK query from a file.")
    query.add_argument("--file", type=Path, required=True, help="Local SPARQL query file.")
    inputs = query.add_mutually_exclusive_group()
    inputs.add_argument("--input", type=Path, help="Read a local .trig or .nq file instead of SQLite.")
    inputs.add_argument("--resource", help="Materialize only this resource from SQLite.")
    query.add_argument("--bind-iri", action="append", default=[], metavar="NAME=IRI")
    query.add_argument("--bind-integer", action="append", default=[], metavar="NAME=INTEGER")
    query.add_argument("--bind-time", action="append", default=[], metavar="NAME=ISO-8601")

    validate = commands.add_parser("validate", help="Parse a local RDF dataset and report graph counts.")
    validate.add_argument("--input", type=Path, required=True)

    acquire = commands.add_parser("acquire-contexts", help="Fetch a new context snapshot candidate for review.")
    acquire.add_argument("--output", type=Path, required=True, help="New snapshot directory.")
    acquire.add_argument("--resource", help="Capture contexts for one resource's complete history.")
    inspect = commands.add_parser("inspect-contexts", help="Verify and list a local context snapshot's pins.")
    inspect.add_argument("--input", type=Path, required=True, help="Snapshot directory.")
    return parser


def _query_bindings(args: argparse.Namespace) -> dict[str, Identifier]:
    bindings = {}
    for kind, pairs in (("iri", args.bind_iri), ("integer", args.bind_integer), ("time", args.bind_time)):
        for pair in pairs:
            name, separator, value = pair.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                raise ValueError(f"Expected a binding in NAME=value form, got {pair!r}")
            if name in bindings:
                raise ValueError(f"Query variable {name!r} was bound more than once")
            if kind == "iri":
                if not urlsplit(value).scheme or any(char.isspace() or char in '<>"{}|^\x60\\' for char in value):
                    raise ValueError(f"Expected an absolute IRI for query variable {name!r}")
                bindings[name] = URIRef(value)
            elif kind == "integer":
                bindings[name] = Literal(int(value))
            else:
                bindings[name] = Literal(observation_time(value), datatype=XSD.dateTime, normalize=False)
    return bindings


def _approved_contexts(args: argparse.Namespace) -> ContextSnapshot:
    if args.context_snapshot is None or not args.context_sha256:
        raise ValueError("SQLite-backed RDF requires both --context-snapshot and --context-sha256")
    return load_context_snapshot(args.context_snapshot, args.context_sha256)


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            result = dataset_summary(load_dataset(args.input))
        elif args.command == "inspect-contexts":
            result = inspect_context_snapshot(args.input)
        else:
            base = normalize_base_iri(args.base_iri)
            vocabulary = normalize_vocabulary_iri(args.vocabulary_iri or f"{base}/vocab/")
            if args.command == "query":
                # Reject remote or unsupported SPARQL before reading any dataset.
                query = prepare_read_query(args.file.read_text(encoding="utf-8"), vocabulary_iri=vocabulary)
                bindings = _query_bindings(args)
                if args.input is not None:
                    dataset = load_dataset(args.input)
                else:
                    contexts = _approved_contexts(args)
                    dataset = build_dataset(
                        Repository(args.database, base, vocabulary_iri=vocabulary), args.resource, contexts=contexts,
                    )
                result = execute_read_query(dataset, query, bindings=bindings)
            elif args.command == "acquire-contexts":
                repository = Repository(args.database, base, vocabulary_iri=vocabulary)
                result = acquire_contexts(repository, args.output, args.resource)
            else:
                contexts = _approved_contexts(args)
                dataset = build_dataset(
                    Repository(args.database, base, vocabulary_iri=vocabulary), args.resource, contexts=contexts,
                )
                save_dataset(dataset, args.output, format=args.format)
                result = {
                    "output": str(args.output), "format": args.format,
                    "context_snapshot_sha256": contexts.sha256, **dataset_summary(dataset),
                }
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
