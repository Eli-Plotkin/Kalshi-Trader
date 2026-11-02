"""SQLite state store (portfolio lots, high-water mark, schedule), legacy import,
misfire options, single-instance lock and the startup clock-skew check.

Everything uses tmp_path files / fakes: no real state files, no network.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import config, main
from nba_trading.ledger import InstanceLock, InstanceLockError, Ledger
from nba_trading.portfolio import HighWaterMark, Portfolio
from nba_trading.tests.test_main import FakeScheduler


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "state.sqlite")


# --- round trips -------------------------------------------------------------


def test_portfolio_round_trip_and_reconcile_persist(db):
    p = Portfolio(ledger=db)
    p.add_position("T1", 70, 10)
    p.add_position("T1", 72, 5)
    p.add_position("T2", 60, 3)
    p.update_position_qty("T1", 4)
    p2 = Portfolio(ledger=db)
    assert [l["qty"] for l in p2._positions["T1"]] == [10, 4]
    assert p2.open_cost_cents() == 70 * 10 + 72 * 4 + 60 * 3
    assert p2.has_position("T2") and p2.has_any_positions()

    # reconcile drops T2 (old, not held) and persists that + a qty correction
    dropped = p2.reconcile({"T1": 20}, now=p2._positions["T2"][0]["time"] + 7200)
    assert dropped == ["T2"]
    p3 = Portfolio(ledger=db)
    assert not p3.has_position("T2")
    assert sum(l["qty"] for l in p3._positions["T1"]) == 20
    p3.remove_position("T1")
    assert not Portfolio(ledger=db).has_any_positions()


def test_high_water_round_trip(db):
    hw = HighWaterMark(ledger=db)
    assert hw.peak() is None
    hw.update(10_000, False)
    hw.update(9_000, False)
    hw.update(12_000, True)           # blocked by open positions
    assert HighWaterMark(ledger=db).peak() == 10_000


def test_schedule_round_trip_and_fired_flags(db, monkeypatch):
    led = Ledger(db)
    monkeypatch.setattr(main, "LEDGER", led)
    assert main.load_schedule() == {}
    sched = {"date": "2026-10-28", "games": {
        "g1": {"game_id": "g1", "home_team": "BOS", "away_team": "LAL",
               "tip_off_utc": "2026-10-28T23:00:00Z", "trigger_time_utc": "2026-10-28T22:45:00+00:00",
               "fired": False},
        "g2": {"game_id": "g2", "home_team": "NYK", "away_team": "MIA",
               "tip_off_utc": "2026-10-29T00:00:00Z", "trigger_time_utc": "2026-10-28T23:45:00+00:00",
               "fired": True}}}
    main.save_schedule(sched)
    assert main.load_schedule() == sched
    assert list(main.load_schedule()["games"]) == ["g1", "g2"]      # order kept
    main.mark_game_fired("g1")
    main.mark_game_fired("nope")                                     # unknown id: no-op
    loaded = main.load_schedule()
    assert loaded["games"]["g1"]["fired"] is True
    # an empty day is distinguishable from "nothing saved"
    main.save_schedule({"date": "2026-10-29", "games": {}})
    assert main.load_schedule() == {"date": "2026-10-29", "games": {}}


def test_fired_flag_is_never_unset_by_a_save(db):
    led = Ledger(db)
    game = {"game_id": "g", "home_team": "A", "away_team": "B", "tip_off_utc": "t",
            "trigger_time_utc": "t", "fired": False}
    led.replace_schedule({"date": "d", "games": {"g": dict(game)}})
    led.mark_game_fired("g")
    led.replace_schedule({"date": "d", "games": {"g": dict(game)}})   # stale in-memory copy
    assert led.get_schedule()["games"]["g"]["fired"] is True


def test_setup_daily_schedule_carries_over_fired_flag_via_sqlite(db, monkeypatch):
    led = Ledger(db)
    monkeypatch.setattr(main, "LEDGER", led)
    tip = datetime.now(timezone.utc) + timedelta(hours=3)
    gid = f"{tip.astimezone(main.EASTERN).date().isoformat()}-BOS-LAL"
    led.replace_schedule({"date": "old", "games": {gid: {"game_id": gid, "fired": True}}})
    game = {"homeTeam": {"teamTricode": "BOS"}, "awayTeam": {"teamTricode": "LAL"},
            "gameTimeUTC": tip.strftime("%Y-%m-%dT%H:%M:%SZ")}
    monkeypatch.setattr(main.nba_scheduler, "get_schedule", lambda client: [game])

    class C:
        def fetch_nba_markets(self):
            return [{"ticker": "X-BOS"}, {"ticker": "X-LAL"}]
    sched = FakeScheduler()
    main.setup_daily_schedule(C(), Portfolio(ledger=led), HighWaterMark(ledger=led), sched, False)
    assert sched.jobs == []                                    # not registered again
    assert led.get_schedule()["games"][gid]["fired"] is True
    assert led.get_schedule()["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_state_visible_through_sibling_connection_same_file(db):
    # Litestream/dashboard-style reader sees committed state.
    Portfolio(ledger=db).add_position("T", 50, 2)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT ticker, price, qty FROM portfolio_lots").fetchall() == [("T", 50, 2)]


# --- atomicity ---------------------------------------------------------------


def test_replace_portfolio_is_all_or_nothing(db):
    led = Ledger(db)
    led.replace_portfolio_lots({"A": [{"price": 1, "qty": 1, "time": 1.0}]})
    bad = {"B": [{"price": 2, "qty": 2, "time": 2.0}],
           "C": [{"price": None, "qty": 3, "time": 3.0}]}       # violates NOT NULL mid-way
    with pytest.raises(sqlite3.IntegrityError):
        led.replace_portfolio_lots(bad)
    assert led.load_portfolio_lots() == {"A": [{"price": 1, "qty": 1, "time": 1.0}]}


def test_replace_schedule_is_all_or_nothing(db):
    led = Ledger(db)
    led.replace_schedule({"date": "d1", "games": {"g1": {"fired": False}}})
    # g2 inserts fine, g3 fails to bind -> the DELETE and g2 must roll back too
    bad = {"date": "d2", "games": {"g2": {"fired": False}, "g3": {"home_team": {"not": "bindable"}}}}
    with pytest.raises(sqlite3.Error):
        led.replace_schedule(bad)
    assert led.get_schedule()["date"] == "d1"
    assert list(led.get_schedule()["games"]) == ["g1"]


def test_portfolio_save_failure_is_logged_not_raised(db, caplog):
    p = Portfolio(ledger=db)
    p.add_position("T", 50, 1)
    p._positions["T"][0]["price"] = None
    with caplog.at_level(logging.ERROR):
        p.update_position_qty("T", 2)
    assert "Error saving portfolio" in caplog.text
    assert Portfolio(ledger=db)._positions["T"][0]["price"] == 50      # DB untouched


# --- one-time legacy import --------------------------------------------------


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def test_legacy_import_into_empty_db_leaves_json_untouched(tmp_path, db):
    pf = _write(tmp_path / "portfolio_data.json",
                {"T1": [{"price": 70, "qty": 9, "time": 123.0}]})
    hf = _write(tmp_path / "high_water_mark.json", {"peak_cents": 55_000})
    sf = _write(tmp_path / "schedule_data.json", {"date": "2026-10-28", "games": {
        "g": {"game_id": "g", "home_team": "BOS", "away_team": "LAL", "tip_off_utc": "t",
              "trigger_time_utc": "u", "fired": True}}})
    before = {p: (tmp_path / p).read_bytes() for p in
              ("portfolio_data.json", "high_water_mark.json", "schedule_data.json")}

    led = Ledger(db)
    p = Portfolio(filename=pf, ledger=led)
    hw = HighWaterMark(filename=hf, ledger=led)
    assert led.import_legacy_schedule(sf) is True
    assert p._positions == {"T1": [{"price": 70, "qty": 9, "time": 123.0}]}
    assert hw.peak() == 55_000
    assert led.get_schedule()["games"]["g"]["fired"] is True
    assert {p: (tmp_path / p).read_bytes() for p in before} == before        # untouched, not deleted


def test_legacy_import_runs_once_even_if_tables_later_empty(tmp_path, db):
    pf = _write(tmp_path / "p.json", {"T1": [{"price": 70, "qty": 9, "time": 1.0}]})
    p = Portfolio(filename=pf, ledger=db)
    p.remove_position("T1")                                   # all positions closed
    assert not Portfolio(filename=pf, ledger=db).has_any_positions()   # stale JSON NOT resurrected


def test_legacy_import_skipped_when_db_already_has_state(tmp_path, db):
    Portfolio(ledger=db).add_position("MINE", 50, 1)
    pf = _write(tmp_path / "p.json", {"OLD": [{"price": 70, "qty": 9, "time": 1.0}]})
    p = Portfolio(filename=pf, ledger=db)
    assert p.has_position("MINE") and not p.has_position("OLD")


def test_legacy_import_missing_or_corrupt_file_is_harmless(tmp_path, db, caplog):
    assert not Portfolio(filename=str(tmp_path / "absent.json"), ledger=db).has_any_positions()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.ERROR):
        hw = HighWaterMark(filename=str(bad), ledger=str(tmp_path / "other.sqlite"))
    assert hw.peak() is None and "import" in caplog.text


# --- misfire tolerance -------------------------------------------------------


def test_pre_game_job_registered_with_misfire_grace_and_coalesce():
    sched = FakeScheduler()
    trigger = datetime.now(timezone.utc) + timedelta(hours=1)
    game = {"game_id": "g", "home_team": "BOS", "away_team": "LAL"}
    main._register_job(sched, None, None, None, game, trigger, True)
    kw = sched.jobs[0][1]
    assert kw["misfire_grace_time"] == config.PRE_GAME_MISFIRE_GRACE_SECONDS >= 300
    assert kw["coalesce"] is True


def test_health_job_registered_with_misfire_grace_and_coalesce():
    sched = FakeScheduler()
    main._register_health_job(sched, None, None, None)
    kw = sched.jobs[0][1]
    assert kw["id"] == "health" and kw["coalesce"] is True
    assert kw["misfire_grace_time"] == config.HEALTH_MISFIRE_GRACE_SECONDS > 1


def test_schedule_retry_registered_with_misfire_grace(monkeypatch, db):
    monkeypatch.setattr(main, "LEDGER", Ledger(db))
    monkeypatch.setattr(main.nba_scheduler, "get_schedule", lambda client: None)
    sched = FakeScheduler()
    main.setup_daily_schedule(None, None, None, sched, True)
    kw = sched.jobs[0][1]
    assert kw["id"] == "schedule-retry" and kw["coalesce"] is True
    assert kw["misfire_grace_time"] == config.SCHEDULE_RETRY_MISFIRE_GRACE_SECONDS > 1


def test_confirm_fill_job_registered_with_misfire_grace(env_live):
    sched = env_live
    (_, kw), = [j for j in sched.jobs if j[1]["id"].startswith("confirm-")]
    assert kw["misfire_grace_time"] == config.CONFIRM_FILL_MISFIRE_GRACE_SECONDS > 1
    assert kw["coalesce"] is True


@pytest.fixture()
def env_live(tmp_path, monkeypatch):
    from nba_trading.tests.test_main import FakeClient
    monkeypatch.setattr(config, "FAVORITE_PRICE_MIN", 50)
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 90)
    monkeypatch.setattr(config, "BANKROLL_FRACTION", 0.05)
    monkeypatch.setattr(config, "LIMIT_PRICE_BUFFER_CENTS", 1)
    monkeypatch.setattr(config, "MAX_DRAWDOWN_PCT", 50)
    monkeypatch.setattr(config, "MAX_EXPOSURE_PCT", 30)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(main, "mark_game_fired", lambda gid: None)
    tip = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    game = {"game_id": "g", "home_team": "BOS", "away_team": "LAL", "tip_off_utc": tip}
    sched = FakeScheduler()
    main.pre_game_job(FakeClient(), Portfolio(), HighWaterMark(), sched, game, dry_run=False)
    return sched


# --- single-instance lock ----------------------------------------------------


def test_second_lock_attempt_is_refused_until_released(db):
    first = InstanceLock(db).acquire()
    with pytest.raises(InstanceLockError, match="already holds"):
        InstanceLock(db).acquire()
    first.release()
    second = InstanceLock(db).acquire()          # free again
    second.release()


def test_lock_context_manager_and_other_db_independent(tmp_path):
    a, b = str(tmp_path / "a.sqlite"), str(tmp_path / "b.sqlite")
    with InstanceLock(a):
        with InstanceLock(b):                    # different DB: no conflict
            pass
        with pytest.raises(InstanceLockError):
            InstanceLock(a).acquire()
    with InstanceLock(a):
        pass


def test_run_bot_refuses_to_start_when_locked_and_never_touches_state(tmp_path, monkeypatch):
    dbp = str(tmp_path / "data" / "l.sqlite")
    monkeypatch.setattr(config, "LEDGER_FILE", dbp)
    monkeypatch.setattr(main, "_configure_logging", lambda: None)
    monkeypatch.setattr(main, "_run_locked", lambda *a, **k: pytest.fail("must not start"))
    holder = InstanceLock(dbp).acquire()
    try:
        with pytest.raises(SystemExit) as ei:
            main.run_bot(dry_run=True)
        assert ei.value.code not in (0, None) and "Refusing to start" in str(ei.value.code)
    finally:
        holder.release()


def test_run_bot_releases_lock_on_exit(tmp_path, monkeypatch):
    dbp = str(tmp_path / "l.sqlite")
    monkeypatch.setattr(config, "LEDGER_FILE", dbp)
    monkeypatch.setattr(main, "_configure_logging", lambda: None)
    calls = []
    monkeypatch.setattr(main, "_run_locked", lambda *a, **k: calls.append(1))
    main.run_bot(dry_run=True)
    assert calls == [1]
    InstanceLock(dbp).acquire().release()        # lock was released

    def boom(*a, **k):
        raise RuntimeError("crash")
    monkeypatch.setattr(main, "_run_locked", boom)
    with pytest.raises(RuntimeError):
        main.run_bot(dry_run=True)
    InstanceLock(dbp).acquire().release()        # released even on error


# --- clock skew --------------------------------------------------------------


class SkewClient:
    def __init__(self, skew=None, exc=None):
        self.skew, self.exc = skew, exc

    def clock_skew_seconds(self):
        if self.exc:
            raise self.exc
        return self.skew


def test_skew_constants_are_named_and_sane():
    assert config.CLOCK_SKEW_WARN_SECONDS == 2.0
    assert config.CLOCK_SKEW_MAX_LIVE_SECONDS == 30.0


def test_small_skew_is_quiet(caplog):
    with caplog.at_level(logging.WARNING):
        assert main.check_clock_skew(SkewClient(0.4), dry_run=False) == 0.4
    assert caplog.records == []


@pytest.mark.parametrize("skew", [3.0, -5.0])
def test_moderate_skew_warns_in_both_modes(skew, caplog):
    for dry in (True, False):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert main.check_clock_skew(SkewClient(skew), dry_run=dry) == skew
        assert any(r.levelno == logging.WARNING and "off by" in r.message for r in caplog.records)


def test_large_skew_refuses_live_start():
    for skew in (31.0, -45.0):
        with pytest.raises(SystemExit, match="Refusing to start"):
            main.check_clock_skew(SkewClient(skew), dry_run=False)


def test_large_skew_only_warns_in_dry_run(caplog):
    with caplog.at_level(logging.WARNING):
        assert main.check_clock_skew(SkewClient(120.0), dry_run=True) == 120.0
    assert any("off by" in r.message for r in caplog.records)


def test_skew_exactly_at_live_limit_does_not_refuse():
    assert main.check_clock_skew(SkewClient(30.0), dry_run=False) == 30.0


def test_failed_skew_check_warns_and_continues(caplog):
    with caplog.at_level(logging.WARNING):
        assert main.check_clock_skew(SkewClient(exc=ConnectionError("down")), dry_run=False) is None
    assert any("Clock-skew check failed" in r.message for r in caplog.records)


def test_ledger_is_usable_from_worker_threads(db):
    # APScheduler runs jobs on worker threads; the shared connection must allow it.
    import threading
    led = Ledger(db)
    errors = []

    def work(i):
        try:
            Portfolio(ledger=led).add_position(f"T{i}", 50, 1)
            led.record_event("INFO", "t", str(i))
        except Exception as e:  # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert led.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8
