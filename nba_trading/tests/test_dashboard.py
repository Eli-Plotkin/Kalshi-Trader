"""Dashboard data layer + a render smoke test against a seeded ledger."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from nba_trading.dashboard import data
from nba_trading.ledger import Ledger


def _seed(path, wins=3, losses=1, hb_minutes_ago=5):
    led = Ledger(str(path))
    for i in range(wins + losses):
        d = led.record_decision(game_id=f"g{i}", action="buy", reason="in_band", dry_run=False,
                                home="BOS", away="LAL", ticker=f"T{i}", ask_cents=93, shares=10,
                                limit_price=93, balance_cents=100_000, equity_cents=100_000,
                                band_min=91, band_max=99, fraction=0.10)
        o = led.record_order(decision_id=d, ticker=f"T{i}", client_order_id=f"c{i}",
                             order_id=f"o{i}", count=10, limit_price=93, outcome="placed")
        led.set_filled(f"o{i}", 10)
        led.record_settlement(o, "yes" if i < wins else "no", 10, 93)
    led.record_decision(game_id="gx", action="skip", reason="outside_band:80", dry_run=False,
                        home="A", away="B", band_min=91, band_max=99, fraction=0.10)
    led.record_order(decision_id=None, ticker="TX", client_order_id="cx", order_id=None,
                     count=5, limit_price=93, outcome="insufficient_funds")
    led.record_snapshot(90_000, 9_300, 99_300, 100_000, 0.7)
    led.record_event("INFO", "heartbeat", "ok")
    led.conn.execute("UPDATE events SET ts=?", ((datetime.now(timezone.utc)
                      - timedelta(minutes=hb_minutes_ago)).isoformat(),))
    led.conn.commit()
    led.close()


@pytest.fixture()
def seeded(tmp_path):
    p = tmp_path / "l.sqlite"
    _seed(p)
    return p


def test_connection_is_read_only(seeded):
    conn = data.connect_ro(str(seeded))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM decisions")


def test_trades_and_metrics(seeded):
    f = data.load_frames(data.connect_ro(str(seeded)))
    t = f["trades"]
    assert sorted(t["status"]) == ["insufficient_funds", "settled", "settled", "settled", "settled"]
    m = data.trade_metrics(t)
    assert (m["settled"], m["wins"], m["losses"]) == (4, 3, 1)
    assert m["win_rate"] == 0.75
    # 3 wins * 10 * 7c - fees, 1 loss * 10 * 93c
    assert m["net_pnl_cents"] < 3 * 70 - 930 + 1
    assert m["roi"] == pytest.approx(m["net_pnl_cents"] / (4 * 930))


def test_open_and_unfilled_statuses(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    d = led.record_decision(game_id="g", action="buy", reason="r", dry_run=False)
    led.record_order(decision_id=d, ticker="A", client_order_id="a", order_id="oa", count=5,
                     limit_price=92, outcome="placed")                      # no settlement yet
    o2 = led.record_order(decision_id=d, ticker="B", client_order_id="b", order_id="ob", count=5,
                          limit_price=92, outcome="placed")
    led.set_filled("ob", 0)
    led.record_settlement(o2, "no", 0, 92)                                  # never filled
    led.close()
    t = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))["trades"]
    assert sorted(t["status"]) == ["open", "unfilled"]
    assert data.trade_metrics(t)["settled"] == 0 and data.trade_metrics(t)["open"] == 1


def test_breakeven_win_rate():
    assert data.breakeven_win_rate(93.0) == pytest.approx(0.93 + 0.07 * 0.93 * 0.07)
    assert data.breakeven_win_rate(50.0) == pytest.approx(0.5175)


def test_implied_edge_numbers(seeded):
    # seeded: 3 wins, 1 loss, all bought at 93c
    t = data.load_frames(data.connect_ro(str(seeded)))["trades"]
    e = data.implied_edge(t)
    assert (e["n"], e["wins"], e["losses"]) == (4, 3, 1)
    assert e["win_rate"] == 0.75 and e["implied_rate"] == pytest.approx(0.93)
    assert e["edge_pts"] == pytest.approx(-18.0)
    assert e["expected_wins"] == pytest.approx(3.72)
    # P(>=3 wins of 4 at p=.93) = 4*.93^3*.07 + .93^4
    assert e["p_value"] == pytest.approx(4 * 0.93**3 * 0.07 + 0.93**4)
    assert e["wilson_lo"] < 0.75 < e["wilson_hi"]


def test_implied_edge_significant_for_long_perfect_record(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    for i in range(70):
        d = led.record_decision(game_id=f"g{i}", action="buy", reason="r", dry_run=False)
        o = led.record_order(decision_id=d, ticker=f"T{i}", client_order_id=f"c{i}",
                             order_id=f"o{i}", count=10, limit_price=93, outcome="placed")
        led.set_filled(f"o{i}", 10)
        led.record_settlement(o, "yes", 10, 93)
    led.close()
    e = data.implied_edge(data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))["trades"])
    assert e["p_value"] == pytest.approx(0.93**70)           # ~0.006
    assert e["edge_pts"] == pytest.approx(7.0)


def test_implied_edge_none_without_settled_trades(tmp_path):
    Ledger(str(tmp_path / "l.sqlite")).close()
    t = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))["trades"]
    assert data.implied_edge(t) is None and data.pnl_vs_pricing(t).empty


def test_pnl_vs_pricing_baseline_is_negative_fees(seeded):
    t = data.load_frames(data.connect_ro(str(seeded)))["trades"]
    c = data.pnl_vs_pricing(t)
    assert len(c) == 4 and (c["implied_baseline"].diff().dropna() < 0).all()
    assert c["actual"].iloc[-1] == pytest.approx(t[t.status == "settled"]["pnl_cents"].sum() / 100)


def test_set_kill_switch_creates_and_removes_file(tmp_path):
    f = tmp_path / "STOP_TRADING"
    data.set_kill_switch(str(f), True)
    assert f.exists()
    data.set_kill_switch(str(f), False)
    assert not f.exists()
    data.set_kill_switch(str(f), False)                       # removing twice is fine


def test_heartbeat_age_and_skip_reasons(seeded):
    f = data.load_frames(data.connect_ro(str(seeded)))
    assert 4 < data.heartbeat_age_minutes(f["events"]) < 7
    r = data.skip_reason_counts(f["decisions"])
    assert list(r["reason"]) == ["outside_band"]                    # ':80' detail stripped


def test_empty_ledger_is_handled(tmp_path):
    Ledger(str(tmp_path / "l.sqlite")).close()
    f = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))
    assert data.trade_metrics(f["trades"])["settled"] == 0
    assert data.heartbeat_age_minutes(f["events"]) is None
    assert f["paper"].empty


def test_app_renders_with_data_and_when_empty(seeded, tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    path = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")

    monkeypatch.setenv("LEDGER_FILE", str(seeded))
    at = AppTest.from_file(path, default_timeout=30).run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 5

    # no sidebar anymore, and the kill-switch toggle is present
    assert len(at.sidebar.children) == 0
    assert [t.label for t in at.toggle] == ["Pause trading (kill switch)"]

    monkeypatch.setenv("LEDGER_FILE", str(tmp_path / "missing.sqlite"))
    at = AppTest.from_file(path, default_timeout=30).run()
    assert not at.exception and any("No ledger yet" in w.value for w in at.warning)


def test_kill_toggle_writes_and_clears_the_real_file(seeded, tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from nba_trading import config
    kill = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(kill))
    monkeypatch.setenv("LEDGER_FILE", str(seeded))
    path = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")

    at = AppTest.from_file(path, default_timeout=30).run()
    assert not kill.exists()
    at.toggle[0].set_value(True).run()
    assert kill.exists() and not at.exception
    at.toggle[0].set_value(False).run()
    assert not kill.exists()


# --- paper trades ---------------------------------------------------------


def _paper_ledger(path, results):
    """results: list of ('yes'|'no') for in-band dry-run games, all asked at 93c."""
    led = Ledger(str(path))
    for i, r in enumerate(results):
        d = led.record_decision(game_id=f"g{i}", action="skip", reason="in_band_unfunded", dry_run=True,
                                ticker=f"T{i}", ask_cents=93, limit_price=93, balance_cents=9,
                                equity_cents=9, band_min=91, band_max=99, fraction=0.1)
        led.record_paper_trade(d, f"T{i}", 93, r)
    led.close()


def test_paper_trades_feed_the_same_edge_math(tmp_path):
    _paper_ledger(tmp_path / "l.sqlite", ["yes"] * 9 + ["no"])
    f = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))
    e = data.implied_edge(f["paper"])
    assert (e["n"], e["wins"], e["losses"]) == (10, 9, 1)
    assert e["implied_rate"] == pytest.approx(0.93) and e["edge_pts"] == pytest.approx(-3.0)
    assert not data.pnl_vs_pricing(f["paper"]).empty


def test_dashboard_loads_a_ledger_written_before_paper_trades_existed(tmp_path):
    p = tmp_path / "old.sqlite"
    Ledger(str(p)).close()
    conn = sqlite3.connect(p)
    conn.execute("DROP TABLE paper_trades")
    conn.commit()
    conn.close()
    f = data.load_frames(data.connect_ro(str(p)))
    assert f["paper"].empty and data.implied_edge(f["paper"]) is None


def test_app_renders_paper_source(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    _paper_ledger(tmp_path / "l.sqlite", ["yes", "yes", "no"])
    monkeypatch.setenv("LEDGER_FILE", str(tmp_path / "l.sqlite"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"),
                           default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("Paper" in o for o in at.radio[0].options)
    assert at.radio[0].value.startswith("Paper")             # no live trades -> defaults to paper


def test_external_kill_switch_changes_are_never_reverted_by_a_refresh(seeded, tmp_path, monkeypatch):
    """A `touch STOP_TRADING` from a terminal must survive the dashboard's periodic rerun."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from nba_trading import config
    kill = tmp_path / "STOP_TRADING"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(kill))
    monkeypatch.setenv("LEDGER_FILE", str(seeded))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"),
                           default_timeout=30).run()
    assert at.toggle[0].value is False

    kill.write_text("paused from a terminal")        # operator pauses outside the dashboard
    at.run()                                          # what the 30s refresh does
    assert kill.exists()                              # NOT deleted by the stale toggle
    assert at.toggle[0].value is True                 # and the toggle now reflects it

    kill.unlink()                                     # resumed outside the dashboard
    at.run()
    assert not kill.exists() and at.toggle[0].value is False


def test_dashboard_body_is_a_30s_fragment():
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "app.py").read_text()
    assert "@st.fragment(run_every=REFRESH_SECONDS)" in src and "REFRESH_SECONDS = 30" in src


# --- reconciliation: adopted orders and actual fill prices --------------------


def _recovered_ledger(path):
    led = Ledger(str(path))
    d = led.record_decision(game_id="g1", action="buy", reason="in_band", dry_run=False,
                            ticker="T1", ask_cents=93, limit_price=93)
    pk, _ = led.begin_order(d, "T1", "c1", 10, 93)
    led.mark_placed(pk, "oid-1")                                       # limit 93c ...
    led.upsert_fills([{"fill_id": "f1", "order_id": "oid-1", "ticker": "T1", "count": 10,
                       "price_cents": 91, "side": "yes", "action": "buy", "fee_cents": 20, "ts": None}])
    led.record_settlement_from_fills(pk, "yes")                        # ... but Kalshi filled at 91c
    a = led.adopt_order("T2", "oid-2", 5, 94, limit_price=94)          # a trade the bot had lost
    led.upsert_fills([{"fill_id": "f2", "order_id": "oid-2", "ticker": "T2", "count": 5,
                       "price_cents": 94, "side": "yes", "action": "buy", "fee_cents": 8, "ts": None}])
    led.record_settlement_from_fills(a, "no")
    led.begin_order(None, "T3", "c3", 5, 92)                           # still unresolved
    led.close()


def test_adopted_trades_count_as_live_trades_and_use_the_price_actually_paid(tmp_path):
    _recovered_ledger(tmp_path / "l.sqlite")
    t = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))["trades"]
    by = {r["ticker"]: r for r in t.to_dict("records")}
    assert by["T1"]["status"] == "settled" and by["T2"]["status"] == "settled"      # adopted -> a real trade
    assert by["T1"]["limit_price"] == 93 and by["T1"]["entry_cents"] == pytest.approx(91)
    assert by["T1"]["source"] == "kalshi_fills"
    assert by["T3"]["status"] == "submitting"                                       # unresolved is visible
    m = data.trade_metrics(t)
    assert (m["settled"], m["wins"], m["losses"]) == (2, 1, 1)
    assert m["avg_price"] == pytest.approx((91 + 94) / 2)


def test_implied_edge_uses_the_fill_price_not_the_limit(tmp_path):
    _recovered_ledger(tmp_path / "l.sqlite")
    t = data.load_frames(data.connect_ro(str(tmp_path / "l.sqlite")))["trades"]
    e = data.implied_edge(t)
    assert e["implied_rate"] == pytest.approx((0.91 + 0.94) / 2)       # not (0.93 + 0.94) / 2
