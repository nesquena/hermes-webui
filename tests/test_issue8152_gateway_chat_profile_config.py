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
import os
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


class TestWhereARelativePrefillPathIsLookedUp:
    """``_prefill_profile_scope``: what the worker binds around the prefill load."""

    @pytest.fixture
    def bind(self):
        from api.streaming import _prefill_profile_scope

        def scope(home, environ=None, profile_keys=()):
            return _prefill_profile_scope(home, environ or {}, profile_keys)

        return scope

    def test_a_relative_file_is_looked_up_in_the_home(self, bind, tmp_path):
        from api.streaming import _prefill_base, _resolve_prefill_path

        with bind(tmp_path):
            assert _resolve_prefill_path("notes/prefill.json", _prefill_base()) == (
                tmp_path.resolve() / "notes" / "prefill.json"
            )

    @pytest.mark.parametrize("raw", ["/abs/prefill.json", "~/prefill.json"])
    def test_an_absolute_or_home_relative_file_is_left_alone(self, bind, tmp_path, raw):
        from pathlib import Path

        from api.streaming import _prefill_base, _resolve_prefill_path

        with bind(tmp_path):
            assert _resolve_prefill_path(raw, _prefill_base()) == Path(raw).expanduser()

    def test_a_script_given_as_one_relative_path_is_looked_up_in_the_home(self, bind, tmp_path):
        from api.streaming import _prefill_base, _prefill_script_command

        with bind(tmp_path):
            assert _prefill_script_command("recall.py", _prefill_base()) == [
                str(tmp_path.resolve() / "recall.py")
            ]

    def test_a_home_with_a_space_in_it_still_gives_one_script_argument(self, bind, tmp_path):
        from api.streaming import _prefill_base, _prefill_script_command

        home = tmp_path / "Application Support" / "hermes"
        home.mkdir(parents=True)
        with bind(home):
            assert _prefill_script_command("recall.py", _prefill_base()) == [
                str(home.resolve() / "recall.py")
            ]

    def test_a_quoted_script_name_with_a_space_is_one_argument_too(self, bind, tmp_path):
        from api.streaming import _prefill_base, _prefill_script_command

        with bind(tmp_path):
            assert _prefill_script_command('"my recall.py"', _prefill_base()) == [
                str(tmp_path.resolve() / "my recall.py")
            ]

    @pytest.mark.parametrize(
        ("raw", "argv"),
        [
            ('"/opt/my tools/recall.py"', ["/opt/my tools/recall.py"]),
            ("/abs/recall.py", ["/abs/recall.py"]),
            ("python3 recall.py", ["python3", "recall.py"]),
            (["python3", "recall.py"], ["python3", "recall.py"]),
        ],
    )
    def test_any_other_script_keeps_its_arguments(self, bind, tmp_path, raw, argv):
        """An absolute path is not moved; a command with arguments and a list
        are the admin's exact argv, as in ``_prefill_script_command``."""
        from api.streaming import _prefill_base, _prefill_script_command

        with bind(tmp_path):
            assert _prefill_script_command(raw, _prefill_base()) == argv

    def test_a_script_in_a_home_with_a_space_runs(self, bind, tmp_path):
        """End of the chain, with a real script."""
        from api.streaming import _load_webui_prefill_context

        if os.name == "nt":
            pytest.skip("a script path is run directly; needs a shebang")
        home = tmp_path / "Application Support"
        home.mkdir()
        script = home / "recall.py"
        script.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + repr(PREFILL) + "))\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

        with bind(home, dict(os.environ)):
            loaded = _load_webui_prefill_context({"webui_prefill_messages_script": "recall.py"})

        assert loaded.get("status") == "loaded", loaded
        assert loaded["message_count"] == 1

    def test_a_relative_file_in_a_home_with_a_space_is_found(self, bind, tmp_path):
        from api.streaming import _load_webui_prefill_context

        home = tmp_path / "Application Support"
        home.mkdir()
        (home / "prefill.json").write_text(json.dumps(PREFILL), encoding="utf-8")

        with bind(home):
            loaded = _load_webui_prefill_context({"prefill_messages_file": "prefill.json"})

        assert loaded["status"] == "loaded"

    def test_the_file_a_failed_script_falls_back_to_is_looked_up_in_the_home(self, bind, tmp_path):
        from api.streaming import _load_webui_prefill_context

        (tmp_path / "prefill.json").write_text(json.dumps(PREFILL), encoding="utf-8")

        with bind(tmp_path, dict(os.environ)):
            loaded = _load_webui_prefill_context({
                "webui_prefill_messages_script": "/nonexistent/recall-script",
                "prefill_messages_file": "prefill.json",
            })

        assert (loaded["status"], loaded["source"]) == ("loaded", "file_fallback"), loaded

    def test_the_file_an_oversized_script_falls_back_to_is_looked_up_in_the_home(
        self, bind, tmp_path
    ):
        from api.streaming import _apply_prefill_context_budget

        (tmp_path / "compact.json").write_text(
            json.dumps([{"role": "assistant", "content": "compact"}]), encoding="utf-8"
        )
        oversized = {
            "status": "loaded", "source": "script", "label": "recall.py", "message_count": 1,
            "messages": [{"role": "assistant", "content": "x" * 400}],
        }

        with bind(tmp_path):
            result = _apply_prefill_context_budget(
                oversized,
                {"webui_prefill_context_max_chars": 100, "prefill_messages_file": "compact.json"},
            )

        assert [m["content"] for m in result["messages"]] == ["compact"]

    def test_a_path_from_the_profiles_own_variable_is_the_profiles(self, bind, tmp_path):
        from api.streaming import _prefill_base

        with bind(tmp_path, profile_keys={"HERMES_PREFILL_MESSAGES_FILE"}):
            assert _prefill_base("HERMES_PREFILL_MESSAGES_FILE") == tmp_path.resolve()
            # exported for the whole process: the ambient rule, as before
            assert _prefill_base("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT") is None

    def test_the_ambient_profiles_base_is_beside_its_config_file(self, tmp_path, monkeypatch):
        import api.config as config
        import api.profiles as profiles
        from api.streaming import _prefill_base_for_home

        elsewhere = tmp_path / "etc hermes"
        elsewhere.mkdir()
        (elsewhere / "config.yaml").write_text("{}\n", encoding="utf-8")
        ambient = profiles.get_active_hermes_home()
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(elsewhere / "config.yaml"))
        try:
            assert _prefill_base_for_home(ambient) == elsewhere.resolve()
            assert _prefill_base_for_home(tmp_path) == tmp_path.resolve()
        finally:
            monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
            config.reload_config()

    def test_outside_a_scope_nothing_is_bound(self, monkeypatch):
        from api.streaming import _prefill_base, _prefill_bound, _prefill_env

        monkeypatch.setenv("REVIEW8153_NAME", "process")

        assert _prefill_bound() is None
        assert _prefill_base() is None and _prefill_base("REVIEW8153_NAME") is None
        assert _prefill_env("REVIEW8153_NAME") == "process"

    def test_the_scope_is_put_back_also_when_the_body_raises(self, bind, tmp_path):
        from api.streaming import _prefill_bound, _prefill_env

        with bind(tmp_path, {"REVIEW8153_NAME": "outer"}):
            with pytest.raises(RuntimeError):
                with bind(tmp_path / "inner", {"REVIEW8153_NAME": "inner"}):
                    assert _prefill_env("REVIEW8153_NAME") == "inner"
                    raise RuntimeError("boom")
            assert _prefill_env("REVIEW8153_NAME") == "outer"
        assert _prefill_bound() is None

    def test_another_thread_does_not_see_it(self, bind, tmp_path):
        import threading

        from api.streaming import _prefill_bound

        seen = []
        with bind(tmp_path, {"REVIEW8153_NAME": "mine"}):
            worker = threading.Thread(target=lambda: seen.append(_prefill_bound()))
            worker.start()
            worker.join()

        assert seen == [None]

    def test_the_process_environment_is_not_changed(self, bind, tmp_path):
        before = dict(os.environ)

        with bind(tmp_path, {"REVIEW8153_NAME": "snapshot"}):
            assert "REVIEW8153_NAME" not in os.environ

        assert dict(os.environ) == before


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


class TestVariablesInTheProfilesConfig:
    """``${VAR}`` in a profile's ``config.yaml`` is expanded from that profile's
    own environment: the same snapshot its Gateway key comes from (#8153 review).

    The snapshot is the process environment without the values another
    profile's ``.env`` loaded into it, with the session profile's own on top.
    """

    def _profile(self, profiles_on_disk, monkeypatch, *, env_file, ambient=None, foreign=True):
        import api.profiles as profiles

        home = profiles_on_disk[PROFILE]
        (home / "config.yaml").write_text(
            "webui_gateway_base_url: ${REVIEW8153_PARENT_URL}\n", encoding="utf-8"
        )
        (home / ".env").write_text(env_file, encoding="utf-8")
        if ambient is not None:
            monkeypatch.setenv("REVIEW8153_PARENT_URL", ambient)
            if foreign:
                # as if the process-active profile's .env had loaded it
                monkeypatch.setattr(
                    profiles, "_loaded_profile_env_keys",
                    set(profiles._loaded_profile_env_keys) | {"REVIEW8153_PARENT_URL"},
                )

    def test_the_url_and_the_key_come_from_the_same_profile(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        self._profile(
            profiles_on_disk, monkeypatch,
            env_file="REVIEW8153_PARENT_URL=http://owned-profile.test\nAPI_SERVER_KEY=owned-key\n",
            ambient="http://foreign-ambient.test",
        )

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert chat["url"] == "http://owned-profile.test/v1/chat/completions"
        assert chat["authorization"] == "Bearer owned-key"
        assert [r["url"] for r in requests if "foreign-ambient" in r["url"]] == []

    def test_a_variable_the_profile_does_not_set_is_not_filled_from_another_profiles(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The profile's key must never travel to another profile's Gateway.
        With nothing to expand to, the URL stays the literal text and the turn
        fails before any request is made."""
        self._profile(
            profiles_on_disk, monkeypatch,
            env_file="API_SERVER_KEY=owned-key\n",
            ambient="http://foreign-ambient.test",
        )

        _, requests, events = _send(PROFILE, tmp_path, monkeypatch)

        assert [r["url"] for r in requests if "foreign-ambient" in r["url"]] == []
        assert [r for r in requests if r["url"].endswith("/v1/chat/completions")] == []
        assert [item[0] for item in events if item[0] in ("apperror", "error")]

    def test_a_variable_set_for_the_whole_process_is_still_expanded(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """Exported by the operator, not loaded from a profile's ``.env``: it
        belongs to every profile's environment."""
        self._profile(
            profiles_on_disk, monkeypatch,
            env_file="API_SERVER_KEY=owned-key\n",
            ambient="http://operator-wide.test", foreign=False,
        )

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert chat["url"] == "http://operator-wide.test/v1/chat/completions"
        assert chat["authorization"] == "Bearer owned-key"


class TestAConfigFileOutsideTheHome:
    """``HERMES_CONFIG_PATH`` may point outside the Hermes home. A relative
    prefill path of the ambient profile has always been looked up beside that
    file, and still is (#8153 review); only another profile's is looked up in
    its own home."""

    @pytest.fixture
    def config_elsewhere(self, profiles_on_disk, tmp_path, monkeypatch):
        import api.config as config

        elsewhere = tmp_path / "etc hermes"
        elsewhere.mkdir()
        (elsewhere / "prefill.json").write_text(json.dumps(PREFILL), encoding="utf-8")
        (elsewhere / "recall.py").write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + repr(PREFILL) + "))\n",
            encoding="utf-8",
        )
        (elsewhere / "recall.py").chmod(0o755)
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(elsewhere / "config.yaml"))
        yield elsewhere
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
        config.reload_config()

    def _prefill_status(self, events):
        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status
        return status[-1]

    def test_the_ambient_profiles_relative_file_is_found_beside_its_config(
        self, config_elsewhere, tmp_path, monkeypatch
    ):
        (config_elsewhere / "config.yaml").write_text(
            "prefill_messages_file: prefill.json\n", encoding="utf-8"
        )

        _, requests, events = _send(None, tmp_path, monkeypatch)

        assert self._prefill_status(events)["status"] == "loaded"
        messages = _chat(requests)["body"]["messages"]
        assert [m["content"] for m in messages[1:-1]] == [PREFILL[0]["content"]]

    def test_the_ambient_profiles_relative_script_is_found_beside_its_config(
        self, config_elsewhere, tmp_path, monkeypatch
    ):
        import os

        if os.name == "nt":
            pytest.skip("a script path is run directly; needs a shebang")
        (config_elsewhere / "config.yaml").write_text(
            "webui_prefill_messages_script: recall.py\n", encoding="utf-8"
        )

        _, _, events = _send(None, tmp_path, monkeypatch)

        status = self._prefill_status(events)
        assert status["status"] == "loaded", status
        assert status["source"] == "script"

    def test_another_profile_still_uses_its_own_home(
        self, config_elsewhere, tmp_path, monkeypatch
    ):
        """The named profile's relative path is not looked up beside the
        ambient config, which has a ``prefill.json`` of its own."""
        (config_elsewhere / "config.yaml").write_text(
            "prefill_messages_file: prefill.json\n", encoding="utf-8"
        )
        (config_elsewhere / "prefill.json").write_text(
            json.dumps([{"role": "assistant", "content": "the ambient profile's notes"}]),
            encoding="utf-8",
        )

        _, requests, events = _send(PROFILE, tmp_path, monkeypatch)

        assert self._prefill_status(events)["status"] == "loaded"
        contents = [m["content"] for m in _chat(requests)["body"]["messages"]]
        assert PREFILL[0]["content"] in contents
        assert "the ambient profile's notes" not in contents


class TestSettingsReadFromTheEnvironment:
    """The prefill overrides and the runs-API switch can also be set in the
    environment. For a Gateway chat that is the session profile's snapshot too:
    its own ``.env`` counts, another profile's ``.env`` does not, and what the
    operator exported for the whole process still does (#8153 review)."""

    def _foreign(self, monkeypatch, name, value):
        """``name`` is in the process environment because the process-active
        profile's ``.env`` put it there."""
        import api.profiles as profiles

        monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            profiles, "_loaded_profile_env_keys", set(profiles._loaded_profile_env_keys) | {name}
        )

    def _other_prefill(self, tmp_path, text):
        path = tmp_path / f"{text.replace(' ', '-')}.json"
        path.write_text(json.dumps([{"role": "assistant", "content": text}]), encoding="utf-8")
        return path

    def _prefill_contents(self, requests):
        return [m["content"] for m in _chat(requests)["body"]["messages"][1:-1]]

    def test_another_profiles_prefill_override_does_not_replace_this_profiles_prefill(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        self._foreign(
            monkeypatch, "HERMES_PREFILL_MESSAGES_FILE",
            str(self._other_prefill(tmp_path, "the default profile's notes")),
        )

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert self._prefill_contents(requests) == [PREFILL[0]["content"]]

    def test_this_profiles_own_env_file_can_set_the_prefill_override(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        own = self._other_prefill(tmp_path, "from the work profile's env file")
        (profiles_on_disk[PROFILE] / ".env").write_text(
            f"API_SERVER_KEY=work-key\nHERMES_PREFILL_MESSAGES_FILE={own.as_posix()}\n",
            encoding="utf-8",
        )

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert self._prefill_contents(requests) == ["from the work profile's env file"]

    def test_an_override_exported_for_the_whole_process_still_wins(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(
            "HERMES_PREFILL_MESSAGES_FILE", str(self._other_prefill(tmp_path, "the operator's notes"))
        )

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        assert self._prefill_contents(requests) == ["the operator's notes"]

    def test_a_default_profile_chat_reads_the_process_environment_as_before(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """Loaded from the process-active profile's ``.env``: for a chat in
        that profile it is its own."""
        self._foreign(
            monkeypatch, "HERMES_PREFILL_MESSAGES_FILE",
            str(self._other_prefill(tmp_path, "the default profile's notes")),
        )
        # The default home of the test environment has no .env file, so hand
        # the worker what reading a real one would give it.
        real = gateway_chat._gateway_runtime_env_for_profile

        def with_its_own_env_file(profile_name):
            runtime_env = dict(real(profile_name))
            if not str(profile_name or "").strip() or profile_name == "default":
                runtime_env["HERMES_PREFILL_MESSAGES_FILE"] = os.environ["HERMES_PREFILL_MESSAGES_FILE"]
            return runtime_env

        monkeypatch.setattr(gateway_chat, "_gateway_runtime_env_for_profile", with_its_own_env_file)

        _, requests, _ = _send(None, tmp_path, monkeypatch)

        assert self._prefill_contents(requests) == ["the default profile's notes"]

    def _runs_switch(self, monkeypatch):
        seen = []
        real = gateway_chat._gateway_use_runs_api_enabled

        def recording(config_data=None, environ=None):
            seen.append(real(config_data, environ))
            return False

        monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", recording)
        return seen

    def test_the_runs_api_switch_in_this_profiles_env_file_is_read(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        home = profiles_on_disk[OTHER_PROFILE]  # its config.yaml does not set the switch
        (home / ".env").write_text(
            "API_SERVER_KEY=play-key\nHERMES_WEBUI_GATEWAY_USE_RUNS_API=true\n", encoding="utf-8"
        )
        seen = self._runs_switch(monkeypatch)

        _send(OTHER_PROFILE, tmp_path, monkeypatch)

        assert seen == [True]

    def test_another_profiles_runs_api_switch_is_not_read(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        self._foreign(monkeypatch, "HERMES_WEBUI_GATEWAY_USE_RUNS_API", "true")
        seen = self._runs_switch(monkeypatch)

        _send(OTHER_PROFILE, tmp_path, monkeypatch)

        assert seen == [False]


class TestTheEnvironmentScope:
    def test_it_is_thread_local_and_put_back_afterwards(self, monkeypatch):
        from api.config import _thread_ctx, _thread_local_env_value

        monkeypatch.setenv("REVIEW8153_ONLY_IN_PROCESS", "process")
        before_env = dict(getattr(_thread_ctx, "env", {}) or {})
        before_block = bool(getattr(_thread_ctx, "block_process_env_fallback", False))
        before_process = dict(os.environ)

        with gateway_chat._gateway_profile_environment({"REVIEW8153_IN_SNAPSHOT": "snapshot"}):
            assert _thread_local_env_value("REVIEW8153_IN_SNAPSHOT", "") == "snapshot"
            # the process environment is not a fallback inside the scope
            assert _thread_local_env_value("REVIEW8153_ONLY_IN_PROCESS", "") == ""
            assert "REVIEW8153_IN_SNAPSHOT" not in os.environ

        assert dict(getattr(_thread_ctx, "env", {}) or {}) == before_env
        assert bool(getattr(_thread_ctx, "block_process_env_fallback", False)) is before_block
        assert _thread_local_env_value("REVIEW8153_ONLY_IN_PROCESS", "") == "process"
        assert dict(os.environ) == before_process

    def test_it_is_put_back_when_the_body_raises(self):
        from api.config import _thread_ctx

        before_env = dict(getattr(_thread_ctx, "env", {}) or {})
        before_block = bool(getattr(_thread_ctx, "block_process_env_fallback", False))

        with pytest.raises(RuntimeError):
            with gateway_chat._gateway_profile_environment({"REVIEW8153_IN_SNAPSHOT": "x"}):
                raise RuntimeError("boom")

        assert dict(getattr(_thread_ctx, "env", {}) or {}) == before_env
        assert bool(getattr(_thread_ctx, "block_process_env_fallback", False)) is before_block

    def test_an_outer_scope_is_restored_by_an_inner_one(self):
        from api.config import _thread_local_env_value

        with gateway_chat._gateway_profile_environment({"REVIEW8153_NAME": "outer"}):
            with gateway_chat._gateway_profile_environment({"REVIEW8153_NAME": "inner"}):
                assert _thread_local_env_value("REVIEW8153_NAME", "") == "inner"
            assert _thread_local_env_value("REVIEW8153_NAME", "") == "outer"

    def test_another_thread_does_not_see_it(self):
        import threading

        from api.config import _thread_local_env_value

        seen = []
        with gateway_chat._gateway_profile_environment({"REVIEW8153_NAME": "mine"}):
            worker = threading.Thread(
                target=lambda: seen.append(_thread_local_env_value("REVIEW8153_NAME", "unset"))
            )
            worker.start()
            worker.join()

        assert seen == ["unset"]

    def test_a_resumed_runs_endpoint_expands_from_the_same_snapshot(
        self, profiles_on_disk, monkeypatch
    ):
        """``_gateway_endpoint_for_profile`` is what a run resumed after a
        restart uses. It had the same gap; the two paths stay in step."""
        import api.profiles as profiles

        home = profiles_on_disk[PROFILE]
        (home / "config.yaml").write_text(
            "webui_gateway_base_url: ${REVIEW8153_PARENT_URL}\n", encoding="utf-8"
        )
        (home / ".env").write_text(
            "REVIEW8153_PARENT_URL=http://owned-profile.test\nAPI_SERVER_KEY=owned-key\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEW8153_PARENT_URL", "http://foreign-ambient.test")
        monkeypatch.setattr(
            profiles, "_loaded_profile_env_keys",
            set(profiles._loaded_profile_env_keys) | {"REVIEW8153_PARENT_URL"},
        )

        assert gateway_chat._gateway_endpoint_for_profile(PROFILE) == (
            "http://owned-profile.test", "owned-key",
        )


class TestEachPrefillSettingFollowsTheBoundEnvironment:
    """The five places the prefill loaders read the environment. Inside a
    bound profile scope each one reads the snapshot and not the process;
    outside one, the process, exactly as before."""

    @pytest.fixture
    def scope(self, tmp_path):
        from api.streaming import _prefill_profile_scope

        def bind(**environ):
            return _prefill_profile_scope(tmp_path, environ)

        return bind

    def test_the_context_budget(self, scope, monkeypatch):
        from api.streaming import _prefill_context_max_chars

        monkeypatch.setenv("HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS", "111")
        assert _prefill_context_max_chars({}) == 111
        with scope(HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS="222"):
            assert _prefill_context_max_chars({}) == 222
        with scope():
            assert _prefill_context_max_chars({"webui_prefill_context_max_chars": 333}) == 333

    def test_the_script_timeout(self, scope, monkeypatch):
        from api.streaming import _prefill_script_timeout

        monkeypatch.setenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT", "7")
        assert _prefill_script_timeout({}) == 7.0
        with scope(HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT="9"):
            assert _prefill_script_timeout({}) == 9.0
        with scope():
            assert _prefill_script_timeout({"webui_prefill_messages_script_timeout": 3}) == 3.0

    def test_the_script(self, scope, monkeypatch):
        from api.streaming import _load_prefill_messages_script

        monkeypatch.setenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", "/nonexistent/process-script")
        assert _load_prefill_messages_script({})["label"] == "process-script"
        with scope(HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT="/nonexistent/snapshot-script"):
            assert _load_prefill_messages_script({})["label"] == "snapshot-script"
        with scope():
            assert _load_prefill_messages_script({})["status"] == "not_configured"

    def test_the_file(self, scope, monkeypatch, tmp_path):
        from api.streaming import _load_webui_prefill_context

        process = tmp_path / "process.json"
        process.write_text(json.dumps([{"role": "assistant", "content": "process"}]), encoding="utf-8")
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text(json.dumps([{"role": "assistant", "content": "snapshot"}]), encoding="utf-8")
        monkeypatch.setenv("HERMES_PREFILL_MESSAGES_FILE", str(process))

        assert _load_webui_prefill_context({})["messages"][0]["content"] == "process"
        with scope(HERMES_PREFILL_MESSAGES_FILE=str(snapshot)):
            assert _load_webui_prefill_context({})["messages"][0]["content"] == "snapshot"
        with scope():
            assert _load_webui_prefill_context({})["status"] == "not_configured"

    def test_the_file_named_in_a_budget_fallback(self, scope, monkeypatch, tmp_path):
        """``_apply_prefill_context_budget`` reads the file setting again when
        a script's output is over the budget."""
        from api.streaming import _apply_prefill_context_budget

        compact = tmp_path / "compact.json"
        compact.write_text(json.dumps([{"role": "assistant", "content": "compact"}]), encoding="utf-8")
        oversized = {
            "status": "loaded", "source": "script", "label": "recall.py", "message_count": 1,
            "messages": [{"role": "assistant", "content": "x" * 400}],
        }
        config = {"webui_prefill_context_max_chars": 100}
        monkeypatch.setenv("HERMES_PREFILL_MESSAGES_FILE", "/nonexistent/process.json")

        with scope(HERMES_PREFILL_MESSAGES_FILE=str(compact)):
            inside = _apply_prefill_context_budget(dict(oversized), config)
        with scope():
            without = _apply_prefill_context_budget(dict(oversized), config)

        assert [m["content"] for m in inside["messages"]] == ["compact"]
        assert without["source"] == "budget_compacted"


class TestPrefillSourcesNamedInAProfilesEnvFile:
    """A prefill file or script can be named in a profile's ``.env`` as well as
    in its ``config.yaml``. It is still that profile's source: a relative path
    is looked up in its home, and its script runs with its environment
    (#8153 review)."""

    SCRIPT = (
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "print(json.dumps([{'role': 'assistant', 'content': "
        "'token=' + os.environ.get('REVIEW8153_NOTES_TOKEN', 'unset')}]))\n"
    )

    def _status(self, events):
        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status
        return status[-1]

    def _contents(self, requests):
        return [m["content"] for m in _chat(requests)["body"]["messages"][1:-1]]

    def _script(self, home, name="recall.py"):
        if os.name == "nt":
            pytest.skip("a script path is run directly; needs a shebang")
        script = home / name
        script.write_text(self.SCRIPT, encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_a_relative_file_in_the_env_file_is_found_in_that_profiles_home(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        home = profiles_on_disk[OTHER_PROFILE]
        (home / "notes.json").write_text(
            json.dumps([{"role": "assistant", "content": "the play profile's notes"}]),
            encoding="utf-8",
        )
        (home / ".env").write_text(
            "API_SERVER_KEY=play-key\nHERMES_PREFILL_MESSAGES_FILE=notes.json\n", encoding="utf-8"
        )

        _, requests, events = _send(OTHER_PROFILE, tmp_path, monkeypatch)

        assert self._status(events)["status"] == "loaded"
        assert self._contents(requests) == ["the play profile's notes"]

    def test_a_relative_script_in_the_env_file_is_run_from_that_profiles_home(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        home = profiles_on_disk[OTHER_PROFILE]
        self._script(home)
        (home / ".env").write_text(
            "API_SERVER_KEY=play-key\nHERMES_WEBUI_PREFILL_MESSAGES_SCRIPT=recall.py\n",
            encoding="utf-8",
        )

        _, _, events = _send(OTHER_PROFILE, tmp_path, monkeypatch)

        status = self._status(events)
        assert (status["status"], status["source"]) == ("loaded", "script"), status

    def test_the_script_runs_with_the_profiles_environment(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The token the script needs is in the profile's ``.env``; the process
        has another profile's under the same name."""
        import api.profiles as profiles

        home = profiles_on_disk[OTHER_PROFILE]
        self._script(home)
        (home / "config.yaml").write_text(
            f"webui_gateway_base_url: {PLAY_GATEWAY}\nwebui_prefill_messages_script: recall.py\n",
            encoding="utf-8",
        )
        (home / ".env").write_text(
            "API_SERVER_KEY=play-key\nREVIEW8153_NOTES_TOKEN=play-token\n", encoding="utf-8"
        )
        monkeypatch.setenv("REVIEW8153_NOTES_TOKEN", "default-token")
        monkeypatch.setattr(
            profiles, "_loaded_profile_env_keys",
            set(profiles._loaded_profile_env_keys) | {"REVIEW8153_NOTES_TOKEN"},
        )

        _, requests, events = _send(OTHER_PROFILE, tmp_path, monkeypatch)

        assert self._status(events)["status"] == "loaded", self._status(events)
        assert self._contents(requests) == ["token=play-token"]

    def test_the_script_does_not_see_another_profiles_value_it_has_none_of(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        import api.profiles as profiles

        home = profiles_on_disk[OTHER_PROFILE]
        self._script(home)
        (home / "config.yaml").write_text(
            f"webui_gateway_base_url: {PLAY_GATEWAY}\nwebui_prefill_messages_script: recall.py\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEW8153_NOTES_TOKEN", "default-token")
        monkeypatch.setattr(
            profiles, "_loaded_profile_env_keys",
            set(profiles._loaded_profile_env_keys) | {"REVIEW8153_NOTES_TOKEN"},
        )

        _, requests, _ = _send(OTHER_PROFILE, tmp_path, monkeypatch)

        assert self._contents(requests) == ["token=unset"]

    def test_a_relative_override_exported_for_the_whole_process_keeps_its_old_place(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """Not from any profile's ``.env``: the operator's. It has always been
        looked up beside the process profile's config file, and still is, also
        for a chat in another profile."""
        import api.config as config

        elsewhere = tmp_path / "etc hermes"
        elsewhere.mkdir()
        (elsewhere / "config.yaml").write_text("{}\n", encoding="utf-8")
        (elsewhere / "operator.json").write_text(
            json.dumps([{"role": "assistant", "content": "the operator's notes"}]), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(elsewhere / "config.yaml"))
        monkeypatch.setenv("HERMES_PREFILL_MESSAGES_FILE", "operator.json")
        try:
            _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)
        finally:
            monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
            config.reload_config()

        assert self._contents(requests) == ["the operator's notes"]

    def test_a_relative_script_exported_for_the_whole_process_keeps_its_old_place(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The operator's, as the file override above: beside the process
        profile's config file, also for a chat in another profile. The named
        profile's home has a script of the same name that must not be the one."""
        import api.config as config

        elsewhere = tmp_path / "etc hermes"
        elsewhere.mkdir()
        (elsewhere / "config.yaml").write_text("{}\n", encoding="utf-8")
        operator = self._script(elsewhere)
        operator.write_text(
            "#!/usr/bin/env python3\nimport json\n"
            "print(json.dumps([{'role': 'assistant', 'content': 'the operator script'}]))\n",
            encoding="utf-8",
        )
        self._script(profiles_on_disk[PROFILE])
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(elsewhere / "config.yaml"))
        monkeypatch.setenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", "recall.py")
        try:
            _, requests, events = _send(PROFILE, tmp_path, monkeypatch)
        finally:
            monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
            config.reload_config()

        assert self._status(events)["status"] == "loaded", self._status(events)
        assert self._contents(requests) == ["the operator script"]
