"""Phase 2 command-line tools for persistent OERSI resource metadata."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.repository import Repository


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Phase 2 SQLite OERSI resource repository.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("OERSI_REPOSITORY_DATABASE", "data/repository.sqlite3")),
        help="SQLite database path (default: data/repository.sqlite3).",
    )
    parser.add_argument(
        "--base-iri",
        default=os.getenv("OERSI_REPOSITORY_BASE_IRI", "https://example.org/oersi/"),
        help="Persistent repository IRI namespace; must match the database configuration.",
    )
    parser.add_argument(
        "--vocabulary-iri",
        default=os.getenv("OERSI_REPOSITORY_VOCABULARY_IRI"),
        help="Repository vocabulary namespace; defaults to <base-iri>/vocab/ and must match the database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the schema and the two resource containers.")
    import_parser = commands.add_parser("import", help="Import Phase 1 JSON-LD and normalized metadata.")
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    commands.add_parser("containers", help="List the two containers.")
    resources_parser = commands.add_parser("resources", help="List resources in a container.")
    resources_parser.add_argument("container_id", choices=("courses", "learning_materials"))
    get_parser = commands.add_parser("get", help="Retrieve a resource and its current version by ID or IRI.")
    get_parser.add_argument("identifier")
    commands.add_parser("stats", help="Display repository statistics.")
    commands.add_parser("unclassified", help="Display resources awaiting classification review.")
    commands.add_parser("pending", help="Display pending metadata/classification updates.")
    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    repository = Repository(
        args.database,
        base_iri=args.base_iri,
        vocabulary_iri=args.vocabulary_iri,
    )

    try:
        repository.initialize()
        if args.command == "init":
            _print({"status": "initialized", "database": str(args.database), "containers": repository.containers()})
        elif args.command == "import":
            _print(repository.import_directory(args.data_dir))
        elif args.command == "containers":
            _print(repository.containers())
        elif args.command == "resources":
            _print(repository.list_resources(args.container_id))
        elif args.command == "get":
            result = repository.get_resource(args.identifier)
            if result is None:
                print(f"Resource not found: {args.identifier}")
                return 1
            _print(result)
        elif args.command == "stats":
            _print(repository.stats())
        elif args.command == "unclassified":
            _print(repository.list_unclassified())
        elif args.command == "pending":
            _print(repository.list_pending_updates())
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
