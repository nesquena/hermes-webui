"""#8523 release gate: the Gateway worker's config resolver keeps master's HERMES_CONFIG_PATH handling.

The tag-delta audit found that ``config_for_session()`` dropped a root-profile Runs API opt-in that lives in an external
``HERMES_CONFIG_PATH`` while a named profile is process-active: the turn went to ``/v1/chat/completions`` instead of
``/v1/runs``. Root now resolves with ``isolate_config_override=True``; named profiles keep the earlier resolver. The
named-profile cases pin the endpoint the worker chose before ``config_for_session()`` existed (exp-v0.52.613).
"""
import io
import urllib.error

import pytest

from tests.test_session_reasoning_effort import isolated_reasoning_profiles, external_root_with_named_active  # noqa: F401
from api import config, models, gateway_chat, profiles


def _posted_urls(session, tmp_path, monkeypatch):
    session.active_stream_id = f"{session.session_id}-stream"
    session.pending_user_message = "hello"
    models.SESSIONS[session.session_id] = session
    monkeypatch.setitem(config.STREAMS, session.active_stream_id, config.create_stream_channel())
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *args: True)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *args: None)
    posted = []

    def urlopen(request, timeout=None):
        posted.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 409, "Fixture refusal", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", urlopen)
    gateway_chat._run_gateway_chat_streaming(
        session.session_id, "hello", session.model, str(tmp_path), session.active_stream_id,
        model_provider=session.model_provider,
    )
    return posted


def _enable_runs_api_in(path, monkeypatch):
    path.write_text(path.read_text() + "\nwebui_gateway_use_runs_api: true\n")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", raising=False)
    config.reload_config()


def test_root_runs_api_from_external_config_survives_named_active_profile(
    external_root_with_named_active, tmp_path, monkeypatch
):
    _root, _work, external = external_root_with_named_active
    _enable_runs_api_in(external, monkeypatch)
    session = models.Session(
        session_id="i8523-root-runs", profile="default", workspace=str(tmp_path),
        model="test-model", model_provider="openai",
    )
    assert _posted_urls(session, tmp_path, monkeypatch) == ["http://root-file:8650/v1/runs"]


@pytest.mark.parametrize("profile", ["", "default"], ids=["empty-profile", "default-alias"])
def test_root_aliases_resolve_like_root(external_root_with_named_active, tmp_path, monkeypatch, profile):
    _root, _work, external = external_root_with_named_active
    _enable_runs_api_in(external, monkeypatch)
    session = models.Session(
        session_id=f"i8523-alias-{profile or 'empty'}", profile=profile, workspace=str(tmp_path),
        model="test-model", model_provider="openai",
    )
    assert _posted_urls(session, tmp_path, monkeypatch)[0].endswith("/v1/runs")


@pytest.mark.parametrize("opt_in_where", ["external", "work-file"])
def test_named_profile_keeps_the_earlier_resolver(
    external_root_with_named_active, tmp_path, monkeypatch, opt_in_where
):
    _root, work, external = external_root_with_named_active
    target = external if opt_in_where == "external" else (work / "config.yaml")
    if not target.exists():
        target.write_text("")
    _enable_runs_api_in(target, monkeypatch)
    assert profiles.get_active_hermes_home() == work
    session = models.Session(
        session_id=f"i8523-work-{opt_in_where}", profile="work", workspace=str(tmp_path),
        model="test-model", model_provider="openai",
    )
    posted = _posted_urls(session, tmp_path, monkeypatch)
    # exp-v0.52.613 (pinned by running this file there): the process-active named profile reads the ambient
    # HERMES_CONFIG_PATH, so the opt-in counts when it lives in the external file and not when it is only in
    # work's own config.yaml.
    expected = {"external": "/v1/runs", "work-file": "/v1/chat/completions"}[opt_in_where]
    assert posted and posted[0].endswith(expected), posted
