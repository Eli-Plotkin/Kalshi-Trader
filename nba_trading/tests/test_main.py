"""Orchestration tests for nba_trading.main.pre_game_job.

Covers the real-money safety properties: dry-run never orders or persists,
client_order_id is deterministic, the game is marked fired only after the job
completes, and an ambiguous order failure is reconciled against Kalshi.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import config, main
from nba_trading.portfolio import HighWaterMark, Portfolio
from nba_trading.tests.fakes import KalshiReadsMixin, order_dict


def _book(no_bid_dollars):
    return {"no_dollars": [[str(no_bid_dollars), 10]]}


_DEFAULT = object()


class FakeClient(KalshiReadsMixin):
    def __init__(self, order_result=_DEFAULT, positions=None):
        self.order_result = {"order_id": "oid-1"} if order_result is _DEFAULT else order_result
        self.positions = positions or []
        self.order_calls = []

    def get_balance(self):
        return 100_000  # $1000

    def fetch_nba_markets(self):
        return [{"ticker": "KXNBA-G1-BOS"}, {"ticker": "KXNBA-G1-LAL"}]

    def get_orderbook(self, ticker):
        # BOS implied ask = 100 - 30 = 70; LAL ask = 100 - 70 = 30 -> BOS favorite
        return _book(0.30) if ticker.endswith("BOS") else _book(0.70)

    def place_limit_order(self, **kwargs):
        self.order_calls.append(kwargs)
        return self.order_result

    def list_positions(self, raise_on_error=False):
        if isinstance(self.positions, Exception):
            if raise_on_error:
                raise self.positions
            return []
        return self.positions


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, *a, **kw):
        self.jobs.append((a, kw))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FAVORITE_PRICE_MIN", 50)
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 90)
    monkeypatch.setattr(config, "BANKROLL_FRACTION", 0.05)
    monkeypatch.setattr(config, "LIMIT_PRICE_BUFFER_CENTS", 1)
    monkeypatch.setattr(config, "MAX_DRAWDOWN_PCT", 50)
    monkeypatch.setattr(config, "MAX_EXPOSURE_PCT", 30)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "STOP_TRADING"))
    fired = []
    monkeypatch.setattr(main, "mark_game_fired", fired.append)
    tip = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    game = {
        "game_id": "2026-10-27-BOS-LAL", "home_team": "BOS", "away_team": "LAL",
        "tip_off_utc": tip,
    }
    return {
        "portfolio": Portfolio(str(tmp_path / "p.json")),
        "high_water": HighWaterMark(str(tmp_path / "h.json")),
        "scheduler": FakeScheduler(), "fired": fired, "game": game,
    }


def _run(env, client, dry_run):
    main.pre_game_job(client, env["portfolio"], env["high_water"],
                      env["scheduler"], env["game"], dry_run)


def test_dry_run_places_no_order_and_persists_nothing(env):
    client = FakeClient()
    _run(env, client, dry_run=True)
    assert client.order_calls == []
    assert not env["portfolio"].has_any_positions()
    assert env["scheduler"].jobs == []
    assert env["fired"] == []


def test_dry_run_is_the_default():
    import inspect
    assert inspect.signature(main.pre_game_job).parameters["dry_run"].default is True
    assert inspect.signature(main.run_bot).parameters["dry_run"].default is True


def test_live_order_uses_deterministic_client_order_id(env):
    client = FakeClient()
    _run(env, client, dry_run=False)
    assert len(client.order_calls) == 1
    assert client.order_calls[0]["client_order_id"] == "nba-2026-10-27-BOS-LAL-KXNBA-G1-BOS"
    assert env["portfolio"].has_position("KXNBA-G1-BOS")
    assert env["fired"] == ["2026-10-27-BOS-LAL"]


def test_game_marked_fired_only_after_order_call(env, monkeypatch):
    order = []

    class Spy(FakeClient):
        def place_limit_order(self, **kw):
            order.append("order")
            return super().place_limit_order(**kw)

    monkeypatch.setattr(main, "mark_game_fired", lambda gid: order.append("fired"))
    _run(env, Spy(), dry_run=False)
    assert order == ["order", "fired"]


def test_ambiguous_failure_with_landed_position_is_recorded(env, caplog):
    client = FakeClient(order_result=None,
                        positions=[{"ticker": "KXNBA-G1-BOS", "position_fp": "7.00"}])
    with caplog.at_level(logging.CRITICAL):
        _run(env, client, dry_run=False)
    assert env["portfolio"].has_position("KXNBA-G1-BOS")
    assert "actually landed" in caplog.text


def test_ambiguous_failure_without_position_alerts_and_does_not_retry(env, caplog):
    client = FakeClient(order_result=None, positions=[])
    with caplog.at_level(logging.CRITICAL):
        _run(env, client, dry_run=False)
    assert len(client.order_calls) == 1
    assert not env["portfolio"].has_any_positions()
    assert "VERIFY MANUALLY" in caplog.text


def test_insufficient_funds_records_nothing(env):
    client = FakeClient(order_result="INSUFFICIENT_FUNDS")
    _run(env, client, dry_run=False)
    assert not env["portfolio"].has_any_positions()
    assert client.order_calls and env["fired"]


def test_duplicate_order_409_is_resolved_by_asking_kalshi_not_by_resending(env, caplog, monkeypatch):
    from nba_trading.ledger import Ledger
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))
    coid = "nba-2026-10-27-BOS-LAL-KXNBA-G1-BOS"
    client = FakeClient(order_result="DUPLICATE_ORDER")
    client.by_client_id = {coid: order_dict("oid-9", coid, filled=3, avg_fill_cents=71)}
    with caplog.at_level(logging.CRITICAL):
        _run(env, client, dry_run=False)
    assert len(client.order_calls) == 1                          # never re-sent
    assert env["portfolio"].has_position("KXNBA-G1-BOS")         # adopted from Kalshi's answer
    assert "ORDER RECOVERED" in caplog.text


# --- readiness: kill switch, exposure cap, equity breaker, reconcile ---


def test_kill_switch_blocks_live_order(env, tmp_path):
    (tmp_path / "STOP_TRADING").write_text("")
    client = FakeClient()
    _run(env, client, dry_run=False)
    assert client.order_calls == []


def test_exposure_cap_reduces_order_size(env, monkeypatch):
    monkeypatch.setattr(config, "BANKROLL_FRACTION", 0.20)
    monkeypatch.setattr(config, "MAX_EXPOSURE_PCT", 10)
    client = FakeClient()
    _run(env, client, dry_run=False)
    # equity $1000, cap 10% = $100; limit 71c -> 140 shares, not the 281 that 20% sizing wants
    assert client.order_calls[0]["count"] == 140


def test_no_order_when_exposure_already_at_cap(env):
    env["portfolio"].add_position("KXNBA-G0-NYK", 90, 500)   # $450 open; equity $1450, cap 30% = $435
    env["portfolio"].reconcile({"KXNBA-G0-NYK": 500})
    client = FakeClient()
    _run(env, client, dry_run=False)
    assert client.order_calls == []


def test_open_positions_do_not_trip_circuit_breaker(env, monkeypatch):
    # Peak $1000. Cash falls to $900 because $100 is sitting in an open position:
    # equity is still $1000, so this must NOT look like a 10% drawdown.
    monkeypatch.setattr(config, "MAX_DRAWDOWN_PCT", 5)
    env["high_water"].update(100_000, False)
    env["portfolio"].add_position("KXNBA-G0-NYK", 50, 200)    # $100 cost
    env["portfolio"].reconcile({"KXNBA-G0-NYK": 200})
    client = FakeClient()
    client.get_balance = lambda: 90_000
    _run(env, client, dry_run=False)
    assert len(client.order_calls) == 1


def test_real_loss_trips_circuit_breaker(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_DRAWDOWN_PCT", 5)
    env["high_water"].update(100_000, False)
    client = FakeClient()
    client.get_balance = lambda: 80_000        # genuinely down 20%
    _run(env, client, dry_run=False)
    assert client.order_calls == []


def test_settled_positions_are_reconciled_away(env):
    env["portfolio"].add_position("KXNBA-OLD-XXX", 90, 10)
    env["portfolio"]._positions["KXNBA-OLD-XXX"][0]["time"] -= 7200   # placed 2h ago
    client = FakeClient(positions=[])           # Kalshi holds nothing -> it settled
    _run(env, client, dry_run=False)
    assert not env["portfolio"].has_position("KXNBA-OLD-XXX")


def test_failed_position_fetch_blocks_live_trading(env):
    env["portfolio"].add_position("KXNBA-OLD-XXX", 90, 10)
    client = FakeClient(positions=RuntimeError("api down"))
    _run(env, client, dry_run=False)
    assert client.order_calls == []
    assert env["portfolio"].has_position("KXNBA-OLD-XXX")     # state not wiped


def test_recent_unfilled_order_is_not_reconciled_away(env):
    env["portfolio"].add_position("KXNBA-NEW-XXX", 90, 10)    # placed just now
    assert env["portfolio"].reconcile({}) == []


class _FillClient(KalshiReadsMixin):
    def __init__(self, order=None, positions=(), error=None):
        self._order, self.positions, self._error = order, list(positions), error

    def get_order(self, order_id):
        if self._error:
            raise self._error
        return self._order


def test_confirm_fill_never_deletes_a_position_kalshi_still_holds(env):
    """A payload with a missing fill field parses as filled=0; Kalshi's positions must win."""
    env["portfolio"].add_position("T", 92, 5)
    client = _FillClient(order_dict("oid", ticker="T", status="executed", filled=0),
                         positions=[{"ticker": "T", "position_fp": "5.00"}])
    main.confirm_fill_job(client, env["portfolio"], "T", "oid", 5)
    assert env["portfolio"].has_position("T")


def test_confirm_fill_drops_an_expired_unfilled_lot(env):
    env["portfolio"].add_position("T", 92, 5)
    env["portfolio"]._positions["T"][0]["time"] -= 7200
    client = _FillClient(order_dict("oid", ticker="T", status="canceled", filled=0), positions=[])
    main.confirm_fill_job(client, env["portfolio"], "T", "oid", 5)
    assert not env["portfolio"].has_position("T")


def test_confirm_fill_partial_fill_shrinks_the_lot_to_what_kalshi_holds(env):
    env["portfolio"].add_position("T", 92, 5)
    client = _FillClient(order_dict("oid", ticker="T", status="canceled", filled=3),
                         positions=[{"ticker": "T", "position_fp": "3.00"}])
    main.confirm_fill_job(client, env["portfolio"], "T", "oid", 5)
    assert env["portfolio"]._positions["T"][-1]["qty"] == 3


def test_confirm_fill_survives_a_failed_lookup(env, caplog):
    env["portfolio"].add_position("T", 92, 5)
    with caplog.at_level(logging.ERROR):
        main.confirm_fill_job(_FillClient(error=RuntimeError("api down")), env["portfolio"], "T", "oid", 5)
    assert env["portfolio"].has_position("T") and "FILL CHECK FAILED" in caplog.text


def test_confirm_live_requires_exact_word(capsys):
    assert main._confirm_live("prod", "https://x", input_fn=lambda _: "LIVE") is True
    assert main._confirm_live("prod", "https://x", input_fn=lambda _: "live") is False
    assert main._confirm_live("prod", "https://x", input_fn=lambda _: "") is False


def test_confirm_live_refuses_when_no_stdin(capsys):
    def eof(_):
        raise EOFError
    assert main._confirm_live("prod", "https://x", input_fn=eof) is False


def test_location_attestation_failure_is_recorded_and_not_retried(env, caplog):
    from nba_trading.ledger import Ledger
    led = Ledger(":memory:")
    main.LEDGER = led
    try:
        client = FakeClient(order_result="LOCATION_ATTESTATION")
        _run(env, client, dry_run=False)
        assert len(client.order_calls) == 1
        assert not env["portfolio"].has_any_positions()
        kinds = [r["kind"] for r in led.conn.execute("SELECT kind FROM events")]
        outcomes = [r["outcome"] for r in led.conn.execute("SELECT outcome FROM orders")]
        assert "location_attestation" in kinds and outcomes == ["blocked_location"]
    finally:
        main.LEDGER = None


# --- schedule fetch failure / stable ids ---


def _setup(monkeypatch, tmp_path, games, now=None):
    monkeypatch.setattr(config, "SCHEDULE_FILE", str(tmp_path / "sched.json"))
    monkeypatch.setattr(main.nba_scheduler, "get_schedule", lambda client: games)
    saved = {}
    monkeypatch.setattr(main, "save_schedule", lambda sch: saved.update(sch))
    monkeypatch.setattr(main, "load_schedule", lambda: {})

    class C:
        def fetch_nba_markets(self):
            return [{"ticker": "KXNBA-G1-BOS"}, {"ticker": "KXNBA-G1-LAL"}]
    return C(), saved


def test_failed_schedule_fetch_is_not_saved_as_an_empty_day(env, monkeypatch, tmp_path):
    client, saved = _setup(monkeypatch, tmp_path, games=None)
    main.setup_daily_schedule(client, env["portfolio"], env["high_water"], env["scheduler"], True)
    assert saved == {}                                           # nothing persisted
    assert len(env["scheduler"].jobs) == 1                       # retry scheduled
    assert env["scheduler"].jobs[0][1]["id"] == "schedule-retry"


def test_genuinely_empty_schedule_is_saved(env, monkeypatch, tmp_path):
    client, saved = _setup(monkeypatch, tmp_path, games=[])
    main.setup_daily_schedule(client, env["portfolio"], env["high_water"], env["scheduler"], True)
    assert saved["games"] == {} and env["scheduler"].jobs == []


def test_game_id_is_stable_across_utc_midnight(env, monkeypatch, tmp_path):
    # 10pm ET tip on Oct 27 = 02:00 UTC Oct 28. The id must use the ET date.
    tip = (datetime.now(timezone.utc) + timedelta(hours=3))
    game = {"homeTeam": {"teamTricode": "BOS"}, "awayTeam": {"teamTricode": "LAL"},
            "gameTimeUTC": tip.strftime("%Y-%m-%dT%H:%M:%SZ")}
    client, saved = _setup(monkeypatch, tmp_path, games=[game])
    main.setup_daily_schedule(client, env["portfolio"], env["high_water"], env["scheduler"], True)
    (gid,) = saved["games"]
    assert gid == f"{tip.astimezone(main.EASTERN).date().isoformat()}-BOS-LAL"


def test_already_fired_game_is_not_rescheduled(env, monkeypatch, tmp_path):
    tip = datetime.now(timezone.utc) + timedelta(hours=3)
    game = {"homeTeam": {"teamTricode": "BOS"}, "awayTeam": {"teamTricode": "LAL"},
            "gameTimeUTC": tip.strftime("%Y-%m-%dT%H:%M:%SZ")}
    client, saved = _setup(monkeypatch, tmp_path, games=[game])
    gid = f"{tip.astimezone(main.EASTERN).date().isoformat()}-BOS-LAL"
    monkeypatch.setattr(main, "load_schedule", lambda: {"date": "old", "games": {gid: {"fired": True}}})
    main.setup_daily_schedule(client, env["portfolio"], env["high_water"], env["scheduler"], False)
    assert env["scheduler"].jobs == []                    # not registered again
    assert saved["games"][gid]["fired"] is True           # flag preserved


def test_in_band_but_unfunded_is_recorded_with_ask_and_zero_size(env, monkeypatch):
    from nba_trading.ledger import Ledger
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    client = FakeClient()
    client.get_balance = lambda: 9                      # the real 9-cent account
    _run(env, client, dry_run=True)
    row = dict(led.conn.execute("SELECT * FROM decisions").fetchone())
    assert row["action"] == "skip" and row["reason"] == "in_band_unfunded"
    assert row["ask_cents"] == 70 and row["ticker"] == "KXNBA-G1-BOS" and row["dry_run"] == 1
    assert row["equity_cents"] == 9 and row["shares"] is None
    assert client.order_calls == []
