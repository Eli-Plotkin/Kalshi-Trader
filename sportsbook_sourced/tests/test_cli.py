"""Tests for the sportsbook_sourced CLI.

`--live`'s success path is always exercised through a monkeypatched
`cli._live_sources` -- never the real one. This repo's root `.env` carries
*real* Kalshi credentials (loaded via `kalshi.config`'s `load_dotenv()`), so
a test that let `--live` run for real could construct a working Kalshi
client. `cli._live_sources` reads credentials directly from `os.environ`
rather than importing `kalshi.config`, specifically so the missing-credential
paths are safe and deterministic to test with `monkeypatch.delenv`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sportsbook_sourced import cli
from sportsbook_sourced.kalshi_feed import snapshot_from_market
from sportsbook_sourced.schemas import SportsbookEvent, SportsbookOdds


def _run(*args):
    """Run cli.main and capture (returncode, parsed JSON payload)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(args))
    return rc, json.loads(buf.getvalue())


def _fresh_live_fixture(league="nba", event_id="live-e1"):
    """One event/market/odds set anchored to "now" (not the fixed dry-run
    sample), so a fake `_live_sources` produces a real, non-stale scan."""
    now = datetime.now(timezone.utc)
    commence = now + timedelta(hours=2)
    event = SportsbookEvent(
        event_id=event_id, league=league, home_team="Los Angeles Lakers",
        away_team="Boston Celtics", commence_time=commence,
    )
    market = snapshot_from_market(market={
        # No date component in the ticker (see game_date_from_ticker):
        # this fixture only needs to exercise CLI orchestration, not the
        # mapper's date-matching signal, so it isn't worth depending on
        # strftime("%b")'s locale-dependent month abbreviation for a ticker
        # that's never actually checked against a real calendar day.
        "ticker": "KXNBAGAME-XYZ-LAL",
        "title": "Los Angeles Lakers vs Boston Celtics Winner",
        "yes_sub_title": "Lakers",
        "close_time": (commence + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "yes_bid_dollars": "0.55",
        "yes_ask_dollars": "0.57",
    }, collected_at=now)
    quotes = {"draftkings": (185, -220), "fanduel": (190, -225)}
    odds = []
    for bookmaker, (home_price, away_price) in quotes.items():
        for name, price in ((event.home_team, home_price), (event.away_team, away_price)):
            odds.append(SportsbookOdds(
                event_id=event.event_id, bookmaker=bookmaker, market_type="moneyline",
                outcome_name=name, american_odds=price,
                last_update=now - timedelta(seconds=30), collected_at=now,
            ))
    return [market], [event], odds


def _set_fake_live_credentials(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("API_KEY_ID", "fake-id-for-test")
    monkeypatch.setenv("PRIVATE_KEY_PATH", "fake-path-for-test")


def _table_count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# dry-run (the default)
# ----------------------------------------------------------------------------


def test_scan_dry_run_is_the_default_and_actually_scans():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["scan"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["mode"] == "dry_run"
    assert payload["league"] == "nba"
    assert payload["markets_examined"] == 1
    assert "scaffold" not in buf.getvalue().lower()


def test_scan_dry_run_finds_the_sample_mispricing():
    _, payload = _run("scan")
    assert payload["opportunities_found"] == 1
    assert payload["tradeable"] == 1
    assert payload["opportunities"][0]["action"] == "buy"


def test_scan_dry_run_does_not_touch_the_real_db_by_default(monkeypatch, tmp_path):
    db_path = tmp_path / "should_not_exist.sqlite"
    monkeypatch.setattr(cli.storage, "DB_PATH", db_path)
    cli.main(["scan"])
    assert not db_path.exists()


def test_db_path_flag_overrides_the_dry_run_default_and_actually_persists(tmp_path):
    """File existence alone would also pass if the schema were created but
    nothing downstream ever ran -- check the real row landed, not just that
    a file happens to exist."""
    db_path = tmp_path / "explicit.sqlite"
    _, payload = _run("scan", "--db-path", str(db_path))
    assert db_path.exists()
    assert payload["db_path"] == str(db_path)
    assert _table_count(db_path, "opportunities") == 1
    assert _table_count(db_path, "sportsbook_events") == 1


def test_dry_run_flag_is_accepted_as_a_no_op():
    """--dry-run is already the default; passing it explicitly must not
    error and must not change behavior (it has no way to turn dry-run off,
    unlike the old `default=True` scaffold flag)."""
    rc, payload = _run("scan", "--dry-run")
    assert rc == 0
    assert payload["mode"] == "dry_run"


def test_live_flag_wins_when_both_dry_run_and_live_are_passed(monkeypatch, tmp_path):
    """--dry-run is a deprecated no-op, not a way to force dry-run back on
    over an explicit --live -- if a script or muscle memory passes both,
    --live must still take effect."""
    _set_fake_live_credentials(monkeypatch)
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "unused.sqlite")
    monkeypatch.setattr(cli, "_live_sources", lambda league: _fresh_live_fixture(league))
    _, payload = _run("scan", "--dry-run", "--live")
    assert payload["mode"] == "live"


def test_dry_run_rejects_unsupported_leagues():
    """Sample data is NBA only; NFL dry-run would silently return nothing
    useful, so it's a clear error instead."""
    with pytest.raises(SystemExit, match="NBA only"):
        cli.main(["scan", "--league", "nfl"])


def test_invalid_league_rejected():
    with pytest.raises(SystemExit):
        cli.main(["scan", "--league", "mlb"])  # not in argparse choices


def test_missing_subcommand_exits_with_error():
    with pytest.raises(SystemExit):
        cli.main([])


# ----------------------------------------------------------------------------
# --live
# ----------------------------------------------------------------------------


def test_live_requires_odds_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    db_path = tmp_path / "should_not_exist.sqlite"
    monkeypatch.setattr(cli.storage, "DB_PATH", db_path)
    with pytest.raises(SystemExit, match="ODDS_API_KEY"):
        cli.main(["scan", "--live"])
    assert not db_path.exists(), (
        "a credential failure must not have the side effect of creating "
        "the real production database"
    )


def test_live_requires_kalshi_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDS_API_KEY", "fake-key-for-test")
    monkeypatch.delenv("API_KEY_ID", raising=False)
    monkeypatch.delenv("PRIVATE_KEY_PATH", raising=False)
    db_path = tmp_path / "should_not_exist.sqlite"
    monkeypatch.setattr(cli.storage, "DB_PATH", db_path)
    with pytest.raises(SystemExit, match="API_KEY_ID"):
        cli.main(["scan", "--live"])
    assert not db_path.exists()


def test_live_never_reaches_the_dry_run_sample_restriction(monkeypatch, tmp_path):
    """--live's NBA-only guard is dry-run-specific; --live must not raise
    it even for nfl, since _live_sources (mocked here) is what's actually
    responsible for league support once real fetching exists."""
    # No credentials set and no monkeypatch of _live_sources: this must fail
    # on the credential check, not on the dry-run league restriction, proving
    # the two code paths are genuinely independent.
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY_ID", raising=False)
    monkeypatch.delenv("PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "should_not_exist.sqlite")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["scan", "--live", "--league", "nfl"])
    assert "NBA only" not in str(exc_info.value)


def test_live_actually_supports_a_non_nba_league(monkeypatch, tmp_path):
    """The NBA-only restriction is real for dry-run's canned sample, but
    --live has no such limit -- it must genuinely succeed for nfl once
    real fetching is (mocked to be) in place, not merely fail differently."""
    _set_fake_live_credentials(monkeypatch)
    monkeypatch.setattr(cli.storage, "DB_PATH", tmp_path / "unused.sqlite")
    seen_leagues = []

    def fake_live_sources(league):
        seen_leagues.append(league)
        return _fresh_live_fixture(league)

    monkeypatch.setattr(cli, "_live_sources", fake_live_sources)
    rc, payload = _run("scan", "--live", "--league", "nfl")
    assert rc == 0
    assert payload["league"] == "nfl"
    assert seen_leagues == ["nfl"], "the requested league must be forwarded to _live_sources"


def test_live_uses_the_mocked_source_and_the_real_db_by_default(monkeypatch, tmp_path):
    _set_fake_live_credentials(monkeypatch)
    db_path = tmp_path / "live.sqlite"
    monkeypatch.setattr(cli.storage, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_live_sources", lambda league: _fresh_live_fixture(league))

    rc, payload = _run("scan", "--live")
    assert rc == 0
    assert payload["mode"] == "live"
    assert payload["markets_examined"] == 1
    assert payload["opportunities_found"] == 1
    assert payload["tradeable"] == 1
    assert payload["db_path"] == str(db_path)
    assert db_path.exists()
    assert _table_count(db_path, "opportunities") == 1


def test_db_path_flag_overrides_the_live_default_too(monkeypatch, tmp_path):
    _set_fake_live_credentials(monkeypatch)
    monkeypatch.setattr(cli, "_live_sources", lambda league: _fresh_live_fixture(league))
    override_path = tmp_path / "override.sqlite"

    _, payload = _run("scan", "--live", "--db-path", str(override_path))
    assert override_path.exists()
    assert payload["db_path"] == str(override_path)
    assert _table_count(override_path, "opportunities") == 1
