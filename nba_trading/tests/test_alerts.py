import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from nba_trading import alerts, config

NTFY = "https://ntfy.example.com/secret-topic-123"
HOOK = "https://hooks.example.com/services/SECRET/TOKEN"
HC = "https://hc-ping.example.com/abc-uuid"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name, val in [("ALERT_NTFY_URL", ""), ("ALERT_WEBHOOK_URL", ""),
                      ("ALERT_MIN_LEVEL", "CRITICAL"), ("ALERT_COOLDOWN_MINUTES", 15),
                      ("HEALTHCHECK_URL", "")]:
        monkeypatch.setattr(config, name, val, raising=False)
    clock = Clock()
    monkeypatch.setattr(alerts, "_clock", clock)
    post = MagicMock()
    get = MagicMock()
    monkeypatch.setattr(alerts.requests, "post", post)
    monkeypatch.setattr(alerts.requests, "get", get)
    alerts.reset()
    yield type("E", (), {"clock": clock, "post": post, "get": get})
    alerts.flush()
    alerts.reset()


def test_noop_when_unconfigured(env):
    assert alerts.notify("CRITICAL", "k", "m") is False
    alerts.ping_healthcheck("ok")
    alerts.flush()
    env.post.assert_not_called()
    env.get.assert_not_called()


def test_level_threshold(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    assert alerts.notify("ERROR", "a", "m") is False
    assert alerts.notify("info", "b", "m") is False
    assert alerts.notify("CRITICAL", "c", "m") is True
    alerts.flush()
    assert env.post.call_count == 1

    monkeypatch.setattr(config, "ALERT_MIN_LEVEL", "warning")
    assert alerts.notify("WARNING", "d", "m") is True
    assert alerts.notify("INFO", "e", "m") is False
    alerts.flush()
    assert env.post.call_count == 2


def test_ntfy_request_shape(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    alerts.notify("CRITICAL", "drawdown", "Halted: drawdown 12%")
    alerts.flush()
    args, kwargs = env.post.call_args
    assert args[0] == NTFY
    assert kwargs["data"] == b"Halted: drawdown 12%"
    assert kwargs["headers"]["Title"] == "[CRITICAL] drawdown"
    assert kwargs["headers"]["Priority"] == "5"
    assert kwargs["headers"]["Tags"] == "rotating_light"
    assert kwargs["timeout"] <= 5


def test_ntfy_non_ascii_title_is_safe(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    alerts.notify("CRITICAL", "kind’s\nx", "m")
    alerts.flush()
    title = env.post.call_args.kwargs["headers"]["Title"]
    title.encode("ascii")
    assert "\n" not in title


def test_webhook_json_shape(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", HOOK)
    alerts.notify("CRITICAL", "kill", "stopped")
    alerts.flush()
    args, kwargs = env.post.call_args
    assert args[0] == HOOK
    assert kwargs["headers"]["Content-Type"] == "application/json"
    body = json.loads(kwargs["data"])
    assert body["level"] == "CRITICAL"
    assert body["kind"] == "kill"
    assert body["message"] == "stopped"
    assert body["text"] == "[CRITICAL] kill: stopped"
    assert "timestamp" in body
    assert kwargs["timeout"] <= 5


def test_both_channels(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", HOOK)
    alerts.notify("CRITICAL", "k", "m")
    alerts.flush()
    assert {c.args[0] for c in env.post.call_args_list} == {NTFY, HOOK}


def test_dedupe_within_cooldown_and_resend_after(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    assert alerts.notify("CRITICAL", "loop", "m1") is True
    env.clock.t += 14 * 60
    assert alerts.notify("CRITICAL", "loop", "m2") is False
    env.clock.t += 2 * 60
    assert alerts.notify("CRITICAL", "loop", "m3") is True
    alerts.flush()
    assert env.post.call_count == 2


def test_cooldown_configurable(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    monkeypatch.setattr(config, "ALERT_COOLDOWN_MINUTES", 1)
    alerts.notify("CRITICAL", "k", "m")
    env.clock.t += 30
    assert alerts.notify("CRITICAL", "k", "m") is False
    env.clock.t += 31
    assert alerts.notify("CRITICAL", "k", "m") is True


def test_different_kinds_not_deduped(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    assert alerts.notify("CRITICAL", "a", "m") is True
    assert alerts.notify("CRITICAL", "b", "m") is True
    alerts.flush()
    assert env.post.call_count == 2


def test_below_threshold_does_not_start_cooldown(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    alerts.notify("INFO", "k", "m")
    assert alerts.notify("CRITICAL", "k", "m") is True


@pytest.mark.parametrize("exc", [RuntimeError("boom"), TimeoutError("slow")])
def test_exceptions_swallowed(env, monkeypatch, exc, caplog):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", HOOK)
    env.post.side_effect = exc
    alerts.notify("CRITICAL", "k", "m")
    alerts.flush()  # must not raise
    assert env.post.call_count == 2


def test_requests_exception_with_url_is_redacted(env, monkeypatch, caplog):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    env.post.side_effect = alerts.requests.exceptions.ConnectionError(f"failed connecting to {NTFY}")
    with caplog.at_level("DEBUG"):
        alerts.notify("CRITICAL", "k", "m")
        alerts.flush()
    assert "secret-topic-123" not in caplog.text
    assert "ntfy.example.com" in caplog.text


def test_success_logs_do_not_leak_urls(env, monkeypatch, caplog):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", HOOK)
    env.post.return_value = MagicMock(status_code=200)
    with caplog.at_level("DEBUG"):
        alerts.notify("CRITICAL", "k", "m")
        alerts.flush()
    assert "secret-topic-123" not in caplog.text
    assert "SECRET" not in caplog.text


def test_bad_status_logged_not_raised(env, monkeypatch, caplog):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    env.post.return_value = MagicMock(status_code=500)
    with caplog.at_level("WARNING"):
        alerts.notify("CRITICAL", "k", "m")
        alerts.flush()
    assert "rejected (500)" in caplog.text
    assert "secret-topic-123" not in caplog.text


def test_notify_never_raises_on_bad_inputs(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    monkeypatch.setattr(config, "ALERT_COOLDOWN_MINUTES", "garbage")
    assert alerts.notify(None, None, None) is False  # INFO < CRITICAL
    assert alerts.notify("CRITICAL", object(), object()) is True
    alerts.flush()


def test_non_blocking(env, monkeypatch):
    monkeypatch.setattr(config, "ALERT_NTFY_URL", NTFY)
    release, finished = threading.Event(), threading.Event()

    def slow_post(*a, **k):
        release.wait(5)
        finished.set()

    env.post.side_effect = slow_post
    alerts.notify("CRITICAL", "slow", "m")
    # Deterministic (no wall-clock threshold): the sender is still blocked on `release`,
    # so if notify() had waited for it, `finished` would already be set.
    assert not finished.is_set()
    release.set()
    alerts.flush()
    assert finished.is_set() and env.post.call_count == 1


def test_healthcheck_ok_and_fail(env, monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", HC)
    alerts.ping_healthcheck("ok")
    alerts.ping_healthcheck("fail")
    alerts.flush()
    urls = sorted(c.args[0] for c in env.get.call_args_list)
    assert urls == [HC, HC + "/fail"]
    assert all(c.kwargs["timeout"] <= 5 for c in env.get.call_args_list)


def test_healthcheck_trailing_slash_and_explicit_url(env):
    alerts.ping_healthcheck("fail", url=HC + "/")
    alerts.flush()
    assert env.get.call_args.args[0] == HC + "/fail"


def test_healthcheck_not_deduped(env, monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", HC)
    alerts.ping_healthcheck("ok")
    alerts.ping_healthcheck("ok")
    alerts.flush()
    assert env.get.call_count == 2


def test_healthcheck_exception_swallowed_and_redacted(env, monkeypatch, caplog):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", HC)
    env.get.side_effect = alerts.requests.exceptions.Timeout(f"timed out {HC}")
    with caplog.at_level("DEBUG"):
        alerts.ping_healthcheck("ok")
        alerts.flush()
    assert "abc-uuid" not in caplog.text


def test_healthcheck_non_blocking(env, monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", HC)
    release, finished = threading.Event(), threading.Event()

    def slow_get(*a, **k):
        release.wait(5)
        finished.set()

    env.get.side_effect = slow_get
    alerts.ping_healthcheck("ok")
    assert not finished.is_set()          # returned while the request was still in flight
    release.set()
    alerts.flush()
    assert finished.is_set()
