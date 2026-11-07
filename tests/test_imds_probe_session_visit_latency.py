"""Regression coverage: the model-catalog rebuild must not probe EC2 IMDS.

Symptom
-------
Opening an existing chat is visibly slow. `/api/models?freshness=session_visit`
forces a live model-catalog rebuild; provider auth enumeration reaches
`agent.bedrock_adapter.has_aws_credentials()`, which falls back to botocore's
credential chain. On a non-EC2 host botocore still tries the Instance Metadata
Service at 169.254.169.254 — a link-local black hole here — burning its full
connect timeout on every cold rebuild.

Measured on this non-EC2 macOS host before the fix, one cold session-visit
rebuild:

    retrieve_iam_role_credentials calls: 2
      call[0] = 1.007s
      call[1] = 0.003s
    raw IMDS HTTP attempts: 4
      PUT  http://169.254.169.254/latest/api/token                      -> 1.005s
      GET  http://169.254.169.254/latest/meta-data/iam/security-credentials/ -> 0.002s
      PUT  http://169.254.169.254/latest/api/token                      -> 0.002s
      GET  http://169.254.169.254/latest/meta-data/iam/security-credentials/ -> 0.001s

The observable behaviour asserted here is the one that costs the wall time:
**zero IMDS HTTP requests leave the process** during a rebuild on a host where
the metadata endpoint is unreachable.

Note on the suite-wide guard: `tests/conftest.py` sets
`AWS_EC2_METADATA_DISABLED=true` for the whole pytest session, which would mask
this bug entirely. Every test here removes that variable for its own scope so
the product code — not the test harness — is what gets measured.
"""

from __future__ import annotations

import os
import socket
import threading

import pytest


@pytest.fixture
def no_suite_imds_guard(monkeypatch):
    """Drop conftest's session-wide AWS_EC2_METADATA_DISABLED for this test.

    Without this the assertions pass even with the fix reverted, because the
    harness — not the product — is what suppressed the probe.
    """
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    import api.aws_imds as aws_imds

    aws_imds.reset_reachability_cache()
    yield
    aws_imds.reset_reachability_cache()


@pytest.fixture
def imds_unreachable(monkeypatch):
    """Simulate this host's real condition: IMDS never answers."""
    import api.aws_imds as aws_imds

    def _refuse(address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if host == "169.254.169.254":
            raise socket.timeout("timed out")
        raise AssertionError(f"unexpected connect to {address!r}")

    monkeypatch.setattr(aws_imds.socket, "create_connection", _refuse)
    aws_imds.reset_reachability_cache()
    return aws_imds


@pytest.fixture
def imds_http_recorder(monkeypatch):
    """Record every HTTP request botocore aims at the IMDS endpoint."""
    import botocore.httpsession as bh

    seen: list[str] = []
    original = bh.URLLib3Session.send

    def recording_send(self, request, *a, **kw):
        url = str(getattr(request, "url", ""))
        if "169.254.169.254" in url:
            seen.append(f"{getattr(request, 'method', '?')} {url}")
            # Never let a real link-local request run inside the suite; the
            # point of the test is whether it was *attempted*.
            raise ConnectionError("IMDS blocked by test recorder")
        return original(self, request, *a, **kw)

    monkeypatch.setattr(bh.URLLib3Session, "send", recording_send)
    return seen


def _resolve_aws_credentials_like_bedrock_adapter():
    """Reproduce the exact chain the WebUI hits during catalog rebuild.

    api/config.py `_build_available_models_uncached`
      -> hermes_cli.models.list_available_providers()
      -> hermes_cli.auth.get_auth_status("bedrock")
      -> agent.bedrock_adapter.has_aws_credentials()
      -> botocore.session.get_session().get_credentials()
    """
    import botocore.session

    return botocore.session.get_session().get_credentials()


def test_credential_resolution_under_scope_issues_no_imds_requests(
    no_suite_imds_guard, imds_unreachable, imds_http_recorder
):
    """The shared scope must stop IMDS traffic on an unreachable-IMDS host.

    FAILS BEFORE THE FIX: without `suppress_ec2_imds_probe()`, botocore issues
    PUT /latest/api/token against 169.254.169.254.
    """
    aws_imds = imds_unreachable
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_WEB_IDENTITY_TOKEN_FILE"):
        os.environ.pop(var, None)

    with aws_imds.suppress_ec2_imds_probe("test"):
        _resolve_aws_credentials_like_bedrock_adapter()

    assert imds_http_recorder == [], (
        "REGRESSION: the model-catalog rebuild probed EC2 IMDS on a host that "
        f"cannot reach it. Requests attempted: {imds_http_recorder}"
    )


def test_scope_restores_env_on_success_and_on_exception(
    no_suite_imds_guard, imds_unreachable
):
    """State owner: the scope must release the env var on EVERY exit path."""
    aws_imds = imds_unreachable
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None

    with aws_imds.suppress_ec2_imds_probe("test") as active:
        assert active is True
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None, "leaked on success"

    with pytest.raises(RuntimeError):
        with aws_imds.suppress_ec2_imds_probe("test"):
            assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
            raise RuntimeError("boom")
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None, "leaked on exception"


def test_nested_scopes_restore_only_once(no_suite_imds_guard, imds_unreachable):
    """Concurrent/nested scopes must not restore out from under each other."""
    aws_imds = imds_unreachable
    with aws_imds.suppress_ec2_imds_probe("outer"):
        with aws_imds.suppress_ec2_imds_probe("inner"):
            assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true", (
            "inner scope exit wrongly re-enabled IMDS while outer scope was active"
        )
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None


def test_overlapping_scopes_on_two_threads_hold_suppression(
    no_suite_imds_guard, imds_unreachable
):
    """A late entrant must JOIN the active scope, not read our own env value.

    FAILS BEFORE THE OWNERSHIP FIX. `test_nested_scopes_restore_only_once` is
    sequential — the inner block always exits first — so it cannot expose the
    overlap. Here thread B enters while A holds the scope and is still inside
    when A exits: if B mistook the module-installed `AWS_EC2_METADATA_DISABLED`
    for an operator decision it never joined the refcount, and A's exit strips
    suppression out from under B, which then issues the IMDS request its scope
    promised to suppress.
    """
    aws_imds = imds_unreachable

    a_installed = threading.Event()
    b_entered = threading.Event()
    a_exited = threading.Event()
    observed: dict[str, object] = {}
    timeout = 10

    def thread_a():
        with aws_imds.suppress_ec2_imds_probe("A: models catalog rebuild") as active:
            observed["a_active"] = active
            a_installed.set()
            assert b_entered.wait(timeout), "thread B never entered its scope"
        a_exited.set()

    def thread_b():
        assert a_installed.wait(timeout), "thread A never installed suppression"
        with aws_imds.suppress_ec2_imds_probe("B: providers auth status") as active:
            observed["b_active"] = active
            b_entered.set()
            assert a_exited.wait(timeout), "thread A never exited"
            observed["b_env_after_a_exit"] = os.environ.get("AWS_EC2_METADATA_DISABLED")
            observed["b_depth_after_a_exit"] = aws_imds._scope_depth

    threads = [threading.Thread(target=thread_a), threading.Thread(target=thread_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout * 2)
        assert not t.is_alive(), "barrier deadlock — a scope never reached its exit"

    assert observed["a_active"] is True
    assert observed["b_active"] is True, (
        "REGRESSION: the overlapping scope reported itself inactive; it read the "
        "module's own suppression value as an operator decision."
    )
    assert observed["b_env_after_a_exit"] == "true", (
        "REGRESSION: thread A's exit removed suppression while thread B was still "
        "inside its scope — B can now issue the IMDS request it suppressed."
    )
    assert observed["b_depth_after_a_exit"] == 1, (
        "late entrant did not join the refcount"
    )

    # And the refcount unwound cleanly once both scopes were gone.
    assert aws_imds._scope_depth == 0
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None, "leaked after both exits"


def test_reachable_imds_is_never_suppressed(no_suite_imds_guard, monkeypatch):
    """Genuine instance-role discovery must survive — fail closed on EC2.

    Negative control for `test_credential_resolution_under_scope_issues_no_imds_requests`:
    if this test also passed when IMDS answers, the guard would be suppressing
    unconditionally rather than reacting to reachability.
    """
    import api.aws_imds as aws_imds

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(aws_imds.socket, "create_connection", lambda *a, **kw: _FakeSock())
    aws_imds.reset_reachability_cache()

    assert aws_imds.imds_is_reachable() is True
    assert aws_imds.should_suppress_imds() is False

    with aws_imds.suppress_ec2_imds_probe("test") as active:
        assert active is False, "suppressed IMDS on a host where IMDS answers"
        assert os.environ.get("AWS_EC2_METADATA_DISABLED") is None


def test_unknown_reachability_fails_closed(no_suite_imds_guard, monkeypatch):
    """An unexpected probe error must NOT be read as 'not on EC2'."""
    import api.aws_imds as aws_imds

    def _explode(*a, **kw):
        raise ValueError("something unexpected")

    monkeypatch.setattr(aws_imds.socket, "create_connection", _explode)
    aws_imds.reset_reachability_cache()

    assert aws_imds.imds_is_reachable() is True, "unknown must fail closed"
    assert aws_imds.should_suppress_imds() is False


@pytest.mark.parametrize("operator_value", ["true", "false"])
def test_explicit_operator_setting_is_authoritative(
    no_suite_imds_guard, imds_unreachable, monkeypatch, operator_value
):
    """We never overwrite a deployment's own AWS_EC2_METADATA_DISABLED."""
    aws_imds = imds_unreachable
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", operator_value)

    assert aws_imds.should_suppress_imds() is False
    with aws_imds.suppress_ec2_imds_probe("test") as active:
        assert active is False
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == operator_value
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == operator_value


def test_explicit_env_credentials_still_resolve(
    no_suite_imds_guard, imds_unreachable, monkeypatch
):
    """Suppression must not break explicit AWS credentials."""
    aws_imds = imds_unreachable
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLEENVKEY123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "envsecret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)

    with aws_imds.suppress_ec2_imds_probe("test"):
        creds = _resolve_aws_credentials_like_bedrock_adapter()

    assert creds is not None and creds.access_key == "AKIAEXAMPLEENVKEY123"
    assert creds.method == "env"


def test_shared_credentials_file_still_resolves(
    no_suite_imds_guard, imds_unreachable, monkeypatch, tmp_path
):
    """Suppression must not break ~/.aws/credentials."""
    aws_imds = imds_unreachable
    cred_file = tmp_path / "credentials"
    cred_file.write_text(
        "[default]\n"
        "aws_access_key_id = AKIAEXAMPLEFILEKEY45\n"
        "aws_secret_access_key = filesecret\n"
    )
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))

    with aws_imds.suppress_ec2_imds_probe("test"):
        creds = _resolve_aws_credentials_like_bedrock_adapter()

    assert creds is not None and creds.access_key == "AKIAEXAMPLEFILEKEY45"
    assert creds.method == "shared-credentials-file"


def test_ecs_container_credentials_endpoint_is_not_the_imds_host():
    """ECS/container credentials use a different host and stay enabled."""
    from botocore.utils import ContainerMetadataFetcher

    import api.aws_imds as aws_imds

    assert ContainerMetadataFetcher.IP_ADDRESS != aws_imds._IMDS_HOST


def test_session_visit_rebuild_scope_covers_the_worker_thread(
    no_suite_imds_guard, imds_unreachable, imds_http_recorder, monkeypatch
):
    """End-to-end: the real rebuild chokepoint must hold the scope.

    FAILS BEFORE THE FIX. `_invoke_models_rebuild` is the shared seam every
    catalog rebuild passes through — foreground and the out-of-band worker
    thread — so asserting here proves the guard covers both.
    """
    import api.config as cfg

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)

    observed: dict[str, str | None] = {}

    def builder():
        observed["env"] = os.environ.get("AWS_EC2_METADATA_DISABLED")
        _resolve_aws_credentials_like_bedrock_adapter()
        return {"groups": []}

    cfg._invoke_models_rebuild(builder)

    assert observed["env"] == "true", (
        "REGRESSION: _invoke_models_rebuild() ran the provider-catalog build "
        "without suppressing the EC2 IMDS probe, so every cold "
        "/api/models?freshness=session_visit rebuild pays the link-local timeout."
    )
    assert imds_http_recorder == [], (
        f"REGRESSION: rebuild attempted IMDS requests: {imds_http_recorder}"
    )
