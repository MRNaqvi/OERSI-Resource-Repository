from __future__ import annotations

import json
import sys

import src.repository_cli as cli


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
