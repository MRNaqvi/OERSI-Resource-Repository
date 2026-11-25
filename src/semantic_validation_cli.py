"""Validate local repository RDF or run the offline competency-question benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv

from src.competency_evaluation import run_benchmark
from src.context_snapshot import load_context_snapshot
from src.rdf_export import dataset_from_snapshot, load_dataset
from src.repository import Repository
from src.semantic_validation import validate_semantics


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=os.getenv("OERSI_REPOSITORY_DATABASE", "data/repository.sqlite3"))
    parser.add_argument("--base-iri", default=os.getenv("OERSI_REPOSITORY_BASE_IRI", "https://example.org/oersi/"))
    parser.add_argument("--vocabulary-iri", default=os.getenv("OERSI_REPOSITORY_VOCABULARY_IRI"))
    parser.add_argument("--context-snapshot", type=Path, default=os.getenv("OERSI_RDF_CONTEXT_SNAPSHOT"))
    parser.add_argument("--context-sha256", default=os.getenv("OERSI_RDF_CONTEXT_SHA256"))
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Check SHACL, repository integrity, and optional source fidelity.")
    validate.add_argument("--input", type=Path, help="Local .trig or .nq dataset. Omit to read from SQLite.")
    validate.add_argument("--against-sqlite", action="store_true", help="Check a local RDF input against stored snapshots.")
    validate.add_argument("--resource", help="Select one resource by stable ID or IRI when reading SQLite.")
    validate.add_argument("--report-json", type=Path)
    validate.add_argument("--shacl-report", type=Path, help="Write the standard RDF report in Turtle.")
    evaluate = commands.add_parser("evaluate", help="Run the offline controlled benchmark and query gold comparison.")
    evaluate.add_argument("--output-dir", type=Path, help="New directory for JSON and SHACL RDF reports.")
    return parser


def _write_new(path, data):
    with path.open("x", encoding="utf-8") as output:
        output.write(data)


def _json(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _validation_input(args):
    if args.against_sqlite and not args.input:
        raise ValueError("--against-sqlite requires --input. Omit both to build and validate from SQLite")
    use_sqlite = not args.input or args.against_sqlite
    if args.resource and not use_sqlite:
        raise ValueError("--resource requires a SQLite-backed validation")
    repository = Repository(args.database, args.base_iri, vocabulary_iri=args.vocabulary_iri)
    namespaces = {"base_iri": repository.base_iri, "vocabulary_iri": repository.vocabulary_iri}
    snapshot = contexts = None
    if use_sqlite:
        if not args.context_snapshot or not args.context_sha256:
            raise ValueError("SQLite-backed validation requires both --context-snapshot and --context-sha256")
        contexts = load_context_snapshot(args.context_snapshot, args.context_sha256)
        snapshot = repository.export_snapshot(args.resource)
    dataset = (
        load_dataset(args.input) if args.input else dataset_from_snapshot(snapshot, contexts=contexts, **namespaces)
    )
    return validate_semantics(dataset, snapshot=snapshot, contexts=contexts, **namespaces)


def _summarize(report):
    shacl = report["shacl"]
    integrity = report["integrity"]
    fidelity = report["fidelity"]
    print(f"SHACL default graph: {'conforms' if shacl['conforms'] else 'fails'} ({len(shacl['violations'])} results)")
    for issue in shacl["violations"]:
        print(f"  {issue.get('focus')} {issue.get('path', '')} {issue.get('constraint')} {' '.join(issue['messages'])}")
    print(f"Repository integrity: {integrity['violation_count']} violations")
    for issue in integrity["violations"]:
        print(f"  {issue['code']} {issue['focus']} {issue['message']}")
    if integrity["unregistered_declared_graphs"] and not fidelity["checked"]:
        print("Unregistered declared graphs (could be empty): " + ", ".join(integrity["unregistered_declared_graphs"]))
    if fidelity["checked"]:
        print(f"Source fidelity: {fidelity['equivalent_versions']}/{fidelity['checked_versions']} versions equivalent")
        for version in fidelity["versions"]:
            if not version["equivalent"]:
                print(f"  {version['version_iri']} {version.get('error', '')}")
                for key in ("scope_errors", "missing_ground_quads", "unexpected_ground_quads"):
                    for detail in version.get(key, ()):
                        print(f"    {key}: {detail}")
    else:
        print(f"Source fidelity: skipped. {fidelity['reason']}")
    if "competency_questions" in report:
        questions = report["competency_questions"]
        print(f"Competency questions: {questions['executed_successfully']}/{questions['question_count']} executed, "
              f"{questions['matches_expected']}/{questions['question_count']} match gold")
        for question in questions["questions"]:
            if not question["correct"]:
                print(f"  {question['id']} {question.get('error') or _json(question).strip()}")
        for format, result in report["round_trips"].items():
            print(f"{format} reload: " + ", ".join(f"{name}={value}" for name, value in result.items()))
        for case in report["negative_examples"]:
            print(f"Negative case {case['case']}: {'detected' if case['detected'] else 'missed'}")
        print("SQLite and historical snapshots unchanged: " + str(all(report["read_only_checks"].values())))


def main(argv=None):
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            paths = [path for path in (args.report_json, args.shacl_report) if path is not None]
            if len({path.resolve() for path in paths}) != len(paths):
                raise ValueError("Report output paths must be distinct")
            for path in paths:
                if path.exists() or path.is_symlink():
                    raise FileExistsError(f"Report output already exists: {path}")
            report, shacl = _validation_input(args)
            if args.report_json:
                _write_new(args.report_json, _json(report))
            if args.shacl_report:
                _write_new(args.shacl_report, shacl.serialize(format="turtle"))
            passed = report["conforms"]
        else:
            if args.output_dir and (args.output_dir.exists() or args.output_dir.is_symlink()):
                raise FileExistsError(f"Evaluation output directory already exists: {args.output_dir}")
            with TemporaryDirectory(prefix="oersi-evaluation-") as scratch:
                report, shacl, negative_shacl = run_benchmark(Path(scratch))
            if args.output_dir:
                args.output_dir.mkdir(exist_ok=False)
                _write_new(args.output_dir / "report.json", _json(report))
                _write_new(args.output_dir / "shacl-report.ttl", shacl.serialize(format="turtle"))
                _write_new(args.output_dir / "negative-shacl-report.ttl", negative_shacl.serialize(format="turtle"))
            passed = report["passed"]
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    _summarize(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
