"""Regression coverage for issue #8984: server log redaction.

The WebUI server installs no logging handler. Agent library WARNINGs raised
inside the server process therefore fall through to Python's last-resort
stderr handler, *without* the Agent's RedactingFormatter. A provider
base_url that carries credentials (a URL whose userinfo part is
``alice:<password>@proxy.example.invalid``) is then written in
clear text to the server's stdout/stderr log.

Mechanism
---------
The Agent redacts at the formatter: ``agent.redact.RedactingFormatter`` masks
the userinfo part of a URL (``user:<password>@`` -> ``user:***@``).
Its own entry points (gateway/run.py, gateway/run_turn.py,
acp_adapter/entry.py) install it; WebUI embeds the Agent as a library and
installed nothing, so the root logger had no handlers after importing
``api.streaming``/``api.routes`` and WARNING+ records went to
``logging.lastResort`` unredacted.

These tests exercise ``api/log_redaction.py`` (wired into ``server.main()``):

* with the Agent importable, the installed root handler uses
  ``agent.redact.RedactingFormatter`` and a userinfo URL logged through the
  ``agent.model_metadata`` logger comes out masked;
* without the Agent, a plain ``logging.Formatter`` is used and nothing
  raises;
* the level and the non-secret text are unchanged (same WARNING level, same
  logger name, same message apart from the mask);
* installation is idempotent — the root logger keeps exactly ONE WebUI
  handler regardless of import/startup ordering.

No live server socket is required — the installer is exercised directly with
an injected stream. Fake credentials only (``alice:<password>@
proxy.example.invalid``); nothing here touches a real secret.
"""
import io
import logging
import os
import re
import subprocess
import sys
import textwrap
import types

import pytest

import api.log_redaction as lr

# Shape mandated by the issue: userinfo ``user:<password>@host`` on a
# reserved-for-docs host. Never a real credential.
FAKE_URL = "https://alice:FAKE-PASSWORD-123@proxy.example.invalid/v1"


@pytest.fixture()
def clean_root_logging():
    """Isolate the process-wide root-logger state for one test.

    Installing a root handler is a process-wide side effect (and pytest's own
    logging plugin also touches the root logger), so snapshot the handler list
    and the module's install flag and restore both afterwards.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_installed = lr._INSTALLED
    root.handlers.clear()
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        lr._INSTALLED = saved_installed


class _FakeRedactingFormatter(logging.Formatter):
    """Test double for ``agent.redact.RedactingFormatter``.

    The real formatter masks the password half of a URL's userinfo
    (``scheme://user:<password>@host`` -> ``scheme://user:***@host``,
    keeping scheme, username and host visible). This double reproduces exactly
    that behaviour so the masking assertion is meaningful without importing
    the Agent.
    """

    _USERINFO_RE = re.compile(r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+):(?P<pw>[^/@\s]+)@")

    def format(self, record: logging.LogRecord) -> str:
        return self._USERINFO_RE.sub(
            lambda m: f"{m.group('prefix')}:***@", super().format(record)
        )


@pytest.fixture()
def fake_agent_redact(monkeypatch):
    """Install a stub ``agent.redact`` exposing RedactingFormatter."""
    agent_pkg = types.ModuleType("agent")
    redact_mod = types.ModuleType("agent.redact")
    redact_mod.RedactingFormatter = _FakeRedactingFormatter
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.redact", redact_mod)
    return redact_mod


def _webui_handler(root):
    """Return the single handler this module installed on ``root``.

    pytest's own logging plugin also attaches capture handlers to the root
    logger during a test, so count only the tagged WebUI handler.
    """
    marked = [h for h in root.handlers if getattr(h, "_hermes_webui_log_redaction", False)]
    assert len(marked) == 1
    return marked[0]


@pytest.fixture()
def no_agent(monkeypatch):
    """Make ``from agent.redact import ...`` raise ImportError for this test."""
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.redact", None)
    # Actually drop the entries so the import machinery (not a None shortcut)
    # fails, exactly like a machine without the Agent installed.
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.redact", raising=False)


# ── 1. with the Agent: userinfo URL is masked ───────────────────────────────
def test_userinfo_url_password_is_masked(fake_agent_redact, clean_root_logging):
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    logging.getLogger("agent.model_metadata").warning(
        "Could not determine context length for model %r (base_url=%s)",
        "m",
        FAKE_URL,
    )
    for handler in clean_root_logging.handlers:
        handler.flush()
    out = buf.getvalue()

    # The RED failure mode: the password lands in the log in clear.
    assert "FAKE-PASSWORD-123" not in out
    # The Agent's masking shape: scheme, username and host stay visible.
    assert "alice:***@" in out
    assert "proxy.example.invalid/v1" in out
    assert "Could not determine context length for model 'm'" in out


# ── 2. without the Agent: plain formatter, nothing raises ───────────────────
def test_no_agent_uses_plain_formatter_and_does_not_raise(no_agent, clean_root_logging):
    buf = io.StringIO()
    # Must not raise at install time (no agent.redact to import).
    assert lr.install_server_log_redaction(stream=buf) is True

    handler = _webui_handler(clean_root_logging)
    assert isinstance(handler.formatter, logging.Formatter)
    assert not isinstance(handler.formatter, _FakeRedactingFormatter)

    # WARNING+ still reaches the stderr channel — the fix masks, it must not
    # make log lines disappear.
    logging.getLogger("agent.model_metadata").warning(
        "Could not determine context length for model %r (base_url=%s)",
        "m",
        FAKE_URL,
    )
    handler.flush()
    assert "Could not determine context length for model 'm'" in buf.getvalue()


# ── 3. level and non-secret text unchanged ──────────────────────────────────
def test_level_and_non_secret_text_unchanged(fake_agent_redact, clean_root_logging):
    buf = io.StringIO()
    lr.install_server_log_redaction(stream=buf)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    capture = _Capture()
    model_logger = logging.getLogger("agent.model_metadata")
    model_logger.addHandler(capture)
    try:
        model_logger.warning(
            "Could not determine context length for model %r (base_url=%s)",
            "m",
            FAKE_URL,
        )
    finally:
        model_logger.removeHandler(capture)

    assert len(records) == 1
    record = records[0]
    assert record.name == "agent.model_metadata"
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    # The record itself keeps the original text; only formatted output is masked.
    assert "Could not determine context length for model 'm'" in record.getMessage()

    # The handler stays at WARNING so DEBUG/INFO records still do not print
    # (lastResort's level), i.e. the visible log changes by masking only.
    handler = _webui_handler(clean_root_logging)
    assert handler.level == logging.WARNING


# ── 4. regression control: exactly ONE handler, install is idempotent ───────
def test_install_is_idempotent_single_handler(fake_agent_redact, clean_root_logging):
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True
    assert lr.install_server_log_redaction(stream=buf) is False
    handler = _webui_handler(clean_root_logging)
    assert isinstance(handler.formatter, _FakeRedactingFormatter)


def test_clean_process_installs_exactly_one_root_handler():
    """A fresh interpreter (no pytest capture handlers) keeps ONE handler.

    Same subprocess pattern as the #4633 crash-visibility tests. Also an
    end-to-end check that a repeated install in a clean process both stays
    single-handler and masks the userinfo password.
    """
    from tests.conftest import REPO_ROOT

    script = textwrap.dedent(
        """
        import io, logging, re, sys, types
        sys.path.insert(0, {repo!r})

        class RF(logging.Formatter):
            _RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\\-]*://[^/@\\s]+):[^/@\\s]+@")
            def format(self, record):
                return self._RE.sub(lambda m: m.group(1) + ":***@", super().format(record))

        pkg = types.ModuleType("agent")
        mod = types.ModuleType("agent.redact")
        mod.RedactingFormatter = RF
        sys.modules["agent"] = pkg
        sys.modules["agent.redact"] = mod

        import api.log_redaction as lr
        buf = io.StringIO()
        first = lr.install_server_log_redaction(stream=buf)
        second = lr.install_server_log_redaction(stream=buf)
        logging.getLogger("agent.model_metadata").warning(
            "Could not determine context length for model %r (base_url=%s)",
            "m", {url!r},
        )
        for h in logging.getLogger().handlers:
            h.flush()
        print("FIRST", first)
        print("SECOND", second)
        print("HANDLERS", len(logging.getLogger().handlers))
        print("OUT", buf.getvalue().strip())
        """
    ).format(repo=os.fspath(REPO_ROOT), url=FAKE_URL)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.fspath(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "FIRST True" in completed.stdout
    assert "SECOND False" in completed.stdout
    assert "HANDLERS 1" in completed.stdout
    out_line = next(line for line in completed.stdout.splitlines() if line.startswith("OUT "))
    assert "FAKE-PASSWORD-123" not in out_line
    assert "alice:***@proxy.example.invalid/v1" in out_line


# ── 5. server.py wiring (same pattern as the #4633 crash-visibility tests) ──
def test_server_wires_install_server_log_redaction():
    import server

    assert hasattr(server, "install_server_log_redaction")
    assert server.install_server_log_redaction is lr.install_server_log_redaction

    from tests.conftest import SERVER_SCRIPT

    src = SERVER_SCRIPT.read_text(encoding="utf-8")
    assert "from api.log_redaction import install_server_log_redaction" in src
    assert "install_server_log_redaction()" in src
