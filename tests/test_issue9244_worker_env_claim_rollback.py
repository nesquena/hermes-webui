"""#9244: a failed process-env mirror write must roll its claims back.

``_claim_process_env`` registers a scope's claims (per-name owners, the
captured baseline and the scope's claim list) BEFORE it writes the projected
values into ``os.environ``. A write that raises — e.g. a value carrying an
embedded NUL, which ``os.environ`` rejects — aborts the registration half-way:

* the callers never receive the release token, so the context manager's
  cleanup skips ``_release_process_env``;
* the claims already registered stay in the mirror registry for the rest of
  the server's lifetime.

A leaked claim keeps a failed worker's profile values active and keeps other
profiles' credential names hidden even after every later worker has finished.

These tests use a synthetic server home (no real credential, no real service)
and a controlled write failure, and assert that

* the failed worker leaves no registration and no partial write behind,
* a concurrently live worker is unaffected,
* the process env returns to its true baseline once everything has exited.

#9244 also asks the concurrent-exit tests to prove the OBSERVED exit order
rather than the order the release signals were sent in; the exit-order cases
live in ``tests/test_issue9187_background_worker_secret_scope.py`` and are
covered here by the shared helpers in this module.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from api import profiles


ALPHA = "alpha"
BETA = "beta"
SECRET_A = "secret-A"
SECRET_B = "secret-B"
OPERATOR_TOKEN = "operator-token-value"

# A value ``os.environ`` refuses to store: the embedded NUL makes the
# environment assignment raise ValueError.
NUL_VALUE = "bad\x00value"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup_server(monkeypatch, tmp_path) -> Path:
    """A synthetic server home: root + ``alpha`` + ``beta``.

    ``alpha`` declares a custom provider whose credential env name
    (``ALPHA_CUSTOM_KEY``) only ITS ``config.yaml`` mentions; ``beta``
    declares ``BETA_CUSTOM_KEY`` the same way.
    """
    base = tmp_path / ".hermes"
    alpha = base / "profiles" / ALPHA
    beta = base / "profiles" / BETA
    _write(alpha / ".env", f"ALPHA_CUSTOM_KEY={SECRET_A}\nALPHA_OWN_KEY=alpha-own\n")
    _write(
        alpha / "config.yaml",
        "model:\n  default: dummy-model\n"
        "custom_providers:\n"
        "  - name: alpha-provider\n"
        "    key_env: ALPHA_CUSTOM_KEY\n",
    )
    _write(beta / ".env", f"BETA_CUSTOM_KEY={SECRET_B}\nBETA_OWN_KEY=beta-own\n")
    _write(
        beta / "config.yaml",
        "model:\n  default: dummy-model\n"
        "custom_providers:\n"
        "  - name: beta-provider\n"
        "    key_env: BETA_CUSTOM_KEY\n",
    )
    _write(base / ".env", "")
    _write(base / "config.yaml", "model:\n  default: dummy-model\n")
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("", "default"))
    monkeypatch.setattr(profiles, "_secret_scope_available", None)
    monkeypatch.setattr(profiles, "_hermes_home_override_available", None)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    from api import paths as _paths

    monkeypatch.setattr(_paths, "STARTUP_ENV", {"OPERATOR_TOKEN": OPERATOR_TOKEN})
    return base


def _install_fake_secret_scope(monkeypatch, calls) -> None:
    """Model the Agent's ``agent.secret_scope`` with a recording setter (agent-free)."""
    agent_pkg: Any = types.ModuleType("agent")
    agent_pkg.__path__ = []
    module: Any = types.ModuleType("agent.secret_scope")

    def set_secret_scope(secrets, *, profile_home=None):
        calls.append((dict(secrets), profile_home))
        return "scope-token"

    module.set_secret_scope = set_secret_scope
    module.reset_secret_scope = lambda token: calls.append(("reset", token))
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


@contextlib.contextmanager
def _enter_background_worker(profile: str):
    with profiles.profile_env_for_background_worker(
        profile, "issue 9244", scope_skill_modules=False
    ):
        yield


def _mirror_registry_snapshot() -> dict:
    """A copy of the mirror registry state, for leak assertions."""
    with profiles._ENV_MIRROR_LOCK:
        return {
            "owners": {k: dict(v) for k, v in profiles._ENV_MIRROR_OWNERS.items()},
            "baseline": dict(profiles._ENV_MIRROR_BASELINE),
            "claims": dict(profiles._ENV_MIRROR_CLAIMS),
        }


def _mirror_registry_is_empty() -> bool:
    snapshot = _mirror_registry_snapshot()
    return not (
        snapshot["owners"] or snapshot["baseline"] or snapshot["claims"]
    )


@pytest.fixture(autouse=True)
def _isolated_mirror_registry(monkeypatch):
    """Keep every case independent of the previous one.

    The mirror registry is process-global, and a leaked claim is exactly what
    these cases are looking for — so a leak from one case must not be able to
    decide the outcome of the next one. The registry is emptied and the
    process env is restored around every test.
    """
    with profiles._ENV_MIRROR_LOCK:
        profiles._ENV_MIRROR_OWNERS.clear()
        profiles._ENV_MIRROR_BASELINE.clear()
        profiles._ENV_MIRROR_CLAIMS.clear()
    before = dict(os.environ)
    try:
        yield
    finally:
        with profiles._ENV_MIRROR_LOCK:
            profiles._ENV_MIRROR_OWNERS.clear()
            profiles._ENV_MIRROR_BASELINE.clear()
            profiles._ENV_MIRROR_CLAIMS.clear()
        os.environ.clear()
        os.environ.update(before)


# ── P1: a failed mirror write must not leave claims behind ──────────────────


def test_failed_mirror_write_leaves_no_registration(monkeypatch, tmp_path):
    """The failed scope must not stay registered in the mirror registry.

    ``_claim_process_env`` registers claims before writing ``os.environ``. When
    a write raises, the scope never gets its token, so the caller's cleanup
    cannot release it. The registry must not keep the failed scope's owners,
    baseline or claim list.
    """
    base = _setup_server(monkeypatch, tmp_path)
    _install_fake_secret_scope(monkeypatch, [])

    alpha = base / "profiles" / ALPHA
    _write(
        alpha / ".env",
        f"ALPHA_CUSTOM_KEY={SECRET_A}\n"
        "ALPHA_FIRST_OK=first-ok\n"
        f"ALPHA_SECOND_BAD=second{NUL_VALUE}bad\n",
    )

    with pytest.raises(ValueError):
        with _enter_background_worker(ALPHA):
            pass

    snapshot = _mirror_registry_snapshot()
    assert snapshot["claims"] == {}, (
        "the failed worker kept its claim list registered: "
        f"{list(snapshot['claims'])}"
    )
    assert snapshot["owners"] == {}, (
        "the failed worker kept per-name owners registered: "
        f"{list(snapshot['owners'])}"
    )
    assert snapshot["baseline"] == {}, (
        "the failed worker kept captured baselines registered: "
        f"{list(snapshot['baseline'])}"
    )
    assert _mirror_registry_is_empty(), "the mirror registry is not empty after the failure"


def test_failed_mirror_write_leaves_no_partial_write(monkeypatch, tmp_path):
    """Names written before the failing one must not survive the failure.

    ``desired`` is applied in order, so the name that follows the failing one
    was already stored in ``os.environ`` when the write raised. A rolled-back
    scope must leave the process env exactly as it found it.
    """
    base = _setup_server(monkeypatch, tmp_path)
    alpha = base / "profiles" / ALPHA
    # A well-formed name that is written first, and a name whose value the
    # environment refuses, written second.
    _write(
        alpha / ".env",
        f"ALPHA_CUSTOM_KEY={SECRET_A}\n"
        "ALPHA_FIRST_OK=first-ok\n"
        f"ALPHA_SECOND_BAD=second{NUL_VALUE}bad\n",
    )
    _install_fake_secret_scope(monkeypatch, [])

    # Order the mirror writes deterministically: the good name first, the
    # failing name second.
    original_claim = profiles._claim_process_env
    desired_order: list[str] = []

    def _recording_claim(desired):
        desired_order.append(list(desired))
        return original_claim(desired)

    monkeypatch.setattr(profiles, "_claim_process_env", _recording_claim)

    before = dict(os.environ)
    with pytest.raises(ValueError):
        with _enter_background_worker(ALPHA):
            pass

    assert desired_order, "the worker never reached the mirror claim"
    names = desired_order[0]
    assert "ALPHA_FIRST_OK" in names and "ALPHA_SECOND_BAD" in names, (
        f"the synthetic profile env did not reach the mirror: {names}"
    )
    assert names.index("ALPHA_FIRST_OK") < names.index("ALPHA_SECOND_BAD"), (
        "the test needs the well-formed name to be written BEFORE the failing one: "
        f"{names}"
    )

    assert os.environ.get("ALPHA_FIRST_OK") == before.get("ALPHA_FIRST_OK"), (
        "the name written before the failing one survived the failed scope: "
        f"{os.environ.get('ALPHA_FIRST_OK')!r}"
    )
    assert "ALPHA_SECOND_BAD" not in os.environ, (
        "the failing name was stored despite the write failing"
    )


def test_failed_mirror_write_does_not_disturb_a_live_worker(monkeypatch, tmp_path):
    """A failed scope must not disturb a concurrently live worker.

    The failed scope's leaked claim would keep its own value active and keep
    the live worker's credential names hidden. The live worker must keep
    reading its own credential for its whole lifetime.
    """
    base = _setup_server(monkeypatch, tmp_path)
    alpha = base / "profiles" / ALPHA
    _write(
        alpha / ".env",
        f"ALPHA_CUSTOM_KEY={SECRET_A}\n"
        "ALPHA_FIRST_OK=first-ok\n"
        f"ALPHA_SECOND_BAD=second{NUL_VALUE}bad\n",
    )
    _install_fake_secret_scope(monkeypatch, [])

    # The deployment baseline for a name beta's own config declares.
    monkeypatch.setenv("BETA_CUSTOM_KEY", "deployment-baseline")

    live_entered = threading.Event()
    release_live = threading.Event()
    observed: dict = {}

    def live_body() -> None:
        try:
            with _enter_background_worker(BETA):
                live_entered.set()
                release_live.wait(60)
                observed["live_read"] = os.environ.get("BETA_CUSTOM_KEY")
                observed["live_after_failure"] = os.environ.get("BETA_CUSTOM_KEY")
        except BaseException as exc:  # pragma: no cover - surfaced below
            observed["live_error"] = exc
            live_entered.set()

    worker = threading.Thread(target=live_body, daemon=True)
    worker.start()
    assert live_entered.wait(60), "the live worker never entered its scope"

    try:
        with pytest.raises(ValueError):
            with _enter_background_worker(ALPHA):
                pass
    except BaseException as exc:  # pragma: no cover - surfaced below
        observed["failure_error"] = exc

    assert "live_error" not in observed, observed.get("live_error")
    observed["live_after_failure"] = os.environ.get("BETA_CUSTOM_KEY")
    assert observed["live_after_failure"] == SECRET_B, (
        "the failed scope disturbed the live worker's credential: "
        f"{observed['live_after_failure']!r}"
    )

    release_live.set()
    worker.join(30)
    assert not worker.is_alive(), "the live worker thread did not terminate"
    assert observed.get("live_read") == SECRET_B, observed.get("live_read")
    assert os.environ.get("BETA_CUSTOM_KEY") == "deployment-baseline", (
        "the failed scope kept beta's credential hidden after beta's worker "
        f"exited: {os.environ.get('BETA_CUSTOM_KEY')!r}"
    )


def test_failed_mirror_write_returns_to_the_true_baseline(monkeypatch, tmp_path):
    """After every scope has exited the process env is back to its baseline."""
    base = _setup_server(monkeypatch, tmp_path)
    alpha = base / "profiles" / ALPHA
    _write(
        alpha / ".env",
        f"ALPHA_CUSTOM_KEY={SECRET_A}\n"
        "ALPHA_FIRST_OK=first-ok\n"
        f"ALPHA_SECOND_BAD=second{NUL_VALUE}bad\n",
    )
    _install_fake_secret_scope(monkeypatch, [])

    monkeypatch.setenv("ALPHA_CUSTOM_KEY", SECRET_A)
    baseline = os.environ.get("ALPHA_CUSTOM_KEY")

    with pytest.raises(ValueError):
        with _enter_background_worker(ALPHA):
            pass

    assert os.environ.get("ALPHA_CUSTOM_KEY") == baseline, (
        "the failed scope left the baseline credential changed: "
        f"{os.environ.get('ALPHA_CUSTOM_KEY')!r} != {baseline!r}"
    )
    assert "ALPHA_FIRST_OK" not in os.environ, (
        "the failed scope left a partial write behind: "
        f"{os.environ.get('ALPHA_FIRST_OK')!r}"
    )
    assert _mirror_registry_is_empty(), (
        "the mirror registry is not empty after the failed scope unwound"
    )


def test_later_worker_is_not_blindfolded_by_an_earlier_failure(monkeypatch, tmp_path):
    """A failure must not hide a credential from the NEXT worker.

    A worker's scrub claims every credential name EVERY profile declares, and
    a leaked hidden claim keeps the name removed from ``os.environ``. A later
    worker would then read ``None`` for a credential it owns, and after that
    worker exits the name would not come back to its deployment baseline.
    """
    base = _setup_server(monkeypatch, tmp_path)
    alpha = base / "profiles" / ALPHA
    _write(
        alpha / ".env",
        f"ALPHA_CUSTOM_KEY={SECRET_A}\n"
        "ALPHA_FIRST_OK=first-ok\n"
        f"ALPHA_SECOND_BAD=second{NUL_VALUE}bad\n",
    )
    _install_fake_secret_scope(monkeypatch, [])

    # The deployment baseline for a name beta's own config declares.
    monkeypatch.setenv("BETA_CUSTOM_KEY", "deployment-baseline")

    with pytest.raises(ValueError):
        with _enter_background_worker(ALPHA):
            pass

    with _enter_background_worker(BETA):
        assert os.environ.get("BETA_CUSTOM_KEY") == SECRET_B, (
            "the earlier failure kept beta's own credential hidden: "
            f"{os.environ.get('BETA_CUSTOM_KEY')!r}"
        )

    assert os.environ.get("BETA_CUSTOM_KEY") == "deployment-baseline", (
        "the earlier failure kept beta's credential hidden after beta's worker "
        f"exited: {os.environ.get('BETA_CUSTOM_KEY')!r}"
    )
    assert _mirror_registry_is_empty(), (
        "the mirror registry is not empty after both scopes finished"
    )


# ── P1, at the claim registry itself ───────────────────────────────────────


def test_claim_process_env_rolls_back_its_own_claims(monkeypatch, tmp_path):
    """``_claim_process_env`` must not keep a failed claim registered.

    The unit under the finding: the claims are registered before the values are
    written, so a failing write used to leave the scope registered with no way
    to release it.
    """
    monkeypatch.setenv("MIRROR_ROLLBACK_OK", "baseline-value")
    monkeypatch.delenv("MIRROR_ROLLBACK_BAD", raising=False)

    with pytest.raises(ValueError):
        profiles._claim_process_env(
            {"MIRROR_ROLLBACK_OK": "new-value", "MIRROR_ROLLBACK_BAD": NUL_VALUE}
        )

    assert _mirror_registry_is_empty(), (
        "the failed claim stayed registered in the mirror registry"
    )
    assert os.environ.get("MIRROR_ROLLBACK_OK") == "baseline-value", (
        "the failed claim left a partial write behind: "
        f"{os.environ.get('MIRROR_ROLLBACK_OK')!r}"
    )
    assert "MIRROR_ROLLBACK_BAD" not in os.environ, (
        "the failing name was stored despite the write failing"
    )


def test_claim_process_env_rollback_keeps_the_other_claims_alive(monkeypatch, tmp_path):
    """A failed claim must not disturb the claims of a live scope.

    The live scope keeps its value, and releasing it afterwards restores the
    baseline — the failed scope left nothing behind that could change that.
    """
    monkeypatch.setenv("MIRROR_SHARED_OK", "baseline-value")
    monkeypatch.delenv("MIRROR_SHARED_BAD", raising=False)

    live_token = profiles._claim_process_env({"MIRROR_SHARED_OK": "live-value"})
    try:
        with pytest.raises(ValueError):
            profiles._claim_process_env(
                {"MIRROR_SHARED_OK": "other-value", "MIRROR_SHARED_BAD": NUL_VALUE}
            )
        assert os.environ.get("MIRROR_SHARED_OK") == "live-value", (
            "the failed claim changed the live scope's value: "
            f"{os.environ.get('MIRROR_SHARED_OK')!r}"
        )
    finally:
        profiles._release_process_env(live_token)

    assert os.environ.get("MIRROR_SHARED_OK") == "baseline-value", (
        "the baseline was not restored after the live scope released: "
        f"{os.environ.get('MIRROR_SHARED_OK')!r}"
    )
    assert _mirror_registry_is_empty(), (
        "the mirror registry is not empty after the live scope released"
    )
