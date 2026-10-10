"""#8152 — a Gateway-routed chat uses its own profile's settings.

``_run_gateway_chat_streaming`` runs on a worker thread with no request-profile
context and read its config with the ambient ``get_config()``, which resolves
the process-default profile there. A chat in a named profile was sent with the
default profile's reasoning effort, runs-API switch, prompt settings and
prefill. It was also sent to the default profile's Gateway, with the default
profile's key: the URL comes from the same config and the key from the process
environment. A run resumed after a restart already used the session's own
profile (``_gateway_endpoint_for_profile``); a new run did not.

Every test here runs the production worker for one turn against a stand-in for
``urllib.request.urlopen`` that records the requests, with a real profile home
on disk whose ``config.yaml`` and ``.env`` differ from the default profile's.
"""
from __future__ import annotations

from collections import OrderedDict
from email.message import Message
import json
import shutil
import urllib.error

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS, create_stream_channel
from api.models import new_session

PROFILE = "work8152"
OTHER_PROFILE = "play8152"
DEFAULT_GATEWAY = "http://127.0.0.1:8642"
WORK_GATEWAY = "http://work-gateway.test:9001"
PLAY_GATEWAY = "http://play-gateway.test:9002"
PREFILL = [{"role": "assistant", "content": "Notes live in the work vault."}]


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, *args):
        return b"{}"

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
        yield b"data: [DONE]\n\n"


@pytest.fixture
def profiles_on_disk(tmp_path, monkeypatch):
    """Two named profile homes beside the default one, removed afterwards."""
    import api.profiles as profiles

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    for name in (
        "HERMES_WEBUI_GATEWAY_BASE_URL", "HERMES_WEBUI_GATEWAY_API_KEY", "API_SERVER_KEY",
        "HERMES_WEBUI_GATEWAY_USE_RUNS_API", "HERMES_PREFILL_MESSAGES_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    homes = {}
    work = profiles.get_hermes_home_for_profile(PROFILE)
    play = profiles.get_hermes_home_for_profile(OTHER_PROFILE)
    assert work != profiles.get_hermes_home_for_profile("default") != play
    try:
        work.mkdir(parents=True)
        (work / "config.yaml").write_text(
            f"webui_gateway_base_url: {WORK_GATEWAY}\n"
            "webui_gateway_use_runs_api: true\n"
            # relative on purpose: it has to be found in this profile's home
            "prefill_messages_file: work-prefill.json\n"
            "agent:\n  reasoning_effort: high\n"
            "webui:\n  pass_session_id: true\n",
            encoding="utf-8",
        )
        (work / ".env").write_text("API_SERVER_KEY=work-key\n", encoding="utf-8")
        (work / "work-prefill.json").write_text(json.dumps(PREFILL), encoding="utf-8")
        (work / "gateway_state.json").write_text(
            json.dumps({"platforms": {"discord": {"state": "connected"}}}), encoding="utf-8"
        )
        play.mkdir(parents=True)
        (play / "config.yaml").write_text(
            f"webui_gateway_base_url: {PLAY_GATEWAY}\n", encoding="utf-8"
        )
        (play / ".env").write_text("API_SERVER_KEY=play-key\n", encoding="utf-8")
        homes[PROFILE], homes[OTHER_PROFILE] = work, play
        yield homes
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(play, ignore_errors=True)


def _send(profile, tmp_path, monkeypatch, *, urlopen=None, name="turn", **worker_kwargs):
    """One turn of a new session in ``profile`` through the Gateway worker.

    Returns the session, the recorded requests and the events put on the
    stream's queue.
    """
    requests = []

    def recording_urlopen(req, timeout=0):
        requests.append({
            "url": req.full_url,
            "authorization": dict(req.header_items()).get("Authorization"),
            "body": json.loads(req.data.decode("utf-8")) if req.data else None,
        })
        if urlopen is not None:
            return urlopen(req)
        return _Response()

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", recording_urlopen)
    session = new_session()
    if profile is not None:
        session.profile = profile
    stream_id = f"stream-8152-{name}-{profile}"
    session.active_stream_id = stream_id
    session.pending_user_message = "Say hello"
    session.pending_attachments = []
    session.save()
    events = []
    channel = STREAMS[stream_id] = create_stream_channel()
    put = channel.put_nowait

    def recording_put(item):
        events.append(item)
        put(item)

    channel.put_nowait = recording_put
    try:
        gateway_chat._run_gateway_chat_streaming(
            session.session_id, "Say hello", "test-model", str(tmp_path), stream_id, [],
            **worker_kwargs,
        )
    finally:
        STREAMS.pop(stream_id, None)
    return session, requests, events


def _chat(requests):
    chats = [r for r in requests if r["url"].endswith("/v1/chat/completions")]
    assert len(chats) == 1, [r["url"] for r in requests]
    return chats[0]


class TestANamedProfile:
    def test_the_chat_goes_to_the_profiles_own_gateway(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert _chat(requests)["url"] == f"{WORK_GATEWAY}/v1/chat/completions"
        # nothing at all goes to the default profile's Gateway
        assert [r["url"] for r in requests if r["url"].startswith(DEFAULT_GATEWAY)] == []

    def test_with_the_profiles_own_key(self, profiles_on_disk, tmp_path, monkeypatch):
        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert requests
        assert {r["authorization"] for r in requests} == {"Bearer work-key"}

    def test_and_the_profiles_reasoning_effort(self, profiles_on_disk, tmp_path, monkeypatch):
        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert _chat(requests)["body"]["reasoning_effort"] == "high"

    def test_and_the_profiles_prompt_setting(self, profiles_on_disk, tmp_path, monkeypatch):
        """``webui.pass_session_id`` (#8150) is on in the named profile only."""
        session, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        system = _chat(requests)["body"]["messages"][0]
        assert system["role"] == "system"
        assert system["content"].endswith(f"- Session ID: {session.session_id}")

    def test_and_the_profiles_connected_platforms(self, profiles_on_disk, tmp_path, monkeypatch):
        """The delivery context lists the platforms of ``gateway_state.json``
        in the Hermes home. On this thread the ambient home is the process
        profile's, so the named profile's own file has to be named."""
        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        system = _chat(requests)["body"]["messages"][0]["content"]
        line = next(line for line in system.splitlines() if "Connected Platforms" in line)
        assert line == "**Connected Platforms:** local (files on this machine), discord: Connected ✓"

    def test_and_the_profiles_prefill(self, profiles_on_disk, tmp_path, monkeypatch):
        """``prefill_messages_file`` is a relative path in the named profile's
        config: it is that profile's file, in that profile's home."""
        _, requests, events = _send(PROFILE, tmp_path, monkeypatch)

        messages = _chat(requests)["body"]["messages"]
        assert [m["role"] for m in messages] == ["system", "assistant", "user"]
        assert messages[1]["content"] == PREFILL[0]["content"]
        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status and status[-1]["status"] == "loaded"

    def test_and_the_profiles_runs_api_switch(self, profiles_on_disk, tmp_path, monkeypatch):
        """The switch is read from the profile's config; whether the runs API
        is then used depends on the Gateway's answer, which is not this test."""
        seen = []
        real = gateway_chat._gateway_use_runs_api_enabled

        def recording(config_data=None, environ=None):
            seen.append(real(config_data, environ))
            return False

        monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", recording)

        _send(PROFILE, tmp_path, monkeypatch)

        assert seen == [True]

    def test_a_401_is_explained_with_the_profiles_key_in_mind(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The hint differs between "no key is set" and "the key does not
        match". The named profile has a key and the default has none."""

        _, _, events = _send(PROFILE, tmp_path, monkeypatch, urlopen=_unauthorized)

        errors = [item[1] for item in events if item[0] == "apperror"]
        assert errors and errors[-1]["type"] == "gateway_auth_error"
        assert errors[-1]["hint"].startswith("Check that HERMES_WEBUI_GATEWAY_API_KEY matches")


def _unauthorized(req):
    raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=Message(), fp=None)


class TestTheOthersAreNotTouched:
    def test_a_401_without_any_key_still_says_to_set_one(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The default profile of this test environment has no key."""
        _, requests, events = _send(None, tmp_path, monkeypatch, urlopen=_unauthorized)

        assert {r["authorization"] for r in requests} == {None}
        errors = [item[1] for item in events if item[0] == "apperror"]
        assert errors and errors[-1]["type"] == "gateway_auth_error"
        assert errors[-1]["hint"].startswith("Set HERMES_WEBUI_GATEWAY_API_KEY")

    def test_an_endpoint_handed_to_the_worker_wins_over_the_profiles(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """A reattached run names the Gateway it lives on; the worker must not
        replace it with the profile's current one."""
        _, requests, _ = _send(
            PROFILE, tmp_path, monkeypatch,
            reattach_endpoint=("http://reattach-gateway.test", "reattach-key"),
        )

        chat = _chat(requests)
        assert chat["url"] == "http://reattach-gateway.test/v1/chat/completions"
        assert chat["authorization"] == "Bearer reattach-key"

    def test_a_default_profile_chat_is_as_before(self, profiles_on_disk, tmp_path, monkeypatch):
        session, requests, _ = _send(None, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert session.profile == "default"
        assert chat["url"] == f"{DEFAULT_GATEWAY}/v1/chat/completions"
        assert chat["authorization"] is None
        assert "reasoning_effort" not in chat["body"]
        assert [m["role"] for m in chat["body"]["messages"]] == ["system", "user"]
        assert "Session ID" not in chat["body"]["messages"][0]["content"]
        assert "discord" not in chat["body"]["messages"][0]["content"]

    def test_one_profile_after_another_does_not_leak(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        _, first, _ = _send(PROFILE, tmp_path, monkeypatch, name="first")
        _, second, _ = _send(OTHER_PROFILE, tmp_path, monkeypatch, name="second")
        _, third, _ = _send(None, tmp_path, monkeypatch, name="third")

        assert (_chat(first)["url"], _chat(first)["authorization"]) == (
            f"{WORK_GATEWAY}/v1/chat/completions", "Bearer work-key",
        )
        play = _chat(second)
        assert (play["url"], play["authorization"]) == (
            f"{PLAY_GATEWAY}/v1/chat/completions", "Bearer play-key",
        )
        assert "reasoning_effort" not in play["body"]
        assert "Session ID" not in play["body"]["messages"][0]["content"]
        assert "discord" not in play["body"]["messages"][0]["content"]
        assert (_chat(third)["url"], _chat(third)["authorization"]) == (
            f"{DEFAULT_GATEWAY}/v1/chat/completions", None,
        )

    def test_the_process_environment_still_overrides_the_url_and_key(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """``HERMES_WEBUI_GATEWAY_BASE_URL`` and ``HERMES_WEBUI_GATEWAY_API_KEY``
        set for the WebUI process win over a profile's ``config.yaml`` URL and
        its ``API_SERVER_KEY``, as they do for a resumed run."""
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://operator-gateway.test")
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "operator-key")

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert chat["url"] == "http://operator-gateway.test/v1/chat/completions"
        assert chat["authorization"] == "Bearer operator-key"
        assert chat["body"]["reasoning_effort"] == "high"  # the rest is still the profile's

    def test_a_profiles_own_env_file_wins_over_the_process_environment_as_on_resume(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """When a profile's ``.env`` sets the same two variables, the profile's
        values are used. That is ``_gateway_endpoint_for_profile``'s order, which
        a resumed run has always had; a new run must not differ from it, or one
        session would talk to two Gateways across a restart."""
        (profiles_on_disk[PROFILE] / ".env").write_text(
            "HERMES_WEBUI_GATEWAY_BASE_URL=http://profile-env-gateway.test\n"
            "HERMES_WEBUI_GATEWAY_API_KEY=profile-env-key\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://operator-gateway.test")
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "operator-key")

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        on_resume = gateway_chat._gateway_endpoint_for_profile(PROFILE)
        assert on_resume == ("http://profile-env-gateway.test", "profile-env-key")
        assert chat["url"] == f"{on_resume[0]}/v1/chat/completions"
        assert chat["authorization"] == f"Bearer {on_resume[1]}"


class TestTheDeliveryPromptsHome:
    def test_a_named_home_is_where_the_platforms_are_read(self, profiles_on_disk):
        from api.streaming import _webui_delivery_context_prompt

        named = _webui_delivery_context_prompt({}, profiles_on_disk[PROFILE])
        other = _webui_delivery_context_prompt({}, profiles_on_disk[OTHER_PROFILE])

        assert "discord: Connected ✓" in named
        assert "discord" not in other

    def test_without_a_home_the_ambient_one_is_used_as_before(self, profiles_on_disk, monkeypatch):
        import sys
        import types

        from api.streaming import _webui_delivery_context_prompt

        # A stand-in module: hermes-agent is not installed where CI runs this.
        monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(
            get_hermes_home=lambda: profiles_on_disk[PROFILE],
            display_hermes_home=lambda: "~/.hermes",
        ))

        assert "discord: Connected ✓" in _webui_delivery_context_prompt({})
        assert "discord: Connected ✓" in _webui_delivery_context_prompt({}, None)
        # and a named home wins over the ambient one
        assert "discord" not in _webui_delivery_context_prompt({}, profiles_on_disk[OTHER_PROFILE])


class TestRelativePrefillPaths:
    """``_prefill_config_for_home``: what the worker hands the prefill loader."""

    def _anchor(self, config, home):
        from api.streaming import _prefill_config_for_home

        return _prefill_config_for_home(config, home)

    def test_a_relative_file_is_put_under_the_home(self, tmp_path):
        out = self._anchor({"prefill_messages_file": "notes/prefill.json"}, tmp_path)

        assert out["prefill_messages_file"] == str(tmp_path / "notes" / "prefill.json")

    @pytest.mark.parametrize("raw", ["/abs/prefill.json", "~/prefill.json"])
    def test_an_absolute_or_home_relative_file_is_left_alone(self, tmp_path, raw):
        assert self._anchor({"prefill_messages_file": raw}, tmp_path)["prefill_messages_file"] == raw

    def test_a_script_given_as_one_relative_path_is_put_under_the_home(self, tmp_path):
        out = self._anchor({"webui_prefill_messages_script": "recall.py"}, tmp_path)

        assert out["webui_prefill_messages_script"] == str(tmp_path / "recall.py")

    @pytest.mark.parametrize(
        "raw",
        ["python3 recall.py", "/abs/recall.py", ["python3", "recall.py"], "", 'unbalanced "quote'],
    )
    def test_any_other_script_is_left_alone(self, tmp_path, raw):
        """A command with arguments keeps its argv, as in
        ``_prefill_script_command``; a list is the admin's exact argv."""
        out = self._anchor({"webui_prefill_messages_script": raw}, tmp_path)

        assert out["webui_prefill_messages_script"] == raw

    @pytest.mark.parametrize("config", [{}, {"prefill_messages_file": ""}, {"prefill_messages_file": None}, None])
    def test_nothing_configured_stays_that_way(self, tmp_path, config):
        out = self._anchor(config, tmp_path)

        assert not out.get("prefill_messages_file")
        assert "webui_prefill_messages_script" not in out

    def test_the_config_handed_in_is_not_changed(self, tmp_path):
        config = {"prefill_messages_file": "prefill.json", "agent": {"reasoning_effort": "high"}}

        out = self._anchor(config, tmp_path)

        assert config["prefill_messages_file"] == "prefill.json"
        assert out is not config and out["agent"] is config["agent"]

    def test_without_a_home_nothing_is_anchored(self):
        assert self._anchor({"prefill_messages_file": "prefill.json"}, None) == {
            "prefill_messages_file": "prefill.json"
        }

    def test_the_loader_then_finds_the_file(self, tmp_path):
        """End of the chain: the loader reads the anchored path."""
        from api.streaming import _load_webui_prefill_context

        (tmp_path / "prefill.json").write_text(json.dumps(PREFILL), encoding="utf-8")

        loaded = _load_webui_prefill_context(
            self._anchor({"prefill_messages_file": "prefill.json"}, tmp_path)
        )

        assert loaded["status"] == "loaded" and loaded["message_count"] == 1


class TestTheProfileConfigHelper:
    def test_a_named_profile_gets_its_own_file(self, profiles_on_disk):
        cfg = gateway_chat._gateway_config_for_profile(PROFILE)

        assert cfg["webui_gateway_base_url"] == WORK_GATEWAY
        assert cfg["agent"]["reasoning_effort"] == "high"

    @pytest.mark.parametrize("name", [None, "", "default", "  "])
    def test_no_profile_is_the_default_profile(self, profiles_on_disk, name):
        from api.config import get_config

        assert gateway_chat._gateway_config_for_profile(name) is get_config()

    def test_a_profile_that_is_gone_is_not_given_the_defaults_settings(self, profiles_on_disk):
        """Profiles are islands (``get_config_for_profile_home``): a session
        whose profile home no longer exists reads no other profile's file."""
        assert gateway_chat._gateway_config_for_profile("gone8152") == {}
