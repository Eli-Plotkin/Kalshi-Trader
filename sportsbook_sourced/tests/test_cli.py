"""Tests for the sportsbook_sourced CLI scaffold."""

from __future__ import annotations

import json

import pytest

from sportsbook_sourced import cli


def test_scan_subcommand_succeeds(capsys, monkeypatch, tmp_path):
    # Redirect DB to tmp so the test doesn't touch the repo's data/ dir.
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "scan.sqlite")
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)

    rc = cli.main(["scan"])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "scaffold_ready"
    assert payload["dry_run"] is True
    assert payload["league"] == "nba"


def test_scan_respects_league_flag(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "scan.sqlite")
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)

    rc = cli.main(["scan", "--league", "nfl"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["league"] == "nfl"


def test_invalid_league_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "scan.sqlite")
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["scan", "--league", "mlb"])  # not in choices


def test_scan_reports_odds_api_unconfigured_when_env_missing(
    capsys, monkeypatch, tmp_path
):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "scan.sqlite")
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)
    cli.main(["scan"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["odds_api_configured"] is False


def test_scan_reports_odds_api_configured_when_env_set(
    capsys, monkeypatch, tmp_path
):
    monkeypatch.setenv("ODDS_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "scan.sqlite")
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)
    cli.main(["scan"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["odds_api_configured"] is True


def test_missing_subcommand_exits_with_error():
    with pytest.raises(SystemExit):
        cli.main([])


def test_scan_creates_db_file(monkeypatch, tmp_path):
    db_path = tmp_path / "scan.sqlite"
    monkeypatch.setattr(cli.storage, "DB_PATH", db_path)
    monkeypatch.setattr(cli.storage, "DATA_DIR", tmp_path)
    cli.main(["scan"])
    assert db_path.exists()
