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

* with the Agent importable, a userinfo URL logged through the
  ``agent.model_metadata`` logger comes out masked;
* without the Agent, nothing raises, the log line is still emitted, and
  the userinfo credential is masked by the WebUI's own boundary mask;
* the level and the non-secret text are unchanged (same WARNING level, same
  logger name, same message apart from the mask);
* installation is idempotent — the root logger keeps exactly ONE WebUI
  handler regardless of import/startup ordering.

The bootstrap transition (round 2)
----------------------------------
The Agent can become importable AFTER the WebUI installed its handler: the
bootstrap transition in ``server.main()`` (``auto_install_agent_deps()``)
installs the Agent's missing dependencies later in the process's life. A
formatter resolved at install time freezes the plain fallback, and the stale
handler then writes credentials in clear for the rest of the process's life —
a privacy regression visible ONLY on that transition, while the ordinary
startup path (Agent importable from the start) masks correctly. The handler
therefore resolves the Agent's formatter when a record is FORMATTED.

The transition is covered twice: with a stub Agent (CI, no hermes-agent
needed) and with the REAL Agent when one is installed — the maintainer's
requirement that "formatter doubles alone do not establish the real runtime
contract".

The boundary guarantee (round 3)
--------------------------------
The Agent's policy is the primary mask, but it does not cover the issue's
shape across the whole supported Agent range: ``RedactingFormatter.format``
only started calling ``redact_log_urls`` (the strict URL pass that masks the
whole userinfo pair) in Agent v0.23.2. Older builds mask the password alone
(``redact_sensitive_text`` passes ``user:pass@`` through unless the caller
opts into ``redact_url_credentials``), v0.22.0 masks neither, and a host
without the Agent has no policy at all. In those states the handler would
write ``alice:<password>@proxy.example.invalid`` to the server log — while
frozen master, whose records go to the Agent's own file handlers, stays
clean on the stderr stream.

So the handler enforces the shape at its own boundary: whatever text the
formatter chain produces, the userinfo pair is masked before the line
reaches the stream. The mask is idempotent over the Agent's output, and an
explicit Agent opt-out (``security.redact_secrets: false``) is honoured.
The old-Agent states are modelled with formatter doubles; the no-Agent
state runs in CI itself, so that regression can never skip.

No live server socket is required — the installer is exercised directly with
an injected stream. Fake credentials only (a reserved-for-docs host with a
synthetic userinfo password); nothing here touches a real secret.
"""
import importlib.abc
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
# The synthetic password, kept as its own name so the assertions read as
# "the password is absent" rather than "some substring is absent".
FAKE_PASSWORD = "FAKE-PASSWORD-123"
# The empty-user-name shape: provider setup accepts ``https://:password@host``
# and saves it unchanged, so the mask must not require a user name.
EMPTY_USER_URL = "https://:EMPTY-USER-PASSWORD@proxy.example.invalid/v1"
EMPTY_USER_PASSWORD = "EMPTY-USER-PASSWORD"


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
    # The module's install flag must follow the handler list: this module
    # treats "already installed" as "the tagged handler is on the root logger".
    # Clearing the handlers without clearing the flag makes the next install
    # in this process return False and install nothing — a failure that only
    # shows up when another test file installed the handler first, i.e. in a
    # CI shard rather than in a single-file run.
    lr._INSTALLED = False
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        lr._INSTALLED = saved_installed


class _FakeRedactingFormatter(logging.Formatter):
    """Test double for ``agent.redact.RedactingFormatter``.

    The real formatter masks the WHOLE userinfo pair of a URL: a URL whose
    userinfo is a user name and a credential renders with both parts replaced
    by asterisks, while the scheme and the host stay readable. This double
    reproduces that shape so the masking assertion is meaningful without
    importing the Agent.
    """

    _USERINFO_RE = re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@"
    )

    def format(self, record: logging.LogRecord) -> str:
        return self._USERINFO_RE.sub(
            lambda m: f"{m.group('scheme')}***:***@", super().format(record)
        )


@pytest.fixture()
def fake_agent_redact(monkeypatch):
    """Install a stub ``agent.redact`` exposing RedactingFormatter."""
    return _install_agent_stub(monkeypatch, _FakeRedactingFormatter)


def _install_agent_stub(monkeypatch, formatter_cls, *, redaction_enabled=True):
    """Install a stub ``agent.redact`` exposing ``formatter_cls``.

    ``redaction_enabled`` models the Agent's ``security.redact_secrets``
    policy: when False the stub also exposes ``_redact_enabled`` returning
    False (the operator opted out of redaction), which is the switch the
    boundary consults before standing down.
    """
    agent_pkg = types.ModuleType("agent")
    redact_mod = types.ModuleType("agent.redact")
    redact_mod.RedactingFormatter = formatter_cls
    if not redaction_enabled:
        redact_mod._redact_enabled = lambda: False
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.redact", redact_mod)
    return redact_mod


class _PassthroughFormatter(logging.Formatter):
    """Test double for an Agent build that masks nothing on the log path.

    Models Agent v0.22.0: ``RedactingFormatter`` exists, imports,
    constructs and formats fine — a qualified dependency column — but the
    userinfo credential reaches the output in clear.
    """

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)


class _PasswordOnlyUserinfoFormatter(logging.Formatter):
    """Test double for an Agent build that masks the password alone.

    Models Agent v0.23.0/v0.23.1: ``RedactingFormatter.format`` runs
    ``redact_sensitive_text`` without ``redact_url_credentials=True``, so
    the userinfo password is masked but the user name survives —
    ``alice:***@`` instead of the promised whole-pair mask.
    """

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(FAKE_PASSWORD, "***")


class _RaisingFormatter(logging.Formatter):
    """Test double for an Agent build whose formatter raises."""

    def format(self, record: logging.LogRecord) -> str:
        raise RuntimeError("formatter boom")


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
    """Make ``from agent.redact import ...`` raise ImportError for this test.

    The entries stay set to ``None`` (the documented "this package is not
    available" sentinel) instead of being deleted: deleting them lets Python
    import an INSTALLED Agent normally, and the test would then exercise the
    Agent instead of the fallback it claims to cover.
    """
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.redact", None)


def _emit_userinfo_warning():
    """Log the credential-bearing warning the way the Agent does."""
    logging.getLogger("agent.model_metadata").warning(
        "Could not determine context length for model %r (base_url=%s)",
        "m",
        FAKE_URL,
    )


# ── 1. with the Agent: userinfo URL is masked ───────────────────────────────
def test_userinfo_url_password_is_masked(fake_agent_redact, clean_root_logging):
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    for handler in clean_root_logging.handlers:
        handler.flush()
    out = buf.getvalue()

    # The RED failure mode: the password lands in the log in clear.
    assert FAKE_PASSWORD not in out
    # The scheme and the host stay readable; the userinfo is masked.
    assert "proxy.example.invalid/v1" in out
    assert "Could not determine context length for model 'm'" in out


# ── 2. without the Agent: no Agent formatter, nothing raises ────────────────
def test_no_agent_uses_plain_formatter_and_does_not_raise(no_agent, clean_root_logging):
    buf = io.StringIO()
    # Must not raise at install time (no agent.redact to import).
    assert lr.install_server_log_redaction(stream=buf) is True

    handler = _webui_handler(clean_root_logging)
    # The Agent's formatter is resolved when a record is FORMATTED, so the
    # assertion is on the EFFECT (no Agent formatter is available) rather
    # than on the handler's formatter class — the transition tests below
    # cover the class.
    assert lr._resolve_agent_formatter_class() is None
    assert handler.formatter._agent_formatter is None

    # WARNING+ still reaches the stderr channel — the fix masks, it must not
    # make log lines disappear.
    _emit_userinfo_warning()
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
        _emit_userinfo_warning()
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
    assert len([h for h in clean_root_logging.handlers
                if getattr(h, "_hermes_webui_log_redaction", False)]) == 1


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
    assert FAKE_PASSWORD not in out_line
    assert "proxy.example.invalid/v1" in out_line


# ── 5. server.py wiring (same pattern as the #4633 crash-visibility tests) ──
def test_server_wires_install_server_log_redaction():
    import server

    assert hasattr(server, "install_server_log_redaction")
    assert server.install_server_log_redaction is lr.install_server_log_redaction

    from tests.conftest import SERVER_SCRIPT

    src = SERVER_SCRIPT.read_text(encoding="utf-8")
    assert "from api.log_redaction import install_server_log_redaction" in src
    assert "install_server_log_redaction()" in src
    # The bootstrap transition must refresh the formatter: the Agent can
    # become importable only AFTER the handler was installed.
    assert "refresh_server_log_redaction()" in src


# ── 6. the bootstrap transition (round 2) ───────────────────────────────────
class _AvailabilitySeam(importlib.abc.MetaPathFinder):
    """Refuse ``agent.redact`` while the owner says the Agent is unavailable.

    This models the bootstrap transition: the Agent is not importable when
    the server installs its handler, and becomes importable afterwards.
    """

    def __init__(self, owner):
        self._owner = owner

    def find_spec(self, fullname, path=None, target=None):
        if self._owner.blocked and (
            fullname == "agent.redact" or fullname.startswith("agent.redact.")
        ):
            raise ModuleNotFoundError(f"availability seam: {fullname} unavailable")
        return None


class _AgentAvailability:
    """Control WHEN the Agent's formatter becomes importable.

    Models the bootstrap transition: the Agent is NOT importable while the
    server installs its handler, and becomes importable afterwards. The
    availability is controlled in BOTH places Python looks — ``sys.modules``
    (where the "not available" sentinel is ``None``) and the import path
    (a meta-path finder that refuses the module) — so the transition cannot be
    short-circuited by a stale submodule entry.
    """

    def __init__(self, monkeypatch):
        self.blocked = True
        self._seam = _AvailabilitySeam(self)
        self._stub_pkg = types.ModuleType("agent")
        self._stub_redact = types.ModuleType("agent.redact")
        self._stub_redact.RedactingFormatter = _FakeRedactingFormatter
        # Save the real entries BEFORE this fixture touches them. The Agent's
        # modules are process-global, and monkeypatch does NOT see the direct
        # ``sys.modules[...]`` writes below — so without this, the test would
        # leave ``sys.modules["agent.redact"] = None`` behind and every later
        # import from the Agent would fail (the redaction tests that run
        # after this file would break).
        self._saved = {}
        for name in ("agent", "agent.redact"):
            self._saved[name] = (name in sys.modules, sys.modules.get(name))
        monkeypatch.setattr(sys, "meta_path", [self._seam] + list(sys.meta_path))
        self._apply()

    def restore(self):
        """Put the Agent's module entries back exactly as they were."""
        for name, (was_present, value) in self._saved.items():
            if was_present:
                sys.modules[name] = value
            else:
                sys.modules.pop(name, None)

    def _apply(self):
        if self.blocked:
            sys.modules["agent"] = None
            sys.modules["agent.redact"] = None
        else:
            sys.modules["agent"] = self._stub_pkg
            sys.modules["agent.redact"] = self._stub_redact

    def release(self):
        """The transition: the Agent becomes importable."""
        self.blocked = False
        self._apply()


@pytest.fixture()
def agent_availability(monkeypatch):
    """The Agent is unavailable until the test releases the transition."""
    availability = _AgentAvailability(monkeypatch)
    try:
        yield availability
    finally:
        availability.restore()


def test_bootstrap_transition_switches_to_the_agent_formatter(
    agent_availability, clean_root_logging
):
    """The Agent becomes importable AFTER the handler was installed.

    A formatter frozen at install time keeps the plain fallback for the rest
    of the process's life and writes the credential in clear. The handler must
    pick the Agent's formatter up on the transition instead.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    # Before the transition: no Agent, so nothing to mask with — but the line
    # must still be emitted.
    assert lr._resolve_agent_formatter_class() is None
    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    assert "Could not determine context length for model 'm'" in buf.getvalue()

    # ── the transition: the Agent becomes importable ───────────────────────
    agent_availability.release()
    assert lr._resolve_agent_formatter_class() is _FakeRedactingFormatter

    buf.truncate(0)
    buf.seek(0)
    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    # The credential must be masked NOW, without reinstalling the handler.
    assert FAKE_PASSWORD not in out
    assert "proxy.example.invalid/v1" in out


def test_bootstrap_transition_never_leaks_the_password_in_clear(
    agent_availability, clean_root_logging
):
    """The transition oracle: the raw password must never reach the stream.

    The full scan is the oracle: no line of the output may contain the
    password, in any form.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    agent_availability.release()
    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()

    out = buf.getvalue()
    assert FAKE_PASSWORD not in out
    assert "***" in out, "the userinfo must be masked, not merely absent"


def test_refresh_server_log_redaction_picks_up_the_agent(
    agent_availability, clean_root_logging
):
    """``refresh_server_log_redaction()`` performs the switch immediately.

    ``server.main()`` calls it right after the bootstrap transition, so the
    Agent's formatter is in use without waiting for the next record.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    agent_availability.release()
    assert lr.refresh_server_log_redaction() == 1

    handler = _webui_handler(clean_root_logging)
    assert isinstance(handler.formatter, lr._DeferredRedactingFormatter)
    assert handler.formatter._agent_formatter is not None


def test_ordinary_path_still_masks_with_the_agent_available(
    fake_agent_redact, clean_root_logging
):
    """The ordinary startup path (Agent available from the start) still masks.

    This is the control arm of the transition oracle: the fix must not change
    the behaviour that already worked.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_PASSWORD not in out
    assert "proxy.example.invalid/v1" in out


def test_no_agent_fallback_masks_userinfo_and_keeps_the_line(
    no_agent, clean_root_logging
):
    """Without the Agent the userinfo is masked by the WebUI's own mask.

    The credential-output guarantee is enforced at the owning boundary
    across supported dependencies, and "no Agent" is a supported state —
    CI itself runs without one — so this regression never skips. The
    output-delivery contract holds (the line is present) and so does the
    diagnostic behaviour (scheme and host stay readable); only the
    credential pair is masked, in the same shape the Agent produces.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    # delivery: the line is present — masking must not delete diagnostics
    assert "Could not determine context length for model 'm'" in out
    # diagnostics: the scheme and the host stay readable
    assert "https://" in out
    assert "proxy.example.invalid/v1" in out
    # guarantee: the whole pair is masked, the password absent
    assert FAKE_PASSWORD not in out
    assert "alice" not in out
    assert "***:***@proxy.example.invalid" in out


def test_empty_username_userinfo_is_masked(no_agent, clean_root_logging):
    """A password with an EMPTY user name is masked too.

    ``https://:secret@host`` is a shape provider setup accepts and saves
    unchanged. The mask used to require a non-empty user name on its first
    userinfo group, so this URL passed through untouched and the password
    reached the server log in clear. Only the password is required now.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    logging.getLogger("agent.model_metadata").warning(
        "Could not determine context length for model %r (base_url=%s)",
        "m",
        EMPTY_USER_URL,
    )
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    # delivery: the line is present — masking must not delete diagnostics
    assert "Could not determine context length for model 'm'" in out
    # diagnostics: the scheme and the host stay readable
    assert "proxy.example.invalid/v1" in out
    # guarantee: the password is absent even without a user name
    assert EMPTY_USER_PASSWORD not in out
    assert "***:***@proxy.example.invalid" in out


# ── 7. the boundary guarantee across supported dependencies (round 3) ───
def test_agent_formatter_without_userinfo_masking_is_completed_by_the_boundary(
    monkeypatch, clean_root_logging
):
    """An Agent whose formatter masks the password alone (v0.23.0/v0.23.1).

    Those builds emit ``alice:***@`` — the password masked, the user name
    surviving. The boundary completes the promised whole-pair shape.
    """
    _install_agent_stub(monkeypatch, _PasswordOnlyUserinfoFormatter)
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_PASSWORD not in out
    assert "alice" not in out
    assert "***:***@proxy.example.invalid" in out
    assert "Could not determine context length for model 'm'" in out


def test_agent_formatter_that_redacts_nothing_is_masked_by_the_boundary(
    monkeypatch, clean_root_logging
):
    """An Agent whose formatter masks nothing on the log path (v0.22.0).

    The formatter imports, constructs and formats fine — a qualified
    dependency column — but leaves the userinfo in clear. Without the
    boundary mask this head writes the credential to the server log while
    frozen master, whose records go to the Agent's own file handlers,
    stays clean on the stderr stream: the matched output-boundary
    regression the Gate reproduced.
    """
    _install_agent_stub(monkeypatch, _PassthroughFormatter)
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_PASSWORD not in out
    assert "alice" not in out
    assert "***:***@proxy.example.invalid" in out
    assert "Could not determine context length for model 'm'" in out


def test_agent_formatter_raising_still_masks_userinfo(
    monkeypatch, clean_root_logging
):
    """A formatter that raises must not degrade into a clear-text line."""
    _install_agent_stub(monkeypatch, _RaisingFormatter)
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert "Could not determine context length for model 'm'" in out
    assert FAKE_PASSWORD not in out
    assert "***:***@proxy.example.invalid" in out


def test_agent_opt_out_is_honoured(monkeypatch, clean_root_logging):
    """An explicit Agent opt-out passes raw credentials through.

    ``security.redact_secrets: false`` is the operator's policy; the
    WebUI honours it instead of overriding it (the maintainer verified
    this behaviour on the previous head). Without the Agent there is no
    policy to honour, so the built-in mask applies (covered above).
    """
    _install_agent_stub(monkeypatch, _PassthroughFormatter, redaction_enabled=False)
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_URL in out  # raw, by the operator's explicit policy
    assert "proxy.example.invalid/v1" in out


# ── 8. the REAL Agent (the maintainer's requirement) ───────────────────────
def _real_agent_available():
    """True when a real hermes-agent with a formatter is importable."""
    try:
        import agent.redact as redact
    except Exception:
        return False
    return isinstance(getattr(redact, "RedactingFormatter", None), type)


@pytest.mark.skipif(
    not _real_agent_available(),
    reason="hermes-agent with agent.redact.RedactingFormatter not installed",
)
def test_real_agent_masks_through_the_server_handler(clean_root_logging):
    """The real Agent's formatter masks the credential on the WebUI's stream.

    A formatter double cannot establish this: the real ``RedactingFormatter``
    is the mechanism the Agent's own entry points install, and the WebUI must
    produce the SAME masked shape as the Agent.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_PASSWORD not in out
    assert "proxy.example.invalid/v1" in out
    # The real formatter masks the WHOLE userinfo pair.
    assert "***:***@proxy.example.invalid" in out


@pytest.mark.skipif(
    not _real_agent_available(),
    reason="hermes-agent with agent.redact.RedactingFormatter not installed",
)
def test_real_agent_bootstrap_transition_never_leaks(clean_root_logging):
    """The transition, with the REAL Agent: the credential never lands clear.

    The Agent is hidden while the handler is installed, then released — the
    bootstrap transition. The password must not appear in clear at any point.
    """
    saved_agent = sys.modules.get("agent")
    saved_redact = sys.modules.get("agent.redact")
    try:
        # Hide the already-imported Agent so the handler starts without it.
        sys.modules["agent"] = None
        sys.modules["agent.redact"] = None

        buf = io.StringIO()
        assert lr.install_server_log_redaction(stream=buf) is True
        assert lr._resolve_agent_formatter_class() is None

        # ── the transition releases the Agent ─────────────────────────────
        if saved_agent is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = saved_agent
        if saved_redact is None:
            sys.modules.pop("agent.redact", None)
        else:
            sys.modules["agent.redact"] = saved_redact

        assert lr._resolve_agent_formatter_class() is not None

        buf.truncate(0)
        buf.seek(0)
        _emit_userinfo_warning()
        _webui_handler(clean_root_logging).flush()
        out = buf.getvalue()

        assert FAKE_PASSWORD not in out
        assert "proxy.example.invalid/v1" in out
    finally:
        if saved_agent is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = saved_agent
        if saved_redact is None:
            sys.modules.pop("agent.redact", None)
        else:
            sys.modules["agent.redact"] = saved_redact


@pytest.mark.skipif(
    not _real_agent_available(),
    reason="hermes-agent with agent.redact.RedactingFormatter not installed",
)
def test_real_agent_output_is_unchanged_by_the_boundary_mask(clean_root_logging):
    """The boundary mask is a no-op over the real Agent's masked output.

    Proves the fix does not rewrite a current Agent's line: the real
    formatter masks the whole pair, and re-masking it must yield the same
    bytes — exactly one masked pair, no artifacts.
    """
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_PASSWORD not in out
    assert "***:***@proxy.example.invalid" in out
    assert out.count("***:***@") == 1


@pytest.mark.skipif(
    not _real_agent_available(),
    reason="hermes-agent with agent.redact.RedactingFormatter not installed",
)
def test_real_agent_opt_out_is_honoured(clean_root_logging, monkeypatch):
    """The real Agent's opt-out switch is consulted, not overridden."""
    import agent.redact as real_redact

    monkeypatch.setattr(real_redact, "_redact_enabled", lambda: False)
    buf = io.StringIO()
    assert lr.install_server_log_redaction(stream=buf) is True

    _emit_userinfo_warning()
    _webui_handler(clean_root_logging).flush()
    out = buf.getvalue()

    assert FAKE_URL in out  # raw, by the operator's explicit policy
    assert "proxy.example.invalid/v1" in out
