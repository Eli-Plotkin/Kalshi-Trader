"""G2: games that tip off together run on parallel threads; the exposure cap must still hold."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import config, main
from nba_trading.ledger import Ledger
from nba_trading.portfolio import HighWaterMark, Portfolio
from nba_trading.tests.fakes import KalshiReadsMixin
from nba_trading.tests.test_main import FakeScheduler, _book


class TwoGameClient(KalshiReadsMixin):
    """Two simultaneous games: BOS/LAL and NYK/MIA. Favorites BOS and NYK both ask 70c.
    Cash is constant (like a resting order that has not reserved anything yet), and
    placing an order is slow, which widens the window for a check-then-act race."""

    def __init__(self, order_delay=0.3):
        self.order_delay = order_delay
        self.order_calls = []
        self._lock = threading.Lock()

    def get_balance(self):
        return 100_000

    def fetch_nba_markets(self):
        return [{"ticker": f"KXNBA-{g}-{t}"} for g, ts in (("G1", ("BOS", "LAL")), ("G2", ("NYK", "MIA")))
                for t in ts]

    def get_orderbook(self, ticker):
        return _book(0.30) if ticker.endswith(("BOS", "NYK")) else _book(0.70)

    def list_positions(self, raise_on_error=False):
        return []

    def place_limit_order(self, **kw):
        time.sleep(self.order_delay)
        with self._lock:
            self.order_calls.append(kw)
        return {"order_id": f"oid-{len(self.order_calls)}"}


def _games():
    tip = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    return [
        {"game_id": "2026-10-28-BOS-LAL", "home_team": "BOS", "away_team": "LAL", "tip_off_utc": tip},
        {"game_id": "2026-10-28-NYK-MIA", "home_team": "NYK", "away_team": "MIA", "tip_off_utc": tip},
    ]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FAVORITE_PRICE_MIN", 50)
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 90)
    monkeypatch.setattr(config, "BANKROLL_FRACTION", 0.20)
    monkeypatch.setattr(config, "LIMIT_PRICE_BUFFER_CENTS", 1)
    monkeypatch.setattr(config, "MAX_DRAWDOWN_PCT", 50)
    monkeypatch.setattr(config, "MAX_EXPOSURE_PCT", 25)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "STOP_TRADING"))
    monkeypatch.setattr(main, "mark_game_fired", lambda gid: None)
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    return {"portfolio": Portfolio(ledger=led), "high_water": HighWaterMark(ledger=led),
            "scheduler": FakeScheduler(), "ledger": led}


def _fire_together(world, client, games):
    threads = [threading.Thread(target=main.pre_game_job,
                                args=(client, world["portfolio"], world["high_water"],
                                      world["scheduler"], g, False))
               for g in games]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a job hung"


def test_simultaneous_games_cannot_exceed_the_exposure_cap(world):
    client = TwoGameClient()
    _fire_together(world, client, _games())

    open_cost = world["portfolio"].open_cost_cents()
    equity = 100_000 + open_cost
    assert len(client.order_calls) == 2                      # both still trade (second is sized down)
    assert open_cost <= config.MAX_EXPOSURE_PCT / 100 * equity + 1, (
        f"exposure {open_cost / equity:.1%} exceeds the {config.MAX_EXPOSURE_PCT}% cap")
    sizes = sorted(c["count"] for c in client.order_calls)
    assert sizes[0] < sizes[1]                               # the later game saw the earlier order


def test_without_serialisation_the_same_scenario_breaks_the_cap(world, monkeypatch):
    """Guards the guard: prove the test above can fail, by swapping in a no-op lock."""
    class NoLock:
        def acquire(self, timeout=None): return True
        def release(self): pass
    monkeypatch.setattr(main, "_TRADE_LOCK", NoLock())
    client = TwoGameClient()
    _fire_together(world, client, _games())
    open_cost = world["portfolio"].open_cost_cents()
    assert open_cost > config.MAX_EXPOSURE_PCT / 100 * (100_000 + open_cost)


def test_second_game_is_not_skipped_just_because_the_first_holds_the_lock(world):
    client = TwoGameClient(order_delay=0.2)
    _fire_together(world, client, _games())
    tickers = sorted(c["ticker"] for c in client.order_calls)
    assert tickers == ["KXNBA-G1-BOS", "KXNBA-G2-NYK"]


def test_lock_timeout_is_a_retryable_skip_not_a_trade(world, monkeypatch):
    monkeypatch.setattr(config, "TRADE_LOCK_TIMEOUT_SECONDS", 0.05)
    client = TwoGameClient()
    game = _games()[0]
    with main._TRADE_LOCK:                                   # another game "holds" the lock
        main.pre_game_job(client, world["portfolio"], world["high_water"], world["scheduler"],
                          game, False)
    assert client.order_calls == []
    assert len(world["scheduler"].jobs) == 1                 # retry queued
    reasons = [r["reason"] for r in world["ledger"].conn.execute("SELECT reason FROM decisions")]
    assert reasons == ["trade_lock_timeout"]


def test_lock_is_released_when_evaluation_raises(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(main, "_pre_game_body", boom)
    with pytest.raises(RuntimeError):
        main.pre_game_job(TwoGameClient(), world["portfolio"], world["high_water"],
                          world["scheduler"], _games()[0], False)
    assert main._TRADE_LOCK.acquire(blocking=False)          # not left locked forever
    main._TRADE_LOCK.release()


def test_portfolio_survives_concurrent_readers_and_writers():
    pf = Portfolio(ledger=Ledger(":memory:"))
    errors = []

    def writer(n):
        try:
            for i in range(200):
                pf.add_position(f"T{n}-{i}", 90, 1)
                if i % 5 == 0:
                    pf.reconcile({}, grace_seconds=0)
        except Exception as e:                               # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(600):
                pf.open_cost_cents()
                pf.has_any_positions()
        except Exception as e:                               # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=60)
    assert errors == []


def test_high_water_mark_updates_are_atomic_under_threads():
    hw = HighWaterMark(ledger=Ledger(":memory:"))
    threads = [threading.Thread(target=lambda v=v: hw.update(v, False)) for v in range(1000, 1200)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    assert hw.peak() == 1199                                 # the max wins, never a lower late write
