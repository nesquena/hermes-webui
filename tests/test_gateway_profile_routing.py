"""Regression tests for profile-scoped WebUI -> Gateway routing."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from api.gateway_chat import (
    _clear_gateway_run_starting,
    _gateway_base_url_for_profile,
    _mark_gateway_run_starting,
    _publish_gateway_run_id,
    stop_gateway_run,
)


def test_gateway_profile_url_uses_multiplex_prefix():
    env = {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://127.0.0.1:8642/"}

    assert _gateway_base_url_for_profile("atlas", environ=env) == (
        "http://127.0.0.1:8642/p/atlas"
    )
    assert _gateway_base_url_for_profile("HOFFEE CMO", environ=env) == (
        "http://127.0.0.1:8642/p/HOFFEE%20CMO"
    )


def test_gateway_profile_url_preserves_owner_route_without_profile():
    env = {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://127.0.0.1:8642/"}

    assert _gateway_base_url_for_profile(None, environ=env) == "http://127.0.0.1:8642"


def test_stop_gateway_run_targets_owning_profile():
    stream_id = "stream-profile-stop"
    _mark_gateway_run_starting(stream_id, profile="mentor")
    _publish_gateway_run_id(stream_id, "run-stop")
    response = MagicMock()
    response.status = 202
    response.code = 202
    response.geturl.return_value = (
        "http://127.0.0.1:8642/p/mentor/v1/runs/run-stop/stop"
    )
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    opener = MagicMock()
    opener.open.return_value = response

    try:
        with patch("urllib.request.build_opener", return_value=opener), patch(
            "api.gateway_chat._gateway_api_key", return_value="secret"
        ):
            assert stop_gateway_run("run-stop") is True
    finally:
        _clear_gateway_run_starting(stream_id)

    request = opener.open.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:8642/p/mentor/v1/runs/run-stop/stop"
    )
    assert request.get_header("Authorization") == "Bearer secret"


def test_stop_gateway_run_fails_closed_without_atomic_profile_binding():
    with patch("urllib.request.build_opener") as build_opener:
        assert stop_gateway_run("unowned-run") is False
    build_opener.assert_not_called()


def test_stop_gateway_run_fails_closed_on_colliding_run_id():
    streams = ("stream-collision-a", "stream-collision-b")
    _mark_gateway_run_starting(streams[0], profile="atlas")
    _mark_gateway_run_starting(streams[1], profile="mentor")
    _publish_gateway_run_id(streams[0], "colliding-run")
    _publish_gateway_run_id(streams[1], "colliding-run")
    try:
        with patch("urllib.request.build_opener") as build_opener:
            assert stop_gateway_run("colliding-run") is False
        build_opener.assert_not_called()
    finally:
        for stream_id in streams:
            _clear_gateway_run_starting(stream_id)


def _run_stop_with_bound_profile(stream_id, run_id, profile):
    """Mark+publish a binding and return the recorded outbound stop request."""
    _mark_gateway_run_starting(stream_id, profile=profile)
    _publish_gateway_run_id(stream_id, run_id)
    response = MagicMock()
    response.status = 202
    response.code = 202
    response.geturl.return_value = (
        f"http://127.0.0.1:8642"
        f"{'' if not profile or profile == 'default' else '/p/' + profile}"
        f"/v1/runs/{run_id}/stop"
    )
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    opener = MagicMock()
    opener.open.return_value = response
    try:
        with patch("urllib.request.build_opener", return_value=opener), patch(
            "api.gateway_chat._gateway_api_key", return_value="secret"
        ):
            result = stop_gateway_run(run_id)
    finally:
        _clear_gateway_run_starting(stream_id)
    return result, opener, response


def test_stop_gateway_run_treats_bound_empty_profile_as_owned_owner_route():
    """D5: a bound empty profile is the unscoped owner route, not 'unowned'.

    The audit probe showed ``stop_gateway_run`` collapsing a bound profile ``''``
    to ``None`` and making no outbound call. It must instead call the unscoped
    owner URL exactly once.
    """
    result, opener, _ = _run_stop_with_bound_profile(
        "stream-empty-profile-stop", "run-empty-profile", ""
    )

    assert result is True
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:8642/v1/runs/run-empty-profile/stop"
    )
    assert request.get_header("Authorization") == "Bearer secret"


def test_stop_gateway_run_uses_root_route_for_explicit_default_profile():
    """The root/default profile is served without a multiplexing prefix."""
    result, opener, _ = _run_stop_with_bound_profile(
        "stream-default-profile-stop", "run-default-profile", "default"
    )

    assert result is True
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:8642/v1/runs/run-default-profile/stop"
    )


def test_stop_gateway_run_still_fails_closed_for_pending_empty_profile():
    """A non-ready empty-profile binding is still unowned: no outbound call."""
    stream_id = "stream-pending-empty-profile"
    _mark_gateway_run_starting(stream_id, profile="")
    try:
        with patch("urllib.request.build_opener") as build_opener:
            assert stop_gateway_run("run-pending-empty") is False
        build_opener.assert_not_called()
    finally:
        _clear_gateway_run_starting(stream_id)
