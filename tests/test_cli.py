from __future__ import annotations

import sys

import src.cli as cli


def _report(invalid: int = 0, context_failures: int = 0, extraction_errors: int = 0):
    return {
        "fetched_records": 1,
        "unique_records": 1,
        "successfully_classified_courses": 1,
        "successfully_classified_learning_materials": 0,
        "unclassified_records": 0,
        "extraction_error_count": extraction_errors,
        "extraction_errors": [],
        "jsonld_validation": {
            "validated_documents": 1 - invalid - context_failures,
            "invalid_jsonld_documents": invalid,
            "context_resolution_failures": context_failures,
            "results": [],
        },
    }


class FakeExtractor:
    report = _report()

    def __init__(self, **kwargs):
        pass

    def run(self):
        return self.report


class FakeClient:
    def __init__(self, **kwargs):
        pass


def _run_cli(monkeypatch, report):
    monkeypatch.setattr(cli, "OERSIClient", FakeClient)
    monkeypatch.setattr(cli, "Extractor", FakeExtractor)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(sys, "argv", ["src.cli"])
    FakeExtractor.report = report
    return cli.main()


def test_cli_exits_zero_when_validation_succeeds(monkeypatch, capsys):
    assert _run_cli(monkeypatch, _report()) == 0
    assert '"invalid_jsonld_documents": 0' in capsys.readouterr().out


def test_cli_exits_nonzero_for_invalid_jsonld(monkeypatch, capsys):
    assert _run_cli(monkeypatch, _report(invalid=1)) == 1
    assert '"invalid_jsonld_documents": 1' in capsys.readouterr().out


def test_cli_exits_nonzero_for_context_resolution_failure(monkeypatch, capsys):
    assert _run_cli(monkeypatch, _report(context_failures=1)) == 1
    assert '"context_resolution_failures": 1' in capsys.readouterr().out
