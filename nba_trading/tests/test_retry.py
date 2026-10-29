"""Transient-failure retry policy: retry reads (bounded), never retry orders, prefer a miss
over a double buy or an out-of-band buy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nba_trading import config, main
from nba_trading.ledger import Ledger
from nba_trading.tests.test_main import FakeClient, FakeScheduler, env  # noqa: F401  (fixtures)


def _run(env, client, dry_run=False, attempt=0):
    main.pre_game_job(client, env["portfolio"], env["high_water"], env["scheduler"],
                      env["game"], dry_run, attempt)


def _retry_attempt(env, index=-1):
    """Run the retry job the scheduler was handed, the way APScheduler would."""
    a, kw = env["scheduler"].jobs[index]
    assert kw["id"].startswith(env["game"]["game_id"] + "-retry")
    return kw["args"][-1]


class NoBalance(FakeClient):
    def get_balance(self):
        return None


@pytest.fixture()
def ledger(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.setattr(main, "LEDGER", led)
    return led


def _decisions(led):
    return [(r["reason"], r["action"]) for r in led.conn.execute("SELECT reason, action FROM decisions ORDER BY id")]


# ---------------------------------------------------------------- transient -> retry

def test_transient_failure_queues_a_retry_and_does_not_mark_fired(env, ledger):  # noqa: F811
    client = NoBalance()
    _run(env, client)
    assert client.order_calls == []
    assert env["fired"] == []                              # game still open for the retry
    assert len(env["scheduler"].jobs) == 1
    assert _retry_attempt(env) == 1
    assert _decisions(ledger)[0] == ("balance_fetch_failed", "skip")


def test_retry_is_a_delayed_one_off_job_with_a_unique_id(env):  # noqa: F811
    _run(env, NoBalance())
    a, kw = env["scheduler"].jobs[0]
    assert kw["id"] == f"{env['game']['game_id']}-retry1" and kw["replace_existing"] is True
    delta = kw["trigger"].run_date - datetime.now(timezone.utc)
    assert timedelta(seconds=50) < delta <= timedelta(seconds=61)


def test_gives_up_after_three_retries_then_skips_and_marks_fired(env, ledger):  # noqa: F811
    client = NoBalance()
    attempt = 0
    for _ in range(config.PRE_GAME_MAX_RETRIES):           # attempts 0,1,2 each queue a retry
        _run(env, client, attempt=attempt)
        attempt = _retry_attempt(env)
    assert attempt == 3 and env["fired"] == []
    _run(env, client, attempt=3)                           # 4th attempt fails: budget used up
    assert len(env["scheduler"].jobs) == 3                 # no 4th retry queued
    assert env["fired"] == [env["game"]["game_id"]]        # now final
    assert client.order_calls == []
    reasons = [r for r, _ in _decisions(ledger)]
    assert reasons[-1] == "retries_exhausted" and "balance_fetch_failed:retry3" in reasons
    assert any(e["kind"] == "retries_exhausted" for e in ledger.conn.execute("SELECT kind FROM events"))


def test_recovered_retry_places_exactly_one_order(env, ledger):  # noqa: F811
    _run(env, NoBalance())                                  # attempt 0: API hiccup
    good = FakeClient()
    _run(env, good, attempt=_retry_attempt(env))            # attempt 1: works
    assert len(good.order_calls) == 1
    assert good.order_calls[0]["client_order_id"] == f"nba-{env['game']['game_id']}-KXNBA-G1-BOS"
    assert env["fired"] == [env["game"]["game_id"]]         # fired once, after the final outcome
    assert len(env["scheduler"].jobs) == 2                  # the retry + the fill check; none extra


def test_no_retry_when_it_would_run_too_close_to_tip_off(env, monkeypatch):  # noqa: F811
    env["game"]["tip_off_utc"] = (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat()
    client = NoBalance()
    _run(env, client)
    assert env["scheduler"].jobs == []                      # a retry 60s out would land < 180s from tip
    assert env["fired"] == [env["game"]["game_id"]] and client.order_calls == []


@pytest.mark.parametrize("reason,breaker", [
    ("balance_fetch_failed", None), ("no_orderbook_data", None),
])
def test_other_read_failures_are_retryable_too(env, reason, breaker):  # noqa: F811
    class C(FakeClient):
        def get_orderbook(self, t): return None
    _run(env, NoBalance() if reason == "balance_fetch_failed" else C())
    assert len(env["scheduler"].jobs) == 1


def test_failed_position_check_is_retried_not_traded(env):  # noqa: F811
    client = FakeClient(positions=RuntimeError("api down"))
    _run(env, client)
    assert client.order_calls == [] and len(env["scheduler"].jobs) == 1 and env["fired"] == []


# ---------------------------------------------------------------- definitive -> never retried

def test_out_of_band_is_final_not_retried(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "FAVORITE_PRICE_MIN", 91)   # fake book asks 70
    client = FakeClient()
    _run(env, client)
    assert env["scheduler"].jobs == [] and env["fired"] == [env["game"]["game_id"]]
    assert client.order_calls == []


def test_kill_switch_and_breaker_are_final(env, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "STOP_TRADING").write_text("")
    _run(env, FakeClient())
    assert env["scheduler"].jobs == [] and len(env["fired"]) == 1


@pytest.mark.parametrize("result", [None, "INSUFFICIENT_FUNDS", "DUPLICATE_ORDER", "LOCATION_ATTESTATION"])
def test_an_order_attempt_is_never_retried(env, result):  # noqa: F811
    client = FakeClient(order_result=result)
    _run(env, client)
    assert len(client.order_calls) == 1                     # one attempt only
    assert env["scheduler"].jobs == [] and env["fired"] == [env["game"]["game_id"]]


def test_successful_order_is_final(env):  # noqa: F811
    client = FakeClient()
    _run(env, client)
    assert len(client.order_calls) == 1 and env["fired"] == [env["game"]["game_id"]]


# ---------------------------------------------------------------- band safety on retry

def test_a_retry_that_finds_the_price_out_of_band_does_not_buy(env, ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "FAVORITE_PRICE_MIN", 60)
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 75)   # fake book asks 70: in band at first
    _run(env, NoBalance())                                   # attempt 0 fails to read
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 65)   # price/band moved before the retry
    client = FakeClient()
    _run(env, client, attempt=_retry_attempt(env))
    assert client.order_calls == []
    assert any(r.startswith("outside_band") for r, _ in _decisions(ledger))


def test_limit_price_outside_band_is_refused(env, ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.strategy, "compute_limit_price", lambda **k: 100)
    client = FakeClient()
    _run(env, client)
    assert client.order_calls == []
    assert ("limit_outside_band", "skip") in _decisions(ledger)


def test_limit_price_never_exceeds_band_max_even_with_a_buffer(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "FAVORITE_PRICE_MAX", 70)
    monkeypatch.setattr(config, "LIMIT_PRICE_BUFFER_CENTS", 5)     # ask 70 + 5 would be 75
    client = FakeClient()
    _run(env, client)
    assert client.order_calls[0]["price"] == 70


# ---------------------------------------------------------------- double-buy protection

def test_existing_ledger_order_for_the_game_blocks_a_second_order(env, ledger):  # noqa: F811
    cid = f"nba-{env['game']['game_id']}-KXNBA-G1-BOS"
    ledger.record_order(decision_id=None, ticker="KXNBA-G1-BOS", client_order_id=cid,
                        order_id="oid-0", count=5, limit_price=71, outcome="placed")
    client = FakeClient()
    _run(env, client)
    assert client.order_calls == []
    assert any(e["kind"] == "order_already_recorded" for e in ledger.conn.execute("SELECT kind FROM events"))


def test_existing_position_blocks_a_retry_from_buying_again(env):  # noqa: F811
    env["portfolio"].add_position("KXNBA-G1-BOS", 71, 5)
    client = FakeClient(positions=[{"ticker": "KXNBA-G1-BOS", "position_fp": "5.00"}])
    _run(env, client, attempt=1)
    assert client.order_calls == []


def test_mark_fired_failure_does_not_escape_after_an_order(env, monkeypatch):  # noqa: F811
    def boom(_):
        raise RuntimeError("db locked")
    monkeypatch.setattr(main, "mark_game_fired", boom)
    client = FakeClient()
    _run(env, client)                                        # must not raise
    assert len(client.order_calls) == 1


def test_dry_run_retries_but_never_marks_fired(env, ledger):  # noqa: F811
    _run(env, NoBalance(), dry_run=True)
    assert len(env["scheduler"].jobs) == 1 and env["fired"] == []
