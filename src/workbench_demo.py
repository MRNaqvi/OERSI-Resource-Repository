"""Create a disposable Phase 5 benchmark database for the Phase 6 workbench."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from src.classification import classify_resource
from src.competency_evaluation import FIXTURES, seed_benchmark
from src.metadata import normalize_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for the demo database and migration backups")
    directory = parser.parse_args().output.resolve()
    directory.mkdir(exist_ok=False)
    backup_dir = directory / "backups"
    backup_dir.mkdir()
    repository, contexts = seed_benchmark(directory / "benchmark.sqlite3")
    # Keep the benchmark's seven versions and add one simulated pending proposal.
    data = next(row for row in repository.export_snapshot()["resources"]
                if row["source_iri"] == "https://example.test/oersi-benchmark/course")
    source = data["versions"][0]["original_jsonld"]
    source = {**source, "learningResourceType": [{"id": "https://w3id.org/kim/hcrt/textbook"}]}
    normalized = normalize_metadata(source)
    category, reason = classify_resource(source)
    normalized["classification"] = {"container": category, "reason": reason}
    repository.import_resource(source, normalized, observed_at="2026-01-20T12:00:00Z")
    environment = {
        "OERSI_REPOSITORY_DATABASE": repository.database_path,
        "OERSI_REPOSITORY_BASE_IRI": repository.base_iri,
        "OERSI_REPOSITORY_VOCABULARY_IRI": repository.vocabulary_iri,
        "OERSI_RDF_CONTEXT_SNAPSHOT": FIXTURES / "amb-snapshot",
        "OERSI_RDF_CONTEXT_SHA256": contexts.sha256,
        "OERSI_WORKBENCH_BACKUP_DIR": backup_dir,
    }
    for name, value in environment.items():
        print(f"export {name}={shlex.quote(str(value))}")
    print("python -m streamlit run streamlit_app.py")


if __name__ == "__main__":
    main()
