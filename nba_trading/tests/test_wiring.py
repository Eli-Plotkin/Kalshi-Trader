"""Alerts / dead-man's switch / crash / live-confirmation wiring in main.py."""

from __future__ import annotations

import pytest

from nba_trading import alerts, config, main
from nba_trading.ledger import Ledger
from nba_trading.tests.fakes import KalshiReadsMixin


@pytest.fixture()
def calls(monkeypatch):
    rec = {"notify": [], "ping": []}
    monkeypatch.setattr(alerts, "notify", lambda level, kind, msg: rec["notify"].append((level, kind, msg)))
    monkeypatch.setattr(alerts, "ping_healthcheck", lambda status="ok", url=None: rec["ping"].append(status))
    monkeypatch.setattr(alerts, "flush", lambda timeout=5.0: None)
    return rec


def test_event_pushes_an_alert_and_records_it(calls, monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    main._event("CRITICAL", "circuit_breaker", "drawdown 30%")
    assert calls["notify"] == [("CRITICAL", "circuit_breaker", "drawdown 30%")]
    assert led.conn.execute("SELECT kind FROM events").fetchone()[0] == "circuit_breaker"


def test_event_alerts_even_without_a_ledger(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", None)
    main._event("ERROR", "reconcile_failed", "x")
    assert calls["notify"] == [("ERROR", "reconcile_failed", "x")]


def test_heartbeat_events_do_not_push_alerts(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))
    main._event("INFO", "heartbeat", "ok")
    assert calls["notify"] == []


def test_alert_failure_cannot_break_event_recording(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    # alerts.notify itself never raises, but even the ledger path must not depend on it
    monkeypatch.setattr(alerts, "notify", lambda *a: False)
    main._event("ERROR", "x", "y")
    assert led.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


class _C(KalshiReadsMixin):
    def __init__(self, balance=1000):
        self.balance = balance

    def get_balance(self):
        return self.balance

    def get_order_status(self, oid): return {}
    def get_market(self, t): return {}
    def list_positions(self, raise_on_error=False): return []


class _P:
    """The slice of Portfolio the health job and run loop touch."""
    def reconcile(self, holdings, grace_seconds=1800, now=None, grace_overrides=None): return []
    def has_position(self, ticker): return False
    def exposure_cents(self, positions): return 0, []
    def open_cost_cents(self): return 0


class _HW:
    def update(self, *a): pass
    def peak(self): return 1000
    def drawdown_pct(self, e): return 0.0


def test_health_job_pings_ok_when_balance_fetch_works(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))
    main.health_job(_C(1000), _P(), _HW())
    assert calls["ping"] == ["ok"]


def test_health_job_pings_fail_when_api_is_unreachable(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))
    main.health_job(_C(None), _P(), _HW())
    assert calls["ping"] == ["fail"]
    assert any(k == "balance_fetch_failed" for _, k, _ in calls["notify"])


def test_health_job_pings_fail_on_exception(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))

    class Boom(_C):
        def get_balance(self):
            raise RuntimeError("down")

    main.health_job(Boom(), _P(), _HW())
    assert calls["ping"] == ["fail"]


def test_crash_alerts_and_exits_nonzero(calls, monkeypatch):
    monkeypatch.setattr(main, "LEDGER", Ledger(":memory:"))

    class FakeSched:
        def __init__(self, **kw): pass
        def start(self): pass
        def shutdown(self): pass
        def add_job(self, *a, **k): pass
        def add_listener(self, *a, **k): pass

    monkeypatch.setattr(main, "BackgroundScheduler", FakeSched)
    monkeypatch.setattr(main, "recover_orders", lambda *a, **k: (0, 0))
    monkeypatch.setattr(main, "health_job", lambda *a: None)
    monkeypatch.setattr(main, "_register_health_job", lambda *a: None)
    monkeypatch.setattr(main, "setup_daily_schedule", lambda *a, **k: None)

    def boom(_):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(main.time, "sleep", boom)
    hw = _HW()
    hw.update = lambda *a: None

    class P(_P):
        def has_any_positions(self): return False

    with pytest.raises(SystemExit) as exc:
        main._run_loop(_C(1000), P(), hw, dry_run=True)
    assert exc.value.code == 1
    assert any(k == "bot_crash" for _, k, _ in calls["notify"])


def test_live_confirmation_accepts_env_without_a_terminal(monkeypatch):
    monkeypatch.setenv("BOT_LIVE_CONFIRM", "LIVE")

    def no_terminal(_):
        raise EOFError

    assert main._confirm_live("prod", "https://x", input_fn=no_terminal) is True


@pytest.mark.parametrize("value", ["", "live", "yes", "TRUE"])
def test_live_confirmation_env_must_be_exactly_LIVE(monkeypatch, value):
    monkeypatch.setenv("BOT_LIVE_CONFIRM", value)

    def no_terminal(_):
        raise EOFError

    assert main._confirm_live("prod", "https://x", input_fn=no_terminal) is False


def test_alert_defaults_catch_errors_not_only_critical():
    assert config.ALERT_MIN_LEVEL.upper() == "ERROR"


def test_malformed_cooldown_env_does_not_crash_config(monkeypatch):
    monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", "banana")
    assert config._float_env("ALERT_COOLDOWN_MINUTES", 15) == 15.0


# --- health-job equity must not double count settled positions (architecture gap G1) ---

from nba_trading.portfolio import HighWaterMark, Portfolio


class _Kalshi(KalshiReadsMixin):
    def __init__(self, balance, positions=None, positions_error=None):
        self.balance, self.positions_error = balance, positions_error
        # KalshiReadsMixin reads `positions`; an Exception there makes the fetch fail.
        self.positions = positions_error if positions_error else (positions or [])

    def get_balance(self):
        return self.balance

    def list_positions(self, raise_on_error=False):
        if self.positions_error:
            raise self.positions_error
        return self.positions

    def get_order_status(self, oid): return {}
    def get_market(self, t): return {}


def _real_state(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    return led, Portfolio(ledger=led), HighWaterMark(ledger=led)


def _age_lots(pf, seconds=7200):
    """Make every lot look `seconds` old (Portfolio caches lots in memory and writes through)."""
    for lots in pf._positions.values():
        for lot in lots:
            lot["time"] -= seconds


def test_settled_position_is_not_double_counted_in_equity(calls, monkeypatch):
    led, pf, hw = _real_state(monkeypatch)
    pf.add_position("T1", 93, 100)                       # $93 at risk
    hw.update(100_000, False)                            # peak $1,000
    _age_lots(pf)
    # Game won: Kalshi holds nothing, cash is now $1,007 (payout 100 contracts - $93 cost + $100).
    client = _Kalshi(balance=100_700)
    main.health_job(client, pf, hw)
    assert not pf.has_position("T1")                     # settled lot cleared
    snap = dict(led.conn.execute("SELECT * FROM snapshots").fetchone())
    assert snap["open_cost_cents"] == 0 and snap["equity_cents"] == 100_700
    assert hw.peak() == 100_700                          # NOT 100_700 + 9_300


def test_unsettled_position_is_still_counted(calls, monkeypatch):
    led, pf, hw = _real_state(monkeypatch)
    pf.add_position("T1", 93, 100)
    _age_lots(pf)
    client = _Kalshi(balance=90_700, positions=[{"ticker": "T1", "position_fp": "100.00"}])
    main.health_job(client, pf, hw)
    snap = dict(led.conn.execute("SELECT * FROM snapshots").fetchone())
    assert snap["open_cost_cents"] == 9_300 and snap["equity_cents"] == 100_000


def test_recent_resting_order_is_not_dropped_by_health_reconcile(calls, monkeypatch):
    led, pf, hw = _real_state(monkeypatch)
    pf.add_position("T1", 93, 100)                       # just placed, no fill yet
    main.health_job(_Kalshi(balance=90_700), pf, hw)
    assert pf.has_position("T1")


def test_failed_position_fetch_records_no_equity_and_pings_fail(calls, monkeypatch):
    led, pf, hw = _real_state(monkeypatch)
    pf.add_position("T1", 93, 100)
    _age_lots(pf)
    hw.update(100_000, False)
    main.health_job(_Kalshi(balance=100_700, positions_error=RuntimeError("api down")), pf, hw)
    assert led.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert hw.peak() == 100_000                          # peak untouched
    assert calls["ping"] == ["fail"]
    assert pf.has_position("T1")                         # nothing wiped on a failed fetch
