"""Ledger unit tests + bot -> ledger integration."""

from __future__ import annotations

import pytest

from nba_trading import main
from nba_trading.ledger import Ledger, kalshi_fee_cents, settlement_pnl_cents
from nba_trading.tests.test_main import FakeClient, _run, env  # noqa: F401  (fixtures)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    led = Ledger(str(tmp_path / "l.sqlite"))
    monkeypatch.setattr(main, "LEDGER", led)
    yield led
    led.close()


def _rows(led, table):
    return [dict(r) for r in led.conn.execute(f"SELECT * FROM {table}")]


def test_fee_matches_kalshi_formula_and_rounds_up():
    assert kalshi_fee_cents(100, 50) == 175           # 0.07*100*.25 = $1.75
    assert kalshi_fee_cents(1, 93) == 1               # 0.0046 -> ceil 1c
    assert kalshi_fee_cents(0, 93) == 0


def test_settlement_pnl_win_and_loss():
    pnl, fee = settlement_pnl_cents(10, 93, yes_won=True)
    assert fee == kalshi_fee_cents(10, 93) and pnl == 10 * 7 - fee
    pnl, fee = settlement_pnl_cents(10, 93, yes_won=False)
    assert pnl == -930 - fee


def test_unsettled_orders_excludes_settled_and_rejected(ledger):
    d = ledger.record_decision(game_id="g", action="buy", reason="in_band", dry_run=False)
    a = ledger.record_order(decision_id=d, ticker="T1", client_order_id="c1", order_id="o1",
                            count=5, limit_price=92, outcome="placed")
    ledger.record_order(decision_id=d, ticker="T2", client_order_id="c2", order_id=None,
                        count=5, limit_price=92, outcome="insufficient_funds")
    assert [r["id"] for r in ledger.unsettled_orders()] == [a]
    ledger.record_settlement(a, "yes", 5, 92)
    assert ledger.unsettled_orders() == []


def test_decision_action_is_constrained(ledger):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_decision(game_id="g", action="maybe", reason="x", dry_run=True)


def test_skip_reasons_are_recorded(env, ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.config, "FAVORITE_PRICE_MIN", 91)      # ask 70 -> outside band
    monkeypatch.setattr(main.config, "FAVORITE_PRICE_MAX", 99)
    _run(env, FakeClient(), dry_run=False)
    rows = _rows(ledger, "decisions")
    assert len(rows) == 1 and rows[0]["action"] == "skip"
    assert rows[0]["reason"] == "outside_band:70" and rows[0]["ask_cents"] == 70
    assert rows[0]["band_min"] == 91 and rows[0]["dry_run"] == 0
    assert _rows(ledger, "orders") == []


def test_kill_switch_skip_recorded(env, ledger, tmp_path):  # noqa: F811
    (tmp_path / "STOP_TRADING").write_text("")
    _run(env, FakeClient(), dry_run=False)
    assert _rows(ledger, "decisions")[0]["reason"] == "kill_switch"


def test_live_buy_records_decision_and_order(env, ledger):  # noqa: F811
    _run(env, FakeClient(), dry_run=False)
    d, o = _rows(ledger, "decisions"), _rows(ledger, "orders")
    assert d[0]["action"] == "buy" and d[0]["shares"] == o[0]["count"]
    assert o[0]["outcome"] == "placed" and o[0]["order_id"] == "oid-1"
    assert o[0]["decision_id"] == d[0]["id"]
    assert _rows(ledger, "snapshots")[0]["equity_cents"] == 100_000


def test_dry_run_records_decision_but_no_order(env, ledger):  # noqa: F811
    _run(env, FakeClient(), dry_run=True)
    d = _rows(ledger, "decisions")
    assert d[0]["action"] == "buy" and d[0]["dry_run"] == 1
    assert _rows(ledger, "orders") == []


def test_failed_and_insufficient_orders_recorded(env, ledger):  # noqa: F811
    _run(env, FakeClient(order_result="INSUFFICIENT_FUNDS"), dry_run=False)
    assert _rows(ledger, "orders")[0]["outcome"] == "insufficient_funds"
    assert any(e["kind"] == "insufficient_funds" for e in _rows(ledger, "events"))


def test_ledger_write_failure_never_blocks_trading(env, monkeypatch):  # noqa: F811
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("disk full")
    monkeypatch.setattr(main, "LEDGER", Broken())
    client = FakeClient()
    _run(env, client, dry_run=False)
    assert len(client.order_calls) == 1


def test_confirm_fill_updates_ledger_and_settlement_uses_kalshi_fills(env, ledger):  # noqa: F811
    from nba_trading.tests.fakes import KalshiReadsMixin, order_dict
    _run(env, FakeClient(), dry_run=False)
    count = _rows(ledger, "orders")[0]["count"]

    class C(KalshiReadsMixin):
        positions = [{"ticker": "KXNBA-G1-BOS", "position_fp": str(count)}]
        orders = {"oid-1": order_dict("oid-1", ticker="KXNBA-G1-BOS", status="executed",
                                      filled=count, avg_fill_cents=71)}
        # Kalshi's real fills: a better price than the 71c limit, and a real fee.
        fills = [{"fill_id": "f1", "order_id": "oid-1", "ticker": "KXNBA-G1-BOS", "count": count,
                  "price_cents": 70, "side": "yes", "action": "buy", "fee_cents": 12, "ts": None}]

        def get_market(self, ticker):
            return {"result": "yes"}

    main.confirm_fill_job(C(), env["portfolio"], "KXNBA-G1-BOS", "oid-1", count)
    assert _rows(ledger, "orders")[0]["filled"] == count
    assert main.settle_open_orders(C()) == 1
    s = _rows(ledger, "settlements")[0]
    assert s["result"] == "yes" and s["filled"] == count and s["source"] == "kalshi_fills"
    assert s["pnl_cents"] == count * (100 - 70) - 12            # ACTUAL price and fee, not the limit
    assert s["avg_entry_cents"] == 70
    assert main.settle_open_orders(C()) == 0                    # idempotent


def test_settlement_waits_for_unresolved_market(env, ledger):  # noqa: F811
    from nba_trading.tests.fakes import KalshiReadsMixin
    _run(env, FakeClient(), dry_run=False)

    class C(KalshiReadsMixin):
        def get_market(self, ticker):
            return {"result": ""}

    assert main.settle_open_orders(C()) == 0
    assert _rows(ledger, "settlements") == []


def test_settlement_never_books_an_unconfirmed_zero_fill_as_a_non_trade(env, ledger):  # noqa: F811
    """No fills stored and the order reads filled=0 with status 'executed': that is a parse gap, not
    an unfilled order. Settling it would erase a real trade from the PnL, so it must wait."""
    from nba_trading.tests.fakes import KalshiReadsMixin, order_dict
    _run(env, FakeClient(), dry_run=False)

    class C(KalshiReadsMixin):
        orders = {"oid-1": order_dict("oid-1", status="executed", filled=0)}

        def get_market(self, ticker):
            return {"result": "yes"}

    assert main.settle_open_orders(C()) == 0
    assert _rows(ledger, "settlements") == []


def test_settlement_books_a_confirmed_unfilled_order_as_zero(env, ledger):  # noqa: F811
    from nba_trading.tests.fakes import KalshiReadsMixin, order_dict
    _run(env, FakeClient(), dry_run=False)

    class C(KalshiReadsMixin):
        orders = {"oid-1": order_dict("oid-1", status="canceled", filled=0)}

        def get_market(self, ticker):
            return {"result": "yes"}

    assert main.settle_open_orders(C()) == 1
    s = _rows(ledger, "settlements")[0]
    assert s["filled"] == 0 and s["pnl_cents"] == 0



# --- paper trades ---------------------------------------------------------


def _dry_decision(led, game, reason="in_band_unfunded", ticker="T1", ask=93, limit=93, dry=True, action="skip"):
    return led.record_decision(game_id=game, action=action, reason=reason, dry_run=dry,
                               ticker=ticker, ask_cents=ask, limit_price=limit)


def test_paper_candidates_only_in_band_dry_run_decisions(ledger):
    ok = _dry_decision(ledger, "g1")
    _dry_decision(ledger, "g2", reason="outside_band:80")                 # not in band
    _dry_decision(ledger, "g3", dry=False)                                # real-money mode
    _dry_decision(ledger, "g4", ticker=None)                              # nothing to settle
    buy = _dry_decision(ledger, "g5", reason="in_band", action="buy")     # funded dry-run buy
    assert sorted(r["id"] for r in ledger.unsettled_paper_candidates()) == sorted([ok, buy])


def test_paper_candidates_dedupe_by_game_and_skip_settled(ledger):
    first = _dry_decision(ledger, "g1")
    _dry_decision(ledger, "g1")                                           # restart re-evaluated the game
    assert [r["id"] for r in ledger.unsettled_paper_candidates()] == [first]
    ledger.record_paper_trade(first, "T1", 93, "yes")
    assert ledger.unsettled_paper_candidates() == []


def test_paper_entry_falls_back_to_ask_when_no_limit_price(ledger):
    _dry_decision(ledger, "g1", ask=94, limit=None)
    assert ledger.unsettled_paper_candidates()[0]["entry_cents"] == 94


def test_record_paper_trade_pnl_is_one_contract(ledger):
    d = _dry_decision(ledger, "g1")
    assert ledger.record_paper_trade(d, "T1", 93, "yes") == 7 - kalshi_fee_cents(1, 93)
    d2 = _dry_decision(ledger, "g2", ticker="T2")
    assert ledger.record_paper_trade(d2, "T2", 93, "no") == -93 - kalshi_fee_cents(1, 93)


def test_unfunded_in_band_skip_row_carries_limit_price(env, ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.config, "FAVORITE_PRICE_MIN", 60)
    client = FakeClient()
    client.get_balance = lambda: 9
    _run(env, client, dry_run=True)
    row = dict(ledger.conn.execute("SELECT * FROM decisions").fetchone())
    assert row["reason"] == "in_band_unfunded" and row["limit_price"] == 71 and row["ask_cents"] == 70


def test_settle_paper_trades_end_to_end(env, ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.config, "FAVORITE_PRICE_MIN", 60)
    client = FakeClient()
    client.get_balance = lambda: 9
    _run(env, client, dry_run=True)

    class Market:
        result = ""

        def get_market(self, ticker):
            return {"result": self.result}

    m = Market()
    assert main.settle_paper_trades(m) == 0                               # game not over yet
    m.result = "yes"
    assert main.settle_paper_trades(m) == 1
    row = dict(ledger.conn.execute("SELECT * FROM paper_trades").fetchone())
    assert row["entry_cents"] == 71 and row["result"] == "yes" and row["pnl_cents"] > 0
    assert main.settle_paper_trades(m) == 0                               # idempotent
