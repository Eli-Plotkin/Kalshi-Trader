"""Password gate for the dashboard.

Remote mode (DASHBOARD_MODE=remote): NOTHING renders until the correct DASHBOARD_PASSWORD is
entered; if no password is configured the app refuses to show data at all. Local mode has no
gate unless DASHBOARD_PASSWORD is set (it is localhost/tunnel only).

The password is never logged, displayed, or put in session state. The comparison is constant-time
(hmac.compare_digest over SHA-256 digests, so length differences do not leak either).

Brute-force limits:
  - per session: a growing delay after every failure (0.5 s, 1 s, ... capped at 5 s);
  - process-wide, across ALL sessions: a rolling budget of LOGIN_BUDGET failures per
    LOGIN_WINDOW_SECONDS. Once it is used up, further attempts are REJECTED without even looking at
    the password (a correct password included) until old failures age out of the window. A slot is
    reserved before the password is checked and handed back on success, so concurrent sessions cannot
    overshoot the budget. The rejection looks exactly like a wrong password to the viewer.
This is a small shared-password gate, not an account system: always serve it over TLS and use a long
random password (see docs/frontend-deployment.md).

Every Streamlit fragment must call `require_authenticated()` itself: a fragment-scoped rerun does
not go through `gate()`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import deque

import streamlit as st

PASSWORD_ENV = "DASHBOARD_PASSWORD"
AUTH_KEY = "dashboard_authenticated"
FAIL_KEY = "dashboard_login_failures"

DELAY_STEP_SECONDS = 0.5
MAX_DELAY_SECONDS = 5.0
LOGIN_BUDGET = 10                 # failed attempts allowed process-wide ...
LOGIN_WINDOW_SECONDS = 300.0      # ... per rolling window of this length
GLOBAL_WINDOW_SECONDS = LOGIN_WINDOW_SECONDS    # (old name, kept)

FAILURE_MESSAGE = "Incorrect password, or sign-in is temporarily unavailable"

_sleep = time.sleep          # indirection so tests do not really sleep
_clock = time.monotonic      # indirection so tests control the window
_lock = threading.Lock()
_recent_failures = deque()   # clock() stamps of failed / in-flight attempts, all sessions, in the window


def configured_password() -> str:
    """The configured password, or "" when unset/blank. Never log or display the result."""
    pw = os.environ.get(PASSWORD_ENV, "")
    return pw if pw.strip() else ""


def check_password(candidate: str, expected: str) -> bool:
    """Constant-time comparison. An empty expected password never matches anything."""
    if not expected:
        return False
    a = hashlib.sha256((candidate or "").encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def _prune(now: float) -> None:
    while _recent_failures and now - _recent_failures[0] > LOGIN_WINDOW_SECONDS:
        _recent_failures.popleft()


def _reserve_attempt():
    """Take one slot of the process-wide budget. Returns the slot's stamp, or None when the budget
    is used up (the attempt must then be rejected without checking the password)."""
    with _lock:
        now = _clock()
        _prune(now)
        if len(_recent_failures) >= LOGIN_BUDGET:
            return None
        _recent_failures.append(now)
        return now


def _release_attempt(stamp) -> None:
    """A successful sign-in does not count against the budget: hand its slot back."""
    with _lock:
        try:
            _recent_failures.remove(stamp)
        except ValueError:
            pass


def recent_failure_count() -> int:
    with _lock:
        _prune(_clock())
        return len(_recent_failures)


def failure_delay(session_failures: int, recent_global_failures: int = 0) -> float:
    """Seconds to wait after a failed attempt: grows with the failure count, capped."""
    n = max(session_failures, recent_global_failures)
    return min(MAX_DELAY_SECONDS, DELAY_STEP_SECONDS * max(n, 1))


def is_authenticated() -> bool:
    return bool(st.session_state.get(AUTH_KEY))


def is_gated(remote: bool) -> bool:
    """True when a password is required to see anything."""
    return remote or bool(configured_password())


def _login_form(expected: str) -> None:
    st.title("Sign in")
    with st.form("dashboard_login", clear_on_submit=True):
        candidate = st.text_input("Password", type="password", key="dashboard_login_password")
        submitted = st.form_submit_button("Sign in")
    if not submitted:
        return
    stamp = _reserve_attempt()
    ok = stamp is not None and check_password(candidate, expected)
    candidate = None                      # never keep, log or display what was typed
    if ok:
        _release_attempt(stamp)
        st.session_state[AUTH_KEY] = True
        st.session_state[FAIL_KEY] = 0
        st.rerun()
    n = int(st.session_state.get(FAIL_KEY, 0)) + 1
    st.session_state[FAIL_KEY] = n
    _sleep(failure_delay(n, recent_failure_count()))
    # One message for "wrong password" and "process-wide budget used up": the viewer cannot tell.
    st.error(f"{FAILURE_MESSAGE} ({n} failed attempt{'s' if n != 1 else ''} this session).")


def gate(remote: bool) -> None:
    """Call before rendering anything. Returns only if the viewer may see the dashboard;
    otherwise renders the refusal / login form and halts the script."""
    expected = configured_password()
    if remote and not expected:
        st.error(
            f"Remote mode needs a password and {PASSWORD_ENV} is not set, so this dashboard refuses "
            f"to show any data. Set {PASSWORD_ENV} in the host's environment (or secrets) to a long "
            "random value and restart the app.")
        st.stop()
    if not expected:              # local mode, no password configured: no gate
        return
    if is_authenticated():
        return
    _login_form(expected)
    st.stop()


def require_authenticated(remote: bool) -> None:
    """Call at the top of EVERY fragment and widget callback. A fragment-scoped rerun (or a stored
    fragment that outlives a logout) does not pass through `gate()`, so it must check for itself:
    when the viewer is not signed in this renders nothing and halts the run."""
    if is_gated(remote) and not is_authenticated():
        st.stop()


def clear_session() -> None:
    """Forget everything about this viewer: the auth flag and every cached value/widget state."""
    for key in list(st.session_state.keys()):
        try:
            del st.session_state[key]
        except Exception:  # noqa: BLE001 - a widget-bound key that cannot be deleted mid-run
            pass
    st.session_state.pop(AUTH_KEY, None)


def logout_button() -> None:
    """A 'Log out' button, shown only while a password gate is active and the viewer is signed in."""
    if not is_authenticated():
        return
    if st.button("Log out", key="dashboard_logout"):
        clear_session()
        st.rerun()
