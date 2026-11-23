"""Command-line access to OERSI resources, history, and review decisions."""

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
    parser = argparse.ArgumentParser(description="Manage the SQLite OERSI resource repository.")
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
    migration = commands.add_parser("migrate", help="Back up and migrate a Phase 2 or Phase 3 draft database.")
    migration.add_argument("--backup", type=Path, required=True, help="Path for a new SQLite backup.")
    import_parser = commands.add_parser("import", help="Import Phase 1 JSON-LD and normalized metadata.")
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    commands.add_parser("containers", help="List the two containers.")
    resources_parser = commands.add_parser("resources", help="List resources in a container.")
    resources_parser.add_argument("container_id", choices=("courses", "learning_materials"))
    get_parser = commands.add_parser("get", help="Retrieve a resource and its current version by ID or IRI.")
    get_parser.add_argument("identifier")
    commands.add_parser("stats", help="Display repository statistics.")
    commands.add_parser("unclassified", help="Display resources awaiting classification review.")
    history = commands.add_parser("history", help="List a resource's versions in order.")
    history.add_argument("identifier", help="Stable resource ID or IRI.")
    version = commands.add_parser("get-version", help="Retrieve a version by IRI or resource and number.")
    version.add_argument("identifier", help="Version IRI, or resource ID or IRI with --version.")
    version.add_argument("--version", type=int)
    at_time = commands.add_parser("at-time", help="Retrieve a resource version as of a specified time.")
    at_time.add_argument("identifier", help="Stable resource ID or IRI.")
    at_time.add_argument("--timestamp", required=True, help="ISO-8601 timestamp with an explicit time zone.")
    at_time.add_argument(
        "--basis", choices=("observation", "repository"), default="observation",
        help="Use source observation time (default) or recorded repository version creation time.",
    )
    compare = commands.add_parser("compare", help="Compare original and normalized metadata across versions.")
    compare.add_argument("identifier", help="Stable resource ID or IRI.")
    compare.add_argument("--from", dest="from_version", required=True, type=int)
    compare.add_argument("--to", dest="to_version", required=True, type=int)
    pending = commands.add_parser("pending", help="List pending metadata and classification candidates.")
    pending.add_argument("--all", action="store_true", help="Include the full review audit trail.")
    for name, description in (
        ("review", "Inspect a candidate and compare it against the current version."),
        ("accept", "Accept a pending candidate as a new version."),
        ("reject", "Reject a pending candidate and record the decision."),
    ):
        action = commands.add_parser(name, help=description)
        action.add_argument("update_id", type=int)
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
        if args.command in {"init", "import"} or (
            args.command in {"containers", "resources", "get", "stats", "unclassified", "pending"}
            and not args.database.exists()
        ):
            repository.initialize()
        if args.command == "init":
            _print({"status": "initialized", "database": str(args.database), "containers": repository.containers()})
        elif args.command == "migrate":
            repository.migrate(args.backup)
            _print({"status": "migrated", "database": str(args.database), "backup": str(args.backup)})
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
            updates = repository.list_pending_updates(include_decided=args.all)
            for update in updates:
                update["candidate_jsonld"] = json.loads(update["candidate_jsonld"])
                if update["candidate_normalized_metadata"] is not None:
                    update["candidate_normalized_metadata"] = json.loads(update["candidate_normalized_metadata"])
            _print(updates)
        elif args.command == "history":
            _print(repository.list_versions(args.identifier))
        elif args.command in {"get-version", "at-time"}:
            if args.command == "get-version":
                result = repository.get_version(args.identifier, args.version)
            else:
                result = repository.get_version_at_time(args.identifier, args.timestamp, basis=args.basis)
            if result is None:
                raise LookupError(f"No matching version for {args.identifier!r}")
            _print(result)
        elif args.command == "compare":
            _print(repository.compare_versions(args.identifier, args.from_version, args.to_version))
        elif args.command == "review":
            _print(repository.review_update(args.update_id))
        elif args.command == "accept":
            _print(repository.accept_update(args.update_id))
        elif args.command == "reject":
            _print(repository.reject_update(args.update_id))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
