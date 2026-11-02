"""Remote-mode dashboard: password gate, freshness banner, synced-ledger rendering, remote pause,
credential isolation and frontend packaging. No network, no cloud, no Kalshi: the replication and
control modules are faked (they are built separately) and ledgers live in tmp dirs."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from nba_trading.dashboard import auth, remote  # noqa: E402
from nba_trading.tests.test_dashboard import _seed  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
APP = str(ROOT / "nba_trading" / "dashboard" / "app.py")
PASSWORD = "correct horse battery staple"


def _iso(seconds_ago=0.0, minutes_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago, minutes=minutes_ago)).isoformat()


class Fakes:
    """What the faked replication/control modules saw."""

    def __init__(self):
        self.store = object()              # the DATA store (read-only for the dashboard)
        self.control_store = object()      # the separate CONTROL store (pause.json / ack.json)
        self.ack_state = None              # what read_ack returns (or an Exception to raise)
        self.started = []
        self.pause_calls = []
        self.pause_state = None            # what read_pause returns (or an Exception to raise)
        self.request_error = None
        self.cleared = []


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Remote mode, tmp data dir, a known password, and faked replication/control modules."""
    data_dir = tmp_path / "dash"
    for k in ("API_KEY_ID", "PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DASHBOARD_MODE", "remote")
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SYNC_SECONDS", "30")
    monkeypatch.setenv("LEDGER_FILE", str(tmp_path / "must-not-be-used.sqlite"))
    monkeypatch.setattr(auth, "_sleep", lambda s: None)
    auth._recent_failures.clear()

    fk = Fakes()
    import nba_trading

    store_mod = types.ModuleType("nba_trading.replication.store")
    store_mod.store_from_env = lambda: fk.store

    sub = types.ModuleType("nba_trading.replication.subscriber")
    sub.start_background = lambda store, dest, interval=30: fk.started.append((store, dest, interval))

    def read_state(dest):
        p = Path(dest) / "sync_state.json"
        return json.loads(p.read_text()) if p.exists() else None
    sub.read_state = read_state

    pkg = types.ModuleType("nba_trading.replication")
    pkg.__path__ = []
    pkg.store, pkg.subscriber = store_mod, sub

    control = types.ModuleType("nba_trading.control")

    def request_pause(store, by, reason=""):
        assert store is fk.control_store, "pause must go through the CONTROL store"
        if fk.request_error:
            raise fk.request_error
        fk.pause_calls.append((store, by, reason))

    def read_pause(store):
        assert store is fk.control_store, "pause must be read from the CONTROL store"
        if isinstance(fk.pause_state, Exception):
            raise fk.pause_state
        return fk.pause_state

    def read_ack(store):
        assert store is fk.control_store, "pause/ack must go through the CONTROL store"
        if isinstance(fk.ack_state, Exception):
            raise fk.ack_state
        return fk.ack_state
    control.request_pause, control.read_pause, control.read_ack = request_pause, read_pause, read_ack
    control.control_store_from_env = lambda: fk.control_store
    control.clear_pause = lambda store: fk.cleared.append(store)

    for name, mod in (("nba_trading.replication", pkg), ("nba_trading.replication.store", store_mod),
                      ("nba_trading.replication.subscriber", sub), ("nba_trading.control", control)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(nba_trading, "replication", pkg, raising=False)
    monkeypatch.setattr(nba_trading, "control", control, raising=False)
    remote._store.clear()
    remote._control_store.clear()
    remote._start_subscriber.clear()

    fk.data_dir = data_dir
    return fk


def _sync(fk, ledger=True, age=10, hb_minutes=2, alive_seconds=None, **state):
    """Pretend the subscriber has synced: a ledger copy plus sync_state.json. The seq advanced about
    2 s after the snapshot was generated (frontend clock), so the skew-proof age equals `age` - 2 s."""
    fk.data_dir.mkdir(parents=True, exist_ok=True)
    if ledger:
        _seed(fk.data_dir / "ledger.sqlite", hb_minutes_ago=hb_minutes)
    s = {"checked_at": _iso(1), "last_ok_at": _iso(1), "seq": 7, "generated_at": _iso(age),
         "seq_advanced_at": _iso(max(age - 2, 0)),
         "bot_heartbeat_at": _iso(minutes_ago=hb_minutes), "error": None, "consecutive_errors": 0}
    if alive_seconds is not None:
        s["bot_alive_at"] = _iso(age + alive_seconds)
    s.update(state)
    (fk.data_dir / "sync_state.json").write_text(json.dumps(s))


def _run():
    return AppTest.from_file(APP, default_timeout=30).run()


def _login(at, pw=PASSWORD):
    at.text_input[0].set_value(pw)
    at.button[0].click()
    return at.run()


def _text(at):
    """Everything user-visible, flattened, to assert that something does NOT appear."""
    parts = []
    for coll in (at.title, at.markdown, at.caption, at.info, at.warning, at.error, at.success, at.text):
        parts += [str(e.value) for e in coll]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += [t.label for t in at.tabs]
    return "\n".join(parts)


def _shows_data(at):
    return len(at.tabs) > 0 or len(at.metric) > 0 or len(at.dataframe) > 0


# ---------------------------------------------------------------- password gate


def test_remote_without_a_password_refuses_to_render_data(env, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    _sync(env)
    at = _run()
    assert not at.exception
    assert any("DASHBOARD_PASSWORD" in e.value and "refuses" in e.value for e in at.error)
    assert not _shows_data(at) and not at.text_input        # not even a login form
    assert env.started == []                                  # nothing synced or read either


def test_remote_with_a_blank_password_also_refuses(env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "   ")
    _sync(env)
    at = _run()
    assert any("DASHBOARD_PASSWORD" in e.value for e in at.error) and not _shows_data(at)


def test_wrong_password_is_denied_and_nothing_is_shown(env):
    _sync(env)
    at = _run()
    assert not _shows_data(at) and env.started == []
    assert [t.label for t in at.text_input] == ["Password"]
    at = _login(at, "not the password")
    assert not at.exception
    assert any("Incorrect password" in e.value for e in at.error)
    assert not _shows_data(at) and env.started == []
    shown = _text(at)
    assert PASSWORD not in shown and "not the password" not in shown
    assert str(env.data_dir) not in shown and "ledger" not in shown.lower()


def test_failed_attempts_are_counted_and_delayed(env, monkeypatch):
    waits = []
    monkeypatch.setattr(auth, "_sleep", waits.append)
    _sync(env)
    at = _run()
    at = _login(at, "bad1")
    at = _login(at, "bad2")
    assert any("2 failed attempts" in e.value for e in at.error)
    assert len(waits) == 2 and waits[1] > waits[0] > 0
    assert auth.failure_delay(1) < auth.failure_delay(5) <= auth.MAX_DELAY_SECONDS
    assert auth.failure_delay(10_000) == auth.MAX_DELAY_SECONDS


def test_right_password_shows_the_dashboard(env):
    _sync(env)
    at = _login(_run())
    assert not at.exception, at.exception
    assert len(at.tabs) == 5
    assert at.session_state[auth.AUTH_KEY] is True
    # the form's widget value is cleared on submit and the password is never stored by the app
    assert all(PASSWORD not in str(at.session_state[k]) for k in at.session_state.keys()
               if k != "dashboard_login_password")


def test_password_is_compared_with_hmac_compare_digest(env, monkeypatch):
    calls = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    _sync(env)
    _login(_run())
    assert calls and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)
    src = (ROOT / "nba_trading" / "dashboard" / "auth.py").read_text()
    assert "hmac.compare_digest" in src and "==" not in re.sub(r'"[^"\n]*"|#.*', "", src).replace("!=", "")


def test_check_password_unit():
    assert auth.check_password("abc", "abc")
    assert not auth.check_password("abd", "abc")
    assert not auth.check_password("", "")                  # unset password never matches
    assert not auth.check_password("abc", "")
    assert auth.check_password("pässwörd✓", "pässwörd✓")


def test_logout_returns_to_the_login_form(env):
    _sync(env)
    at = _login(_run())
    assert len(at.tabs) == 5
    logout = [b for b in at.button if b.label == "Log out"]
    assert len(logout) == 1
    logout[0].click()
    at.run()
    assert not _shows_data(at) and [t.label for t in at.text_input] == ["Password"]
    assert not at.session_state[auth.AUTH_KEY] if auth.AUTH_KEY in at.session_state else True


def test_local_mode_has_no_gate_unless_a_password_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_MODE", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    p = tmp_path / "l.sqlite"
    _seed(p)
    monkeypatch.setenv("LEDGER_FILE", str(p))
    at = _run()
    assert len(at.tabs) == 5 and not [b for b in at.button if b.label == "Log out"]

    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setattr(auth, "_sleep", lambda s: None)
    at = _run()
    assert not _shows_data(at) and [t.label for t in at.text_input] == ["Password"]
    at = _login(at)
    assert len(at.tabs) == 5


def test_invalid_mode_is_an_error_not_silently_local(monkeypatch, tmp_path):
    p = tmp_path / "l.sqlite"
    _seed(p)
    monkeypatch.setenv("LEDGER_FILE", str(p))
    monkeypatch.setenv("DASHBOARD_MODE", "remtoe")
    at = _run()
    assert any("DASHBOARD_MODE" in e.value for e in at.error) and not _shows_data(at)


# ---------------------------------------------------------------- remote rendering / freshness


def test_remote_renders_from_the_synced_ledger_and_starts_the_sync_once(env):
    _sync(env, age=12)
    at = _login(_run())
    assert not at.exception, at.exception
    assert len(at.tabs) == 5
    assert env.started == [(env.store, str(env.data_dir), 30)]
    assert any(re.match(r"Data as of \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC \(\d+ s ago\)", s.value)
               for s in at.success)
    assert any(re.fullmatch(r"Bot heartbeat [12] min ago", c.value) for c in at.caption)   # 2 min - 2 s skew-proof
    at.run()                                                  # a rerun must not start another thread
    assert len(env.started) == 1
    assert str(env.data_dir) not in _text(at)                 # no ledger path in remote mode
    assert [m.value for m in at.metric if m.label == "Drawdown"] == ["0.7%"]
    assert not [m for m in at.metric if m.delta and "limit" in str(m.delta)]   # no local-config limits


def test_banner_is_above_the_title_and_amber_red_by_age(env):
    _sync(env, age=150)
    at = _login(_run())
    assert any("lagging" in w.value for w in at.warning) and not at.success
    _sync(env, age=900)
    at.run()
    assert any("STALE" in e.value for e in at.error)


def test_fresh_snapshot_with_a_silent_bot_says_so(env):
    _sync(env, age=5, hb_minutes=60)
    at = _login(_run())
    assert any("replication OK, bot silent" in w.value for w in at.warning)
    assert any(s.value.startswith("Data as of") for s in at.success)       # banner itself is green


def test_waiting_for_first_data_is_a_clear_state_not_a_crash(env):
    at = _login(_run())                                       # nothing synced yet: no ledger, no state
    assert not at.exception, at.exception
    assert any("Waiting for the first data" in i.value for i in at.info)
    assert len(at.tabs) == 0
    assert env.started                                        # but the sync thread was started
    _sync(env)                                                # the first sync lands
    at.run()
    assert len(at.tabs) == 5 and not at.exception


def test_sync_error_is_shown_with_time_since_last_good_sync(env):
    _sync(env, age=100, error="EndpointConnectionError: could not reach bucket", consecutive_errors=4,
          last_ok_at=_iso(seconds_ago=95))
    at = _login(_run())
    msg = " ".join(w.value for w in at.warning)
    assert "EndpointConnectionError" in msg and "4 failures in a row" in msg
    assert re.search(r"Last good sync \d+ s ago", msg)
    assert len(at.tabs) == 5                                  # still serves the last good data


def test_sync_error_before_any_data(env):
    env.data_dir.mkdir(parents=True)
    (env.data_dir / "sync_state.json").write_text(json.dumps(
        {"error": "NoSuchBucket", "consecutive_errors": 2, "generated_at": None, "last_ok_at": None}))
    at = _login(_run())
    assert not at.exception
    assert any("NoSuchBucket" in w.value and "no successful sync yet" in w.value for w in at.warning)
    assert any("Waiting for the first data" in i.value for i in at.info)


def test_unstartable_sync_is_reported_not_raised(env, monkeypatch):
    def boom():
        raise RuntimeError("replication is not configured")
    sys.modules["nba_trading.replication.store"].store_from_env = boom
    at = _login(_run())
    assert not at.exception
    assert any("Could not start the background sync" in e.value for e in at.error)


def test_sync_seconds_parsing(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SYNC_SECONDS", "45")
    assert remote.sync_seconds() == 45
    monkeypatch.setenv("DASHBOARD_SYNC_SECONDS", "junk")
    assert remote.sync_seconds() == remote.DEFAULT_SYNC_SECONDS == 10
    monkeypatch.setenv("DASHBOARD_SYNC_SECONDS", "0")
    assert remote.sync_seconds() == remote.MIN_SYNC_SECONDS
    monkeypatch.delenv("DASHBOARD_DATA_DIR", raising=False)
    assert remote.data_dir() == "/tmp/kalshi-dashboard"


# ---------------------------------------------------------------- remote pause


def test_request_pause_calls_control_with_the_store_and_there_is_no_resume_control(env, tmp_path):
    _sync(env)
    at = _login(_run())
    assert not at.toggle                                       # the local kill-switch toggle is gone
    assert [b.label for b in at.button if b.label != "Log out"] == ["Request pause"]
    assert not any("resume" in (b.label or "").lower() for b in at.button)
    assert any("python -m nba_trading.control resume" in i.value for i in at.info)

    next(t for t in at.text_input if t.label.startswith("Reason")).set_value("injury news")
    next(b for b in at.button if b.label == "Request pause").click()
    at.run()
    assert not at.exception, at.exception
    assert env.pause_calls == [(env.control_store, "dashboard", "injury news")]
    assert any("Pause requested" in s.value for s in at.success)
    assert env.cleared == []                                   # resume/clear is never reachable
    assert not (tmp_path / "STOP_TRADING").exists()


def test_failed_pause_request_is_shown_as_an_error(env):
    _sync(env)
    env.request_error = OSError("AccessDenied")
    at = _login(_run())
    next(b for b in at.button if b.label == "Request pause").click()
    at.run()
    assert any("Could not request the pause" in e.value and "AccessDenied" in e.value for e in at.error)
    assert not [s for s in at.success if "Pause requested" in s.value]


def test_current_remote_pause_state_is_shown(env):
    _sync(env)
    env.pause_state = {"paused": True, "requested_at": "2026-10-29T18:00:00+00:00",
                       "by": "dashboard", "reason": "injury news"}
    at = _login(_run())
    assert ("Remote pause", "REQUESTED") in [(m.label, m.value) for m in at.metric]
    assert any("injury news" in t.value for t in at.text)            # plain text, not markdown
    assert not any("injury news" in w.value for w in at.warning)

    env.pause_state = None
    at.run()
    assert ("Remote pause", "not requested") in [(m.label, m.value) for m in at.metric]


def test_unreadable_pause_state_degrades_to_unknown(env):
    _sync(env)
    env.pause_state = ConnectionError("bucket unreachable")
    at = _login(_run())
    assert not at.exception, at.exception
    assert ("Remote pause", "unknown") in [(m.label, m.value) for m in at.metric]
    assert len(at.tabs) == 5


# ---------------------------------------------------------------- credential isolation


FORBIDDEN_PREFIXES = ("kalshi", "agent_trader", "apscheduler", "nba_trading.main")


def _imports_of(path):
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            yield base
            for a in node.names:
                yield f"{base}.{a.name}"


def test_dashboard_sources_never_import_kalshi_or_the_bot():
    for f in (ROOT / "nba_trading" / "dashboard").glob("*.py"):
        for name in _imports_of(f):
            assert not name.startswith(FORBIDDEN_PREFIXES), f"{f.name} imports {name}"
        tree = ast.parse(f.read_text())
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "clear_pause" not in used, f"{f.name} must not expose resume"


_ISOLATION_SCRIPT = r"""
import os, sys
for k in ("API_KEY_ID", "PRIVATE_KEY_PATH"):
    assert k not in os.environ
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=60).run()
assert not at.exception, at.exception
bad = sorted(m for m in sys.modules if m.split(".")[0] in ("kalshi", "agent_trader", "apscheduler")
             or m == "nba_trading.main")
assert not bad, bad
print("ISOLATION_OK", len(at.tabs))
"""


def test_remote_app_imports_and_runs_without_kalshi_credentials(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("API_KEY_ID", "PRIVATE_KEY_PATH")}
    env.update(DASHBOARD_MODE="remote", DASHBOARD_DATA_DIR=str(tmp_path / "d"),
               REPLICATION_LOCAL_DIR=str(tmp_path / "bucket"), PYTHONPATH=str(ROOT))
    env.pop("DASHBOARD_PASSWORD", None)                        # also proves the refusal path needs no secrets
    out = subprocess.run([sys.executable, "-c", _ISOLATION_SCRIPT, APP], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "ISOLATION_OK" in out.stdout, out.stdout + out.stderr

    env["DASHBOARD_PASSWORD"] = PASSWORD
    out = subprocess.run([sys.executable, "-c", _ISOLATION_SCRIPT, APP], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "ISOLATION_OK" in out.stdout, out.stdout + out.stderr


@pytest.mark.skipif(not (ROOT / "nba_trading" / "replication" / "subscriber.py").exists()
                    or not (ROOT / "nba_trading" / "control.py").exists(),
                    reason="replication/control modules not present yet")
def test_real_replication_and_control_modules_import_without_trading_dependencies(tmp_path):
    script = ("import sys\n"
              "import nba_trading.replication.subscriber, nba_trading.replication.store, nba_trading.control\n"
              "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('kalshi','agent_trader','apscheduler')"
              " or m == 'nba_trading.main')\n"
              "assert not bad, bad\nprint('OK')\n")
    env = {k: v for k, v in os.environ.items() if k not in ("API_KEY_ID", "PRIVATE_KEY_PATH")}
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", script], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "OK" in out.stdout, out.stdout + out.stderr


# ---------------------------------------------------------------- packaging (static checks)


def _dockerfile():
    return (ROOT / "Dockerfile.frontend").read_text()


def test_dockerfile_frontend_runs_as_non_root_with_a_healthcheck():
    d = _dockerfile()
    assert d.startswith("#") and "FROM python:3.13-slim" in d
    users = re.findall(r"^USER\s+(\S+)", d, re.M)
    assert users and users[-1] not in ("root", "0")
    assert "HEALTHCHECK" in d and "/_stcore/health" in d
    assert "DASHBOARD_MODE=remote" in d
    assert "--server.address=0.0.0.0" in d and "8501" in d
    assert re.search(r"^HEALTHCHECK", d, re.M).start() > re.search(r"^USER", d, re.M).start()


def test_dockerfile_frontend_copies_no_secrets_and_installs_no_trading_requirements():
    d = _dockerfile()
    code = "\n".join(line for line in d.splitlines() if not line.lstrip().startswith("#"))
    copies = re.findall(r"^(?:COPY|ADD)\s+(.*)$", code, re.M)
    assert copies
    for c in copies:
        assert ".env" not in c and ".pem" not in c and ".key" not in c
        assert not re.search(r"(^|\s)\.\s+\S*\.?\s*$", c), f"copies the whole repo: {c}"   # no `COPY . .`
        for banned in ("kalshi", "agent_trader", "deploy", "data", "requirements.txt", "requirements-dashboard"):
            assert not re.search(rf"(^|\s){re.escape(banned)}(/|\s|$)", c), f"{banned} in COPY {c}"
    installs = re.findall(r"pip install\s+(.*)", code)
    assert installs == ["-r requirements-frontend.txt"]
    assert "requirements.txt" not in code.replace("requirements-frontend.txt", "")


def test_frontend_requirements_contain_nothing_that_can_trade():
    reqs = [re.split(r"[<>=\s#]", ln.strip(), maxsplit=1)[0].lower()
            for ln in (ROOT / "requirements-frontend.txt").read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert {"streamlit", "pandas", "boto3"} <= set(reqs)
    for banned in ("cryptography", "apscheduler", "requests", "anthropic", "pydantic", "kalshi"):
        assert banned not in reqs


def test_the_files_the_dockerfile_copies_exist_and_the_root_dockerignore_still_protects_secrets():
    assert (ROOT / "strategy_config.py").exists() and (ROOT / "nba_trading").is_dir()
    assert (ROOT / ".streamlit" / "config.toml").exists()
    ignore = (ROOT / ".dockerignore").read_text().split()
    for must in (".env", "*.pem", "data/", "*.sqlite"):
        assert must in ignore


def test_streamlit_config_hardening():
    cfg = (ROOT / ".streamlit" / "config.toml").read_text()
    assert re.search(r"^headless\s*=\s*true", cfg, re.M)
    assert re.search(r"^gatherUsageStats\s*=\s*false", cfg, re.M)
    assert re.search(r"^enableXsrfProtection\s*=\s*true", cfg, re.M)
    assert re.search(r'^toolbarMode\s*=\s*"minimal"', cfg, re.M)
    assert not re.search(r"^address\s*=", cfg, re.M)           # a local `streamlit run` must stay on localhost


# ---------------------------------------------------------------- S3: fragments, logout, login budget


def _fragment_rerun(at):
    """What a browser's auto-rerun (or a crafted BackMsg) does: re-run the REGISTERED fragments only,
    without going through the script top (and therefore without the gate)."""
    import streamlit.testing.v1.local_script_runner as lsr
    ids = list(at._fragment_storage._fragments)
    orig = lsr.RerunData
    lsr.RerunData = lambda **kw: orig(fragment_id_queue=ids, is_fragment_scoped_rerun=True, **kw)
    try:
        at.run()
    finally:
        lsr.RerunData = orig
    return ids


def test_reviewer_r10_logged_out_fragment_rerun_renders_nothing_and_starts_nothing(env, monkeypatch):
    _sync(env)
    at = _login(_run())
    assert len(at.tabs) == 5 and len(env.started) == 1
    at.button(key="dashboard_logout").click()
    at.run()
    assert not _shows_data(at) and [t.label for t in at.text_input] == ["Password"]

    calls = []
    for name in ("ensure_sync_started", "load_sync_state", "current_pause", "current_ack"):
        real = getattr(remote, name)
        monkeypatch.setattr(remote, name, lambda *a, _n=name, _r=real, **k: calls.append(_n) or _r(*a, **k))
    env.started.clear()

    ids = _fragment_rerun(at)
    assert ids, "the fragments are still registered after logout: that is why each must check itself"
    assert not at.exception, at.exception
    assert calls == [] and env.started == []                    # nothing read, nothing started
    assert not at.success and not at.caption and not at.info and not at.tabs and not at.metric
    assert not at.dataframe and not at.warning
    assert auth.AUTH_KEY not in at.session_state


def test_logout_clears_the_session_state_completely(env):
    _sync(env)
    at = _login(_run())
    next(t for t in at.text_input if t.label.startswith("Reason")).set_value("my private note")
    next(b for b in at.button if b.label == "Request pause").click()
    at.run()
    assert "pause_result" in at.session_state
    at.button(key="dashboard_logout").click()
    at.run()
    keys = set(at.session_state.keys())
    assert not (keys & {auth.AUTH_KEY, "pause_result", "pause_reason", "pause_request"}), keys
    assert "my private note" not in " ".join(str(at.session_state[k]) for k in keys)
    assert not _shows_data(at)


def test_fragment_rerun_works_normally_while_logged_in(env):
    _sync(env)
    at = _login(_run())
    _fragment_rerun(at)
    assert not at.exception and len(at.tabs) == 5


@pytest.fixture()
def fake_clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(auth, "_clock", lambda: t[0])
    auth._recent_failures.clear()
    yield t
    auth._recent_failures.clear()


def test_budget_is_process_wide_rolling_and_rejects_rather_than_delays(fake_clock):
    assert auth.LOGIN_BUDGET == 10 and auth.LOGIN_WINDOW_SECONDS == 300
    stamps = [auth._reserve_attempt() for _ in range(auth.LOGIN_BUDGET)]
    assert all(s is not None for s in stamps)
    assert auth._reserve_attempt() is None                       # the 11th is rejected outright
    fake_clock[0] += auth.LOGIN_WINDOW_SECONDS - 1
    assert auth._reserve_attempt() is None                       # still inside the window
    fake_clock[0] += 2
    assert auth._reserve_attempt() is not None                   # the old failures aged out
    # rolling, not a fixed bucket: only slots that actually aged out are free again
    assert auth.recent_failure_count() == 1


def test_a_successful_sign_in_hands_its_slot_back(fake_clock):
    stamp = auth._reserve_attempt()
    assert auth.recent_failure_count() == 1
    auth._release_attempt(stamp)
    assert auth.recent_failure_count() == 0
    auth._release_attempt(stamp)                                 # releasing twice is harmless


def test_budget_is_thread_safe_and_never_overshoots(fake_clock):
    import threading
    got, lock = [], threading.Lock()

    def worker():
        r = auth._reserve_attempt()
        with lock:
            got.append(r)
    threads = [threading.Thread(target=worker) for _ in range(60)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for g in got if g is not None) == auth.LOGIN_BUDGET


def test_once_the_budget_is_used_up_even_the_right_password_is_rejected_with_the_generic_message(env, fake_clock):
    _sync(env)
    for _ in range(auth.LOGIN_BUDGET):                           # failures by OTHER sessions
        assert auth._reserve_attempt() is not None
    at = _login(_run(), PASSWORD)
    assert not at.exception
    assert auth.AUTH_KEY not in at.session_state and not _shows_data(at) and env.started == []
    msg = " ".join(e.value for e in at.error)
    assert msg.startswith(auth.FAILURE_MESSAGE)
    assert "budget" not in msg.lower() and "locked" not in msg.lower() and "too many" not in msg.lower()
    # identical wording to an ordinary wrong password (nothing about the budget leaks)
    fake_clock[0] += auth.LOGIN_WINDOW_SECONDS + 1
    auth._recent_failures.clear()
    at2 = _login(_run(), "wrong")
    assert " ".join(e.value for e in at2.error) == msg

    fake_clock[0] += 1
    at3 = _login(_run(), PASSWORD)                               # budget free again: the right password works
    assert len(at3.tabs) == 5


def test_failures_in_separate_sessions_share_one_budget(env, fake_clock):
    _sync(env)
    for _ in range(3):
        _login(_run(), "nope")                                   # three different browser sessions
    assert auth.recent_failure_count() == 3
    at = _login(_run(), PASSWORD)
    assert len(at.tabs) == 5                                     # a right answer is not charged
    assert auth.recent_failure_count() == 3


def test_the_per_session_delay_still_applies(env, monkeypatch):
    waits = []
    monkeypatch.setattr(auth, "_sleep", waits.append)
    _sync(env)
    at = _run()
    _login(at, "a")
    _login(at, "b")
    assert len(waits) == 2 and waits[1] > waits[0] > 0


def test_password_is_never_logged_printed_or_displayed(env, caplog, capsys):
    import logging
    caplog.set_level(logging.DEBUG)
    _sync(env)
    guess = "hunter2-very-secret-guess"
    at = _login(_run(), guess)
    at = _login(at, PASSWORD)
    out = capsys.readouterr()
    assert guess not in caplog.text and PASSWORD not in caplog.text
    assert guess not in out.out + out.err and PASSWORD not in out.out + out.err
    assert guess not in _text(at) and PASSWORD not in _text(at)
    src = (ROOT / "nba_trading" / "dashboard" / "auth.py").read_text()
    assert not re.search(r"\b(print|logging|logger|log)\b\s*[.(]", re.sub(r'"""[\s\S]*?"""|#.*', "", src))


# ---------------------------------------------------------------- F6: pause panel


HOSTILE = "[click me](http://evil.example) **bold** <script>alert(1)</script> ![x](http://evil/x.png) $$ `code`"


def test_pause_by_and_reason_are_plain_text_never_markdown(env):
    _sync(env)
    env.pause_state = {"paused": True, "requested_at": _iso(5), "by": HOSTILE, "reason": HOSTILE + "\nline2"}
    at = _login(_run())
    assert not at.exception
    texts = [t.value for t in at.text]
    assert any("[click me](http://evil.example)" in t for t in texts)         # shown literally, via st.text
    for coll in (at.markdown, at.warning, at.error, at.info, at.success, at.caption):
        for e in coll:
            assert "evil" not in e.value and "<script>" not in e.value, (type(e).__name__, e.value)
    assert not any("\n" in t for t in texts)                                  # one line


def test_pause_fields_are_truncated(env):
    _sync(env)
    env.pause_state = {"paused": True, "requested_at": _iso(5), "by": "b" * 5000, "reason": "r" * 5000}
    at = _login(_run())
    shown = " ".join(t.value for t in at.text)
    assert "b" * (remote.MAX_BY_CHARS + 1) not in shown and "r" * (remote.MAX_REASON_CHARS + 1) not in shown
    assert "b" * (remote.MAX_BY_CHARS - 5) in shown and "r" * (remote.MAX_REASON_CHARS - 5) in shown


def test_plain_strips_control_characters_and_non_strings():
    assert remote.plain("a\x00b\r\nc d") == "a b c d"
    assert remote.plain(None) == "" and remote.plain(12345) == "12345" and remote.plain({"x": 1}).startswith("{")
    assert remote.plain("x" * 500, 10) == "x" * 9 + "…"


def test_pause_request_and_ack_are_shown_and_acknowledged(env):
    _sync(env)
    req = _iso(20)
    env.pause_state = {"paused": True, "requested_at": req, "by": "dashboard", "reason": "injury"}
    env.ack_state = {"acked_at": _iso(5), "pause_requested_at": req, "latched": True, "by": "bot"}
    at = _login(_run())
    texts = " | ".join(t.value for t in at.text)
    assert f"requested by dashboard at {req}" in texts and "acknowledged by bot at" in texts
    assert not [w for w in at.warning if "not acknowledged" in w.value]


def test_missing_ack_after_two_poll_intervals_warns_but_not_before(env):
    _sync(env)
    env.pause_state = {"paused": True, "requested_at": _iso(10), "by": "dashboard", "reason": ""}
    at = _login(_run())
    assert not [w for w in at.warning if "not acknowledged" in w.value]               # only 10 s old: be patient
    assert any("not yet acknowledged" in t.value for t in at.text)
    assert remote.ACK_WARN_SECONDS == 60
    env.pause_state = {"paused": True, "requested_at": _iso(61), "by": "dashboard", "reason": ""}
    at.run()
    warns = [w.value for w in at.warning if "not acknowledged" in w.value]
    assert warns and "60 s" in warns[0]


def test_pause_status_pure_logic():
    now = datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)
    iso = lambda s: (now - timedelta(seconds=s)).isoformat()
    req = {"paused": True, "requested_at": iso(61), "by": "dashboard", "reason": ""}
    assert remote.pause_status(req, None, None, None, now)["warn_no_ack"] is True
    req["requested_at"] = iso(60)
    assert remote.pause_status(req, None, None, None, now)["warn_no_ack"] is False
    req["requested_at"] = "garbage"                                                    # unreadable time: warn
    assert remote.pause_status(req, None, None, None, now)["warn_no_ack"] is True
    ack = {"acked_at": iso(1), "pause_requested_at": "2020-01-01T00:00:00+00:00", "latched": True}
    st_ = remote.pause_status({**req, "requested_at": iso(500)}, None, ack, None, now)
    assert st_["ack"] == "acked" and not st_["warn_no_ack"]
    assert any("(for the request at 2020" in line for line in st_["lines"])            # ack is for an older request
    assert remote.pause_status(None, None, None, None, now)["state"] == "not_requested"
    assert remote.pause_status(None, None, ack, None, now)["state"] == "latched_only"  # bucket flag gone, bot latched
    assert remote.pause_status(None, "boom", None, None, now)["state"] == "unknown"
    assert remote.pause_status(req, None, None, "ack unreadable", now)["ack"] == "unknown"


def test_latched_pause_with_a_vanished_bucket_flag_is_shown(env):
    _sync(env)
    env.pause_state = None
    env.ack_state = {"acked_at": _iso(100), "pause_requested_at": _iso(200), "latched": True, "by": "bot"}
    at = _login(_run())
    assert any("latched" in w.value for w in at.warning)
    assert ("Remote pause", "LATCHED") in [(m.label, m.value) for m in at.metric]


def test_exceptions_from_read_pause_and_read_ack_degrade_to_unknown_without_crashing(env):
    _sync(env)
    env.pause_state = {"paused": True, "requested_at": _iso(100), "by": "dashboard", "reason": ""}
    env.ack_state = TimeoutError("ack bucket slow " + HOSTILE)
    at = _login(_run())
    assert not at.exception, at.exception
    assert len(at.tabs) == 5
    texts = " | ".join(t.value for t in at.text)
    assert "Bot acknowledgement: unknown" in texts
    assert not any("evil" in e.value for e in list(at.markdown) + list(at.warning) + list(at.error) + list(at.info))
    env.pause_state = ValueError("pause.json is malformed")
    env.ack_state = None
    at.run()
    assert not at.exception and ("Remote pause", "unknown") in [(m.label, m.value) for m in at.metric]


def test_pause_goes_through_the_control_store_not_the_data_store(env):
    _sync(env)
    assert env.control_store is not env.store
    at = _login(_run())
    next(b for b in at.button if b.label == "Request pause").click()
    at.run()
    assert [c[0] is env.control_store for c in env.pause_calls] == [True]


def test_remote_module_uses_only_the_documented_control_names():
    tree = ast.parse((ROOT / "nba_trading" / "dashboard" / "remote.py").read_text())    # code only, no docstrings
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "control"}
    assert used == {"control_store_from_env", "read_pause", "read_ack", "request_pause"}, used


# ---------------------------------------------------------------- bot alive / banner in the app


def test_app_shows_bot_alive_and_silent(env):
    _sync(env, age=5, alive_seconds=20)
    at = _login(_run())
    assert any(re.fullmatch(r"Bot alive \d+ s ago", c.value) for c in at.caption)
    assert ("Bot", "alive") in [(m.label, m.value) for m in at.metric]
    _sync(env, age=5, alive_seconds=400)
    at.run()
    assert any("replication OK, bot silent" in w.value for w in at.warning)
    assert ("Bot", "SILENT") in [(m.label, m.value) for m in at.metric]


def test_app_banner_flags_clock_skew_and_is_never_green(env):
    _sync(env, age=5, generated_at=_iso(seconds_ago=-300))       # snapshot stamped 5 min in the future
    at = _login(_run())
    assert any("clock skew between hosts" in w.value for w in at.warning)
    assert not at.success


def test_app_banner_frozen_seq_goes_amber_then_red(env):
    _sync(env, age=5, seq_advanced_at=_iso(90), generated_at=_iso(93))
    at = _login(_run())
    assert any("lagging" in w.value for w in at.warning) and not at.success
    _sync(env, age=5, seq_advanced_at=_iso(300), generated_at=_iso(303))
    at.run()
    assert any("STALE" in e.value for e in at.error)


# ---------------------------------------------------------------- data safety


def test_a_corrupt_synced_ledger_shows_the_retry_message_not_a_traceback(env):
    _sync(env, ledger=False)
    (env.data_dir / "ledger.sqlite").write_bytes(b"this is not a sqlite database" * 100)
    at = _login(_run())
    assert not at.exception, at.exception
    assert any("could not be read yet" in e.value and "Retrying" in e.value for e in at.error)
    assert not _shows_data(at)


def test_a_synced_ledger_with_a_missing_table_shows_the_retry_message(env):
    import sqlite3
    _sync(env, ledger=False)
    conn = sqlite3.connect(env.data_dir / "ledger.sqlite")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    at = _login(_run())
    assert not at.exception, at.exception
    assert any("could not be read yet" in e.value for e in at.error)


def test_remote_app_touches_no_trading_module_even_after_login_and_pause(env, monkeypatch):
    """Block every trading module at import time, then use the whole remote app: log in, render, request a
    pause. If anything in the dashboard path imported or called into them, the run would raise ImportError."""
    for name in ("kalshi", "kalshi.client", "kalshi.preflight", "agent_trader", "apscheduler",
                 "apscheduler.schedulers.background", "nba_trading.main", "nba_trading.executor"):
        monkeypatch.setitem(sys.modules, name, None)               # None => `import name` raises ImportError
    _sync(env)
    at = _login(_run())
    assert not at.exception, at.exception
    assert len(at.tabs) == 5
    next(b for b in at.button if b.label == "Request pause").click()
    at.run()
    assert not at.exception, at.exception
    assert env.pause_calls and any("Pause requested" in s.value for s in at.success)


# ---------------------------------------------------------------- shipped configuration agrees


def test_sync_interval_default_is_10_everywhere():
    docker = re.search(r"DASHBOARD_SYNC_SECONDS=(\d+)", _dockerfile())
    env_ex = re.search(r"^DASHBOARD_SYNC_SECONDS=(\d+)", (ROOT / ".env.frontend.example").read_text(), re.M)
    assert int(docker.group(1)) == int(env_ex.group(1)) == remote.DEFAULT_SYNC_SECONDS == 10
    docs = (ROOT / "docs" / "frontend-deployment.md").read_text()
    assert "`10` everywhere" in docs


def test_frontend_docs_state_the_real_thresholds_and_key_model():
    docs = (ROOT / "docs" / "frontend-deployment.md").read_text()
    assert not re.search(r"\b(90|300) ?s\b", docs)
    for needle in ("up to 60 s", "60 - 180 s", "over 180 s", "clock skew between hosts", "Bot alive",
                   "read only", "CONTROL", "ack.json", "What a leaked frontend key or password can do",
                   "cannot resume"):
        assert needle in docs, needle


# ---------------------------------------------------------------- real replication + control modules


@pytest.mark.skipif(not all(hasattr(__import__("nba_trading.control", fromlist=["x"]), n)
                            for n in ("control_store_from_env", "read_ack", "write_ack")),
                    reason="control module without the control-store API")
def test_end_to_end_with_the_real_modules_and_two_separate_buckets(tmp_path, monkeypatch):
    """Real publisher -> LocalDirStore data bucket -> real subscriber thread -> dashboard, and the pause
    through a SEPARATE control directory with the real control.read_pause / read_ack."""
    import time
    from nba_trading import control
    from nba_trading.ledger import Ledger
    from nba_trading.replication.publisher import publish_once
    from nba_trading.replication.store import LocalDirStore

    src = tmp_path / "src.sqlite"
    led = Ledger(str(src))
    led.record_event("INFO", "heartbeat", "ok")
    led.close()
    data_bucket, control_bucket = tmp_path / "databucket", tmp_path / "controlbucket"
    publish_once(LocalDirStore(str(data_bucket)), str(src))

    for k in ("API_KEY_ID", "PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DASHBOARD_MODE", "remote")
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path / "dash"))
    monkeypatch.setenv("DASHBOARD_SYNC_SECONDS", "5")
    monkeypatch.setenv("REPLICATION_LOCAL_DIR", str(data_bucket))
    monkeypatch.setenv("CONTROL_LOCAL_DIR", str(control_bucket))
    monkeypatch.setattr(auth, "_sleep", lambda s: None)
    auth._recent_failures.clear()
    remote._store.clear(); remote._control_store.clear(); remote._start_subscriber.clear()
    try:
        at = _login(_run())
        deadline = time.time() + 20
        while time.time() < deadline and not at.tabs:
            time.sleep(0.3)
            at.run()
        assert not at.exception, at.exception
        assert len(at.tabs) == 5, "the real subscriber never delivered the ledger"
        assert any(s.value.startswith("Data as of ") for s in at.success), [w.value for w in at.warning]

        next(t for t in at.text_input if t.label.startswith("Reason")).set_value("e2e")
        next(b for b in at.button if b.label == "Request pause").click()
        at.run()
        assert any("Pause requested" in s.value for s in at.success)
        cstore = control.control_store_from_env()
        doc = control.read_pause(cstore)
        assert doc["paused"] is True and doc["by"] == "dashboard" and doc["reason"] == "e2e"
        assert not list(data_bucket.rglob("pause.json")), "the pause must not touch the DATA bucket"

        control.write_ack(cstore, pause_requested_at=doc["requested_at"])
        at.run()
        assert any("acknowledged by bot at" in t.value for t in at.text)
    finally:
        remote._store.clear(); remote._control_store.clear(); remote._start_subscriber.clear()
