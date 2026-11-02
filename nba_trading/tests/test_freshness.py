"""Freshness banner logic (pure): thresholds at the boundaries, bot-silent case, sync errors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nba_trading.dashboard import freshness as fr

NOW = datetime(2026, 10, 29, 18, 0, 30, tzinfo=timezone.utc)


def _state(age_s=10, hb_min=2, **kw):
    s = {"generated_at": NOW - timedelta(seconds=age_s),
         "bot_heartbeat_at": None if hb_min is None else NOW - timedelta(minutes=hb_min),
         "last_ok_at": NOW - timedelta(seconds=5), "error": None, "consecutive_errors": 0}
    s.update(kw)
    return s


@pytest.mark.parametrize("age,level", [
    (0, "fresh"), (59, "fresh"), (60, "fresh"),
    (61, "stale"), (179, "stale"), (180, "stale"),
    (181, "broken"), (3600, "broken"),
])
def test_levels_at_the_contract_boundaries(age, level):
    a = fr.assess(_state(age_s=age), now=NOW)
    assert a["level"] == level
    assert a["data_age_seconds"] == pytest.approx(age)


def test_message_has_the_clock_time_and_age():
    a = fr.assess(_state(age_s=12), now=NOW)
    assert a["message"] == "Data as of 2026-10-29 18:00:18 UTC (12 s ago)"


def test_stale_and_broken_messages_say_so():
    assert "lagging" in fr.assess(_state(age_s=120), now=NOW)["message"]
    assert "STALE" in fr.assess(_state(age_s=600), now=NOW)["message"]


def test_heartbeat_ok_and_formatting():
    a = fr.assess(_state(hb_min=7), now=NOW)
    assert (a["heartbeat"]["level"], a["heartbeat"]["message"]) == ("ok", "Bot heartbeat 7 min ago")


def test_fresh_snapshot_with_old_heartbeat_is_replication_ok_bot_silent():
    a = fr.assess(_state(age_s=20, hb_min=46), now=NOW)
    assert a["level"] == "fresh"
    assert a["heartbeat"]["level"] == "silent"
    assert "replication OK, bot silent" in a["heartbeat"]["message"]
    assert "46 min" in a["heartbeat"]["message"]


def test_heartbeat_boundary_is_45_minutes():
    assert fr.assess(_state(hb_min=45), now=NOW)["heartbeat"]["level"] == "ok"
    assert fr.assess(_state(hb_min=45.5), now=NOW)["heartbeat"]["level"] == "silent"


def test_old_heartbeat_with_stale_data_does_not_claim_replication_ok():
    a = fr.assess(_state(age_s=900, hb_min=60), now=NOW)
    assert a["level"] == "broken"
    assert "replication OK" not in a["heartbeat"]["message"]


def test_no_heartbeat_in_snapshot_is_unknown_not_silent():
    assert fr.assess(_state(hb_min=None), now=NOW)["heartbeat"]["level"] == "unknown"


def test_no_state_or_no_generated_at_is_waiting():
    for s in (None, {}, {"generated_at": None, "error": None}):
        a = fr.assess(s, now=NOW)
        assert a["level"] == "waiting" and a["data_age_seconds"] is None and a["error"] is None
        assert "Waiting for the first data" in a["message"]


def test_sync_error_text_and_time_since_last_good_sync():
    a = fr.assess(_state(age_s=100, error="ConnectionError: boom", consecutive_errors=3,
                         last_ok_at=NOW - timedelta(seconds=95)), now=NOW)
    assert a["level"] == "stale"                       # level still follows the data age
    assert "ConnectionError: boom" in a["error"]
    assert "3 failures in a row" in a["error"]
    assert "Last good sync 95 s ago" in a["error"]


def test_sync_error_before_any_good_sync_is_waiting_with_error():
    a = fr.assess({"error": "NoSuchBucket", "consecutive_errors": 1, "generated_at": None}, now=NOW)
    assert a["level"] == "waiting"
    assert "NoSuchBucket" in a["error"] and "no successful sync yet" in a["error"]


def test_accepts_objects_and_iso_strings_and_naive_datetimes():
    class S:
        generated_at = (NOW - timedelta(seconds=30)).isoformat()
        bot_heartbeat_at = (NOW - timedelta(minutes=3)).replace(tzinfo=None)   # naive => UTC
        last_ok_at = None
        error = None
        consecutive_errors = 0
    a = fr.assess(S(), now=NOW)
    assert a["level"] == "fresh" and a["heartbeat"]["message"] == "Bot heartbeat 3 min ago"
    z = fr.assess({"generated_at": "2026-10-29T17:59:30Z"}, now=NOW)
    assert z["data_age_seconds"] == pytest.approx(60)


def test_future_timestamp_is_never_clamped_into_a_green_banner():
    """Was: 'clamps to zero'. A generated_at ahead of this host's clock hid a real problem."""
    a = fr.assess(_state(age_s=-120), now=NOW)             # 2 min in the future, no seq_advanced_at
    assert a["data_age_seconds"] == pytest.approx(-120)     # reported as is, not 0
    assert a["level"] in ("stale", "broken") and a["level"] != "fresh"
    assert "clock skew between hosts" in a["message"] and "about 120 s" in a["message"]
    assert "age is measured from observed updates" in a["message"]
    assert a["skew_warning"]


def test_small_future_timestamp_within_tolerance_is_fine():
    a = fr.assess(_state(age_s=-10), now=NOW)               # exactly the 10 s tolerance
    assert a["level"] == "fresh" and a["skew_warning"] is None
    a = fr.assess(_state(age_s=-11), now=NOW)
    assert a["level"] == "stale" and a["skew_warning"]


def test_long_error_text_is_truncated_and_single_line():
    a = fr.assess(_state(error="x\n" * 500), now=NOW)
    assert "\n" not in a["error"] and len(a["error"]) < 450


def test_thresholds_agree_between_the_frontend_and_the_replication_package():
    """freshness.py deliberately imports nothing from the replication package, so it keeps its own copy
    of the thresholds. They must never drift apart: the banner colour and the subscriber's notion of
    'fresh' have to mean the same thing."""
    from nba_trading.dashboard import freshness
    from nba_trading.replication import subscriber
    assert freshness.FRESH_MAX_SECONDS == subscriber.FRESH_SECONDS
    assert freshness.STALE_MAX_SECONDS == subscriber.STALE_SECONDS
    assert freshness.HEARTBEAT_SILENT_SECONDS == subscriber.BOT_SILENT_SECONDS
    if hasattr(subscriber, "BOT_ALIVE_SILENT_SECONDS"):
        assert freshness.BOT_ALIVE_SILENT_SECONDS == subscriber.BOT_ALIVE_SILENT_SECONDS == 180




# ---------------------------------------------------------------- skew-proof age (F5)

def _obs(seen_ago=5, gen_offset=0.0, **kw):
    """A state with the new fields. seq_advanced_at is the FRONTEND clock `seen_ago` s before NOW;
    generated_at is the BACKEND clock, `gen_offset` s away from where an in-sync clock would put it
    (about 3 s before the observation)."""
    seen = NOW - timedelta(seconds=seen_ago)
    s = {"seq_advanced_at": seen, "generated_at": seen - timedelta(seconds=3) + timedelta(seconds=gen_offset),
         "bot_heartbeat_at": None, "bot_alive_at": None, "last_ok_at": NOW, "checked_at": NOW,
         "error": None, "consecutive_errors": 0}
    s.update(kw)
    return s


def test_age_comes_from_seq_advanced_at_not_from_the_backend_clock():
    a = fr.assess(_obs(seen_ago=20), now=NOW)
    assert a["data_age_seconds"] == pytest.approx(20)
    assert a["level"] == "fresh" and a["skew_warning"] is None
    assert a["skew_seconds"] == pytest.approx(3)


@pytest.mark.parametrize("seen_ago,level", [(60, "fresh"), (61, "stale"), (180, "stale"), (181, "broken")])
def test_levels_use_the_observed_age(seen_ago, level):
    assert fr.assess(_obs(seen_ago=seen_ago), now=NOW)["level"] == level


@pytest.mark.parametrize("backend_skew,true_age,expected", [
    (300, 240, "broken"),      # r5: backend clock +5 min, replication really dead for 4 min
    (180, 181, "broken"),      # backend +3 min, dead 181 s
    (3600, 400, "broken"),     # backend +1 h
])
def test_reviewer_r5_dead_replication_is_never_green_whatever_the_backend_clock_says(backend_skew, true_age, expected):
    a = fr.assess(_obs(seen_ago=true_age, gen_offset=backend_skew), now=NOW)
    assert a["level"] == expected
    assert a["skew_warning"] and "clock skew between hosts" in a["message"]


def test_reviewer_r5_healthy_replication_with_a_slow_backend_clock_is_flagged_not_green():
    a = fr.assess(_obs(seen_ago=5, gen_offset=-120), now=NOW)       # backend clock 2 min behind
    assert a["level"] == "stale"                                    # at least stale, never green
    assert "clock skew between hosts (about 123 s)" in a["message"]
    assert a["data_age_seconds"] == pytest.approx(5)                # the age itself is still the observed one


@pytest.mark.parametrize("skew,flagged", [(-10, False), (-11, True), (0, False), (60, False), (61, True), (300, True)])
def test_skew_estimate_bounds(skew, flagged):
    seen = NOW - timedelta(seconds=5)
    a = fr.assess({"seq_advanced_at": seen, "generated_at": seen - timedelta(seconds=skew)}, now=NOW)
    assert a["skew_seconds"] == pytest.approx(skew)
    assert bool(a["skew_warning"]) is flagged
    assert (a["level"] != "fresh") is flagged


def test_generated_at_in_the_future_relative_to_the_frontend_clock_is_flagged_even_with_a_sane_estimate():
    seen = NOW - timedelta(seconds=1)
    a = fr.assess({"seq_advanced_at": seen, "generated_at": NOW + timedelta(seconds=30)}, now=NOW)
    assert a["level"] != "fresh" and a["skew_warning"]


def test_frozen_seq_ages_out_even_when_generated_at_looks_fresh():
    """Forged-manifest case (r9): a manifest with an absurd seq and a recent generated_at, then no advance."""
    for frozen_for, level in ((30, "fresh"), (90, "stale"), (200, "broken")):
        seen = NOW - timedelta(seconds=frozen_for)
        a = fr.assess({"generated_at": seen - timedelta(seconds=3), "seq_advanced_at": seen}, now=NOW)
        assert a["level"] == level
        # a forged generated_at that keeps looking recent can only make it worse, never greener
        forged = fr.assess({"generated_at": NOW - timedelta(seconds=1), "seq_advanced_at": seen}, now=NOW)
        assert forged["level"] == "broken" if frozen_for > 180 else forged["level"] != "fresh"
    # and a generated_at far in the future cannot pin it green either
    a = fr.assess({"generated_at": NOW + timedelta(days=3650), "seq_advanced_at": NOW - timedelta(seconds=200)}, now=NOW)
    assert a["level"] == "broken"


def test_message_shows_the_date_as_well_as_the_time():
    a = fr.assess(_obs(seen_ago=20), now=NOW)
    assert a["message"].startswith("Data as of 2026-10-29 18:00:07 UTC (20 s ago)")


def test_falls_back_to_generated_at_only_when_seq_advanced_at_is_absent():
    a = fr.assess({"generated_at": NOW - timedelta(seconds=70)}, now=NOW)
    assert a["data_age_seconds"] == pytest.approx(70) and a["level"] == "stale" and a["skew_seconds"] is None
    b = fr.assess({"generated_at": NOW - timedelta(seconds=70), "seq_advanced_at": NOW - timedelta(seconds=5)}, now=NOW)
    assert b["data_age_seconds"] == pytest.approx(5)


# ---------------------------------------------------------------- bot alive (11.4)

def test_bot_alive_age_is_skew_proof_and_preferred_over_the_heartbeat():
    # observed 20 s ago, snapshot generated 3 s earlier, alive stamp 40 s before the snapshot: 20 + 40
    seen = NOW - timedelta(seconds=20)
    gen = seen - timedelta(seconds=3)
    a = fr.assess({"seq_advanced_at": seen, "generated_at": gen, "bot_alive_at": gen - timedelta(seconds=40),
                   "bot_heartbeat_at": gen - timedelta(minutes=100)}, now=NOW)
    hb = a["heartbeat"]
    assert hb["level"] == "ok" and hb["source"] == "bot_alive_at"
    assert hb["message"] == "Bot alive 60 s ago" and hb["age_seconds"] == pytest.approx(60)


def test_bot_alive_is_unaffected_by_a_wrong_backend_clock():
    # identical durations, backend clock +10 min: same answer, because only backend-minus-backend is used
    seen = NOW - timedelta(seconds=20)
    for offset in (0, 600, -600):
        gen = seen - timedelta(seconds=3) + timedelta(seconds=offset)
        a = fr.assess({"seq_advanced_at": seen, "generated_at": gen,
                       "bot_alive_at": gen - timedelta(seconds=40)}, now=NOW)
        assert a["heartbeat"]["age_seconds"] == pytest.approx(60)


@pytest.mark.parametrize("behind,level", [(150, "ok"), (160, "ok"), (161, "silent")])
def test_bot_alive_boundary_is_180_seconds(behind, level):
    seen = NOW - timedelta(seconds=20)         # data age 20; alive age = 20 + behind
    gen = seen
    a = fr.assess({"seq_advanced_at": seen, "generated_at": gen,
                   "bot_alive_at": gen - timedelta(seconds=behind)}, now=NOW)
    assert a["heartbeat"]["level"] == level


def test_bot_silent_with_fresh_replication_says_replication_ok_bot_silent():
    seen = NOW - timedelta(seconds=10)
    a = fr.assess({"seq_advanced_at": seen, "generated_at": seen, "bot_alive_at": seen - timedelta(seconds=400)}, now=NOW)
    assert a["level"] == "fresh" and a["heartbeat"]["level"] == "silent"
    assert "replication OK, bot silent" in a["heartbeat"]["message"]
    b = fr.assess({"seq_advanced_at": NOW - timedelta(seconds=400), "generated_at": NOW - timedelta(seconds=400),
                   "bot_alive_at": NOW - timedelta(seconds=800)}, now=NOW)
    assert b["heartbeat"]["level"] == "silent" and "replication OK" not in b["heartbeat"]["message"]


def test_a_bot_alive_stamp_later_than_its_own_snapshot_cannot_confirm_the_bot_is_alive():
    seen = NOW - timedelta(seconds=10)
    a = fr.assess({"seq_advanced_at": seen, "generated_at": seen, "bot_alive_at": seen + timedelta(hours=5)}, now=NOW)
    assert a["heartbeat"]["level"] == "silent" and "cannot confirm" in a["heartbeat"]["message"]


def test_heartbeat_fallback_rule_is_the_45_minute_one_only_without_bot_alive_at():
    seen = NOW - timedelta(seconds=10)
    old = {"seq_advanced_at": seen, "generated_at": seen, "bot_heartbeat_at": seen - timedelta(minutes=30)}
    assert fr.assess(old, now=NOW)["heartbeat"]["level"] == "ok"             # 30 min is fine for the old rule
    assert fr.assess({**old, "bot_alive_at": seen - timedelta(minutes=30)}, now=NOW)["heartbeat"]["level"] == "silent"
    assert fr.assess({**old, "bot_heartbeat_at": None}, now=NOW)["heartbeat"]["level"] == "unknown"


def test_assess_accepts_objects_with_the_new_fields():
    class S:
        generated_at = NOW - timedelta(seconds=8)
        seq_advanced_at = NOW - timedelta(seconds=5)
        bot_alive_at = NOW - timedelta(seconds=30)
        bot_heartbeat_at = None
        checked_at = NOW
        last_ok_at = NOW
        error = None
        consecutive_errors = 0
    a = fr.assess(S(), now=NOW)
    assert a["level"] == "fresh" and a["data_age_seconds"] == pytest.approx(5)
    assert a["heartbeat"]["level"] == "ok"


# ---------------------------------------------------------------- shipped values

def test_worst_case_age_with_the_shipped_defaults_stays_inside_green():
    """Uses the values that actually ship (Dockerfile.frontend, .env.frontend.example, remote.py and the
    publisher/page defaults), not just the code defaults: a Dockerfile that said 30 once made the worst case
    55 s, which is not 'comfortably inside' the 60 s green limit."""
    import re
    from pathlib import Path
    from nba_trading.dashboard import freshness, remote
    from nba_trading.replication import publisher

    root = Path(__file__).resolve().parents[2]
    docker = re.search(r"DASHBOARD_SYNC_SECONDS=(\d+)", (root / "Dockerfile.frontend").read_text())
    env_ex = re.search(r"^DASHBOARD_SYNC_SECONDS=(\d+)", (root / ".env.frontend.example").read_text(), re.M)
    app_src = (root / "nba_trading" / "dashboard" / "app.py").read_text()
    refresh = int(re.search(r"^BANNER_REFRESH_SECONDS = (\d+)", app_src, re.M).group(1))
    body = int(re.search(r"^REFRESH_SECONDS = (\d+)", app_src, re.M).group(1))
    assert docker and env_ex
    shipped_sync = {int(docker.group(1)), int(env_ex.group(1)), remote.DEFAULT_SYNC_SECONDS}
    assert shipped_sync == {10}, shipped_sync                      # one value everywhere

    transfer_margin = 5
    worst = publisher.DEFAULT_HEARTBEAT_SECONDS + max(shipped_sync) + max(refresh, body) + transfer_margin
    assert worst <= 40
    assert worst * 1.5 <= freshness.FRESH_MAX_SECONDS
    assert freshness.assess({"seq_advanced_at": NOW - timedelta(seconds=worst),
                             "generated_at": NOW - timedelta(seconds=worst + 3)}, now=NOW)["level"] == "fresh"
