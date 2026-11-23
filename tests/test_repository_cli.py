from __future__ import annotations

import json
import sys

import src.repository_cli as cli
from tests.test_repository import make_record, make_repository
from tests.test_version_management import MATERIAL, when, with_normalized


def test_init_and_stats_commands_work_with_temporary_database(tmp_path, monkeypatch, capsys):
    database = tmp_path / "repo.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        ["repository_cli", "--database", str(database), "--base-iri", "https://example.org/oersi/", "init"],
    )

    assert cli.main() == 0
    init_result = json.loads(capsys.readouterr().out)
    assert init_result["status"] == "initialized"
    assert len(init_result["containers"]) == 2

    monkeypatch.setattr(
        sys,
        "argv",
        ["repository_cli", "--database", str(database), "--base-iri", "https://example.org/oersi", "stats"],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["containers"] == 2


def test_history_and_review_cli_commands(tmp_path, monkeypatch, capsys):
    database = tmp_path / "repo.sqlite"
    repository = make_repository(database)
    source, normalized = make_record()
    identifier = normalized["stable_id"]
    repository.import_resource(source, normalized, observed_at=when(1))
    repository.import_resource(*with_normalized(source, description="Changed"), observed_at=when(2))
    repository.import_resource(*with_normalized(source, learningResourceType=MATERIAL), observed_at=when(3))
    monkeypatch.setattr(cli, "Repository", lambda *args, **kwargs: repository)

    def run(*arguments):
        monkeypatch.setattr(sys, "argv", ["repository_cli", "--database", str(database), *arguments])
        exit_code = cli.main()
        return exit_code, json.loads(capsys.readouterr().out)

    assert [version["version_number"] for version in run("history", identifier)[1]] == [1, 2]
    first = run("get-version", identifier, "--version", "1")[1]
    assert run("get-version", first["version_iri"]) == (0, first)
    assert run("at-time", identifier, "--timestamp", when(1)) == (0, first)
    assert run("compare", identifier, "--from", "1", "--to", "2")[1]["original_jsonld"]["changed"]
    pending = run("pending")[1]
    update_id = str(pending[0]["update_id"])
    assert run("review", update_id)[1]["current_version"]["version_number"] == 2
    assert run("accept", update_id)[1]["status"] == "accepted"
    assert run("pending") == (0, [])
    assert run("pending", "--all")[1][0]["status"] == "accepted"
    assert run("reject", update_id)[0] == 1

    rejected = with_normalized(source, name="Rejected proposal")
    repository.import_resource(*rejected, observed_at=when(4))
    reject_id = str(run("pending")[1][0]["update_id"])
    assert run("reject", reject_id)[1]["status"] == "rejected"
    assert run("get", identifier)[1]["current_version"]["version_number"] == 3
