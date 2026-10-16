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
import threading
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

    def test_a_regenerated_turn_is_no_different(self, profiles_on_disk, tmp_path, monkeypatch):
        """Regeneration starts the same worker with ``regeneration=True`` and
        goes through the same prelude. The route that starts it is not driven
        here."""
        _, requests, events = _send(
            PROFILE, tmp_path, monkeypatch, name="regenerated", regeneration=True
        )

        assert requests
        assert all(r["url"].startswith(WORK_GATEWAY) for r in requests), requests
        assert {r["authorization"] for r in requests} == {"Bearer work-key"}
        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status and status[-1]["status"] == "loaded"

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

    def test_two_profiles_at_the_same_moment_each_keep_their_own(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """Two workers, each held at its chat request until the other has
        built its own: neither turn's settings come from the other's thread."""
        both_ready = threading.Barrier(2, timeout=30)
        requests, lock = [], threading.Lock()

        def urlopen(req, timeout=0):
            if req.full_url.endswith("/v1/chat/completions"):
                both_ready.wait()
                with lock:
                    requests.append({
                        "url": req.full_url,
                        "authorization": dict(req.header_items()).get("Authorization"),
                        "body": json.loads(req.data.decode("utf-8")),
                    })
            return _Response()

        monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", urlopen)
        turns = []
        for profile in (PROFILE, OTHER_PROFILE):
            session = new_session()
            session.profile = profile
            stream_id = f"stream-8152-together-{profile}"
            session.active_stream_id = stream_id
            session.pending_user_message = "Say hello"
            session.pending_attachments = []
            session.save()
            STREAMS[stream_id] = create_stream_channel()
            turns.append((stream_id, threading.Thread(
                target=gateway_chat._run_gateway_chat_streaming,
                args=(session.session_id, "Say hello", "test-model", str(tmp_path), stream_id, []),
            )))
        try:
            for _, thread in turns:
                thread.start()
            for _, thread in turns:
                thread.join(timeout=60)
            assert not any(thread.is_alive() for _, thread in turns)
        finally:
            both_ready.abort()
            for stream_id, _ in turns:
                STREAMS.pop(stream_id, None)

        by_url = {request["url"]: request for request in requests}
        assert set(by_url) == {
            f"{WORK_GATEWAY}/v1/chat/completions", f"{PLAY_GATEWAY}/v1/chat/completions",
        }
        work = by_url[f"{WORK_GATEWAY}/v1/chat/completions"]
        play = by_url[f"{PLAY_GATEWAY}/v1/chat/completions"]
        assert (work["authorization"], play["authorization"]) == (
            "Bearer work-key", "Bearer play-key",
        )
        assert work["body"]["reasoning_effort"] == "high"
        assert "reasoning_effort" not in play["body"]
        assert "discord" in work["body"]["messages"][0]["content"]
        assert "discord" not in play["body"]["messages"][0]["content"]
        assert PREFILL[0] in work["body"]["messages"]
        assert PREFILL[0] not in play["body"]["messages"]

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
        """The same settings as the ambient config. Not the same object since
        round 5: the file is expanded again under the profile's environment."""
        from api.config import get_config

        assert gateway_chat._gateway_config_for_profile(name) == get_config()

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

        def with_its_own_env_file(profile_name, home=None):
            runtime_env = dict(real(profile_name, home))
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


class TestAConfigPathInsideTheProfilesHome:
    """``HERMES_CONFIG_PATH`` may name a file in the session profile's own
    home. The per-profile loader then hands back the process-wide config cache,
    whose ``${VAR}`` were expanded from the process environment when it was
    loaded: the profile's key went to the URL another profile's value expanded
    to (#8153 round 5). The file is read and expanded under the profile's own
    environment instead."""

    @pytest.fixture
    def config_in_the_home(self, profiles_on_disk, monkeypatch):
        import api.config as config
        import api.profiles as profiles

        home = profiles_on_disk[PROFILE]
        (home / "config.yaml").write_text(
            "webui_gateway_base_url: ${ROUND5_GATEWAY_URL}\n", encoding="utf-8"
        )
        (home / ".env").write_text(
            "ROUND5_GATEWAY_URL=http://owned-profile.test\nAPI_SERVER_KEY=owned-key\n",
            encoding="utf-8",
        )
        # as if the process-active profile's .env had loaded its own value
        monkeypatch.setenv("ROUND5_GATEWAY_URL", "http://foreign-ambient.test")
        monkeypatch.setattr(
            profiles, "_loaded_profile_env_keys",
            set(profiles._loaded_profile_env_keys) | {"ROUND5_GATEWAY_URL"},
        )

        def point_at(path):
            monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
            config.reload_config()  # the cache now holds the process's expansion

        yield home, point_at
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
        config.reload_config()

    def test_the_url_and_the_key_are_both_the_profiles(
        self, config_in_the_home, tmp_path, monkeypatch
    ):
        import api.config as config

        home, point_at = config_in_the_home
        point_at(home / "config.yaml")
        assert config.get_config()["webui_gateway_base_url"] == "http://foreign-ambient.test"

        _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert chat["url"] == "http://owned-profile.test/v1/chat/completions"
        assert chat["authorization"] == "Bearer owned-key"
        assert [r["url"] for r in requests if "foreign-ambient" in r["url"]] == []

    def test_a_resumed_run_too(self, config_in_the_home):
        home, point_at = config_in_the_home
        point_at(home / "config.yaml")

        assert gateway_chat._gateway_endpoint_for_profile(PROFILE) == (
            "http://owned-profile.test", "owned-key",
        )

    def test_the_file_that_is_named_is_the_one_read(self, config_in_the_home):
        """Not ``config.yaml`` beside it: the override names the file."""
        home, point_at = config_in_the_home
        (home / "other.yaml").write_text(
            "webui_gateway_base_url: ${ROUND5_GATEWAY_URL}/named\n", encoding="utf-8"
        )
        point_at(home / "other.yaml")

        assert gateway_chat._gateway_endpoint_for_profile(PROFILE) == (
            "http://owned-profile.test/named", "owned-key",
        )

    def test_the_documented_defaults_are_still_filled_in(self, config_in_the_home):
        home, point_at = config_in_the_home
        point_at(home / "config.yaml")

        cfg = gateway_chat._gateway_config_for_profile(PROFILE)

        assert "helpful" in cfg["agent"]["personalities"]
        assert cfg["experimental"]["unified_session_db"] is False

    def test_a_config_set_in_memory_is_still_what_is_served(
        self, config_in_the_home, monkeypatch
    ):
        """A caller that rebound ``cfg`` has no file to expand again."""
        import api.config as config

        home, point_at = config_in_the_home
        point_at(home / "config.yaml")
        monkeypatch.setattr(config, "cfg", {"webui_gateway_base_url": "http://in-memory.test"})

        assert gateway_chat._gateway_endpoint_for_profile(PROFILE) == (
            "http://in-memory.test", "owned-key",
        )

    def test_the_shared_cache_is_left_as_the_process_expanded_it(self, config_in_the_home):
        import api.config as config

        home, point_at = config_in_the_home
        point_at(home / "config.yaml")

        gateway_chat._gateway_config_for_profile(PROFILE)

        assert config.get_config()["webui_gateway_base_url"] == "http://foreign-ambient.test"

    def test_other_callers_of_the_loader_get_the_cache_as_before(self, config_in_the_home):
        import api.config as config

        home, point_at = config_in_the_home
        point_at(home / "config.yaml")

        assert config.get_config_for_profile_home(home) is config.get_config()

    def test_the_default_profile_with_its_config_file_elsewhere(
        self, config_in_the_home, tmp_path, monkeypatch
    ):
        """The other way a home is the ambient one: it is the active profile's,
        and ``HERMES_CONFIG_PATH`` names a file outside it."""
        home, point_at = config_in_the_home
        elsewhere = tmp_path / "etc"
        elsewhere.mkdir()
        (elsewhere / "config.yaml").write_text(
            "webui_gateway_base_url: ${ROUND5_GATEWAY_URL}\n", encoding="utf-8"
        )
        point_at(elsewhere / "config.yaml")
        # The default home of the test environment has no .env file, so hand
        # over what reading a real one would give.
        real = gateway_chat._gateway_runtime_env_for_profile

        def with_its_own_env_file(profile_name, home=None):
            return {**real(profile_name, home), "ROUND5_GATEWAY_URL": "http://default-own.test"}

        monkeypatch.setattr(gateway_chat, "_gateway_runtime_env_for_profile", with_its_own_env_file)

        assert gateway_chat._gateway_endpoint_for_profile(None)[0] == "http://default-own.test"

    @pytest.mark.parametrize("no_home", [None, "", object()], ids=["none", "empty", "not-a-path"])
    def test_the_loader_without_a_usable_home_expands_in_the_scope_too(
        self, config_in_the_home, no_home
    ):
        import api.config as config

        home, point_at = config_in_the_home
        point_at(home / "config.yaml")

        with gateway_chat._gateway_profile_environment({"ROUND5_GATEWAY_URL": "http://scoped.test"}):
            scoped = config.get_config_for_profile_home(no_home, expand_in_scope=True)
            cached = config.get_config_for_profile_home(no_home)

        assert scoped["webui_gateway_base_url"] == "http://scoped.test"
        assert cached is config.get_config()


class TestOneHomeForTheWholeTurn:
    """The profile's home is found once and that one directory serves the
    environment, the config, the prefill loaders and the delivery context
    (#8153 round 5). Looked up again for each, a home that is a symlink
    retargeted in between gave one directory's Gateway URL another's key."""

    @pytest.fixture
    def retargeted(self, profiles_on_disk, tmp_path, monkeypatch):
        """The profile's home is a symlink to one directory, switched to
        another as soon as the profile's ``.env`` has been read."""
        import api.profiles as profiles

        # Both under the profiles root: a named profile's home has to be.
        link = profiles_on_disk[PROFILE]
        first = link.parent / "first8152"
        shutil.move(str(link), str(first))
        second = link.parent / "second8152"
        second.mkdir()
        (second / "config.yaml").write_text(
            f"webui_gateway_base_url: {PLAY_GATEWAY}\n", encoding="utf-8"
        )
        (second / ".env").write_text("API_SERVER_KEY=second-key\n", encoding="utf-8")
        os.symlink(first, link)
        read_env = profiles.get_profile_runtime_env
        switched = []

        def read_env_then_switch(home):
            env = read_env(home)
            if not switched:
                switched.append(True)
                os.unlink(link)
                os.symlink(second, link)
            return env

        monkeypatch.setattr(profiles, "get_profile_runtime_env", read_env_then_switch)
        try:
            yield switched
        finally:
            if os.path.islink(link):
                os.unlink(link)
            shutil.rmtree(first, ignore_errors=True)
            shutil.rmtree(second, ignore_errors=True)

    def test_a_new_turn_keeps_to_the_home_it_started_in(
        self, retargeted, tmp_path, monkeypatch
    ):
        _, requests, events = _send(PROFILE, tmp_path, monkeypatch)

        assert retargeted
        assert requests
        assert all(r["url"].startswith(WORK_GATEWAY) for r in requests), requests
        assert {r["authorization"] for r in requests} == {"Bearer work-key"}
        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status and status[-1]["status"] == "loaded"

    def test_a_resumed_runs_endpoint_too(self, retargeted):
        assert gateway_chat._gateway_endpoint_for_profile(PROFILE) == (WORK_GATEWAY, "work-key")
        assert retargeted

    def test_the_home_is_looked_up_once_in_a_turn(self, profiles_on_disk, tmp_path, monkeypatch):
        lookups = []
        home_for = gateway_chat._gateway_home_for_profile

        def counting(profile_name):
            lookups.append(profile_name)
            return home_for(profile_name)

        monkeypatch.setattr(gateway_chat, "_gateway_home_for_profile", counting)

        _send(PROFILE, tmp_path, monkeypatch)

        assert lookups == [PROFILE]

    def test_and_once_for_a_resumed_runs_endpoint(self, profiles_on_disk, monkeypatch):
        lookups = []
        home_for = gateway_chat._gateway_home_for_profile

        def counting(profile_name):
            lookups.append(profile_name)
            return home_for(profile_name)

        monkeypatch.setattr(gateway_chat, "_gateway_home_for_profile", counting)

        gateway_chat._gateway_endpoint_for_profile(PROFILE)

        assert lookups == [PROFILE]

    def test_a_home_the_lookup_hands_back_as_a_link_is_made_real(
        self, profiles_on_disk, tmp_path, monkeypatch
    ):
        """The default profile's home is not resolved by the lookup."""
        import api.profiles as profiles

        real = tmp_path / "real-default"
        real.mkdir()
        link = tmp_path / "linked-default"
        os.symlink(real, link)
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: link)

        assert gateway_chat._gateway_home_for_profile(None) == real.resolve()

    def test_the_home_handed_on_is_the_real_directory(self, profiles_on_disk):
        link = profiles_on_disk[PROFILE]
        real = link.parent / "real8152"
        shutil.move(str(link), str(real))
        os.symlink(real, link)
        try:
            assert gateway_chat._gateway_home_for_profile(PROFILE) == real.resolve()
        finally:
            os.unlink(link)
            shutil.rmtree(real, ignore_errors=True)


class TestHermesHomeIsTheProfilesOwn:
    """``HERMES_HOME`` in the profile's environment is that profile's home, as
    in the local worker's profile scope (``api/profiles.py``), so a
    ``${HERMES_HOME}`` in its config and a recall script see their own profile
    and not the process's (#8153 round 5)."""

    def test_in_the_snapshot(self, profiles_on_disk, monkeypatch):
        import api.profiles as profiles

        monkeypatch.setenv("HERMES_HOME", str(profiles.get_hermes_home_for_profile("default")))

        environ = gateway_chat._gateway_environment_for_profile(PROFILE)

        assert environ["HERMES_HOME"] == str(gateway_chat._gateway_home_for_profile(PROFILE))
        assert os.environ["HERMES_HOME"] == str(profiles.get_hermes_home_for_profile("default"))

    def test_in_the_profiles_config(self, profiles_on_disk, tmp_path, monkeypatch):
        import api.profiles as profiles

        home = profiles_on_disk[PROFILE]
        (home / "config.yaml").write_text(
            f"webui_gateway_base_url: {WORK_GATEWAY}\n"
            "prefill_messages_file: ${HERMES_HOME}/work-prefill.json\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(profiles.get_hermes_home_for_profile("default")))

        _, requests, events = _send(PROFILE, tmp_path, monkeypatch)

        status = [item[1]["prefill"] for item in events if item[0] == "context_status"]
        assert status and status[-1]["status"] == "loaded", status
        assert _chat(requests)["body"]["messages"][1]["content"] == PREFILL[0]["content"]

    def test_for_a_recall_script(self, profiles_on_disk, tmp_path, monkeypatch):
        import api.profiles as profiles

        home = profiles_on_disk[OTHER_PROFILE]
        (home / "recall.py").write_text(
            "#!/usr/bin/env python3\nimport json, os\n"
            "print(json.dumps([{'role': 'assistant', 'content': os.environ.get('HERMES_HOME', 'unset')}]))\n",
            encoding="utf-8",
        )
        (home / "recall.py").chmod(0o755)
        (home / "config.yaml").write_text(
            f"webui_gateway_base_url: {PLAY_GATEWAY}\nwebui_prefill_messages_script: recall.py\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(profiles.get_hermes_home_for_profile("default")))

        _, requests, _ = _send(OTHER_PROFILE, tmp_path, monkeypatch)

        contents = [m["content"] for m in _chat(requests)["body"]["messages"] if m["role"] == "assistant"]
        assert contents == [str(gateway_chat._gateway_home_for_profile(OTHER_PROFILE))]


class TestWhileAnotherProfilesWorkerRuns:
    """A background worker of another profile mirrors that profile's runtime
    environment into ``os.environ`` for as long as it runs, and
    ``_loaded_profile_env_keys`` does not know. Read live, those values looked
    like the operator's own: a chat's config expanded to the other profile's
    Gateway URL and went there with its own key (#8153 round 6). The snapshot
    is built from the process environment as it was before any worker mirrored
    into it."""

    OPERATOR_GATEWAY = "http://operator-supplied.test:9003"

    @pytest.fixture
    def worker_of_the_other_profile(self, profiles_on_disk, monkeypatch):
        """Opens the other profile's worker scope, as its title task does."""
        import api.profiles as profiles

        (profiles_on_disk[PROFILE] / "config.yaml").write_text(
            "webui_gateway_base_url: ${REVIEW8153_PARENT_URL}\n", encoding="utf-8"
        )
        # The operator exports the variable; it is every profile's.
        monkeypatch.setenv("REVIEW8153_PARENT_URL", self.OPERATOR_GATEWAY)
        (profiles_on_disk[OTHER_PROFILE] / ".env").write_text(
            f"API_SERVER_KEY=play-key\nREVIEW8153_PARENT_URL={PLAY_GATEWAY}\n", encoding="utf-8"
        )
        return lambda: profiles.profile_env_for_background_worker(
            OTHER_PROFILE, scope_skill_modules=False
        )

    def test_a_new_turn_keeps_its_own_pair(
        self, worker_of_the_other_profile, tmp_path, monkeypatch
    ):
        with worker_of_the_other_profile():
            assert os.environ["REVIEW8153_PARENT_URL"] == PLAY_GATEWAY  # the mirror is live
            _, requests, _ = _send(PROFILE, tmp_path, monkeypatch)

        chat = _chat(requests)
        assert chat["url"] == f"{self.OPERATOR_GATEWAY}/v1/chat/completions"
        assert chat["authorization"] == "Bearer work-key"
        assert [r["url"] for r in requests if PLAY_GATEWAY in r["url"]] == []

    def test_a_resumed_runs_endpoint_too(self, worker_of_the_other_profile):
        with worker_of_the_other_profile():
            endpoint = gateway_chat._gateway_endpoint_for_profile(PROFILE)

        assert endpoint == (self.OPERATOR_GATEWAY, "work-key")

    def test_a_default_profile_chat_keeps_the_launchers_key(
        self, worker_of_the_other_profile, monkeypatch
    ):
        """The launcher's ``API_SERVER_KEY`` authenticates root sessions; the
        other profile's ``.env`` has its own under the same name."""
        monkeypatch.setenv("API_SERVER_KEY", "launcher-key")

        with worker_of_the_other_profile():
            assert os.environ["API_SERVER_KEY"] == "play-key"  # the mirror is live
            environ = gateway_chat._gateway_environment_for_profile(None)

        assert gateway_chat._gateway_api_key(environ) == "launcher-key"

    def test_once_the_worker_is_done_the_live_environment_is_read_again(
        self, worker_of_the_other_profile, monkeypatch
    ):
        with worker_of_the_other_profile():
            pass
        monkeypatch.setenv("REVIEW8153_PARENT_URL", "http://changed-later.test")

        assert gateway_chat._gateway_endpoint_for_profile(PROFILE)[0] == "http://changed-later.test"


class TestTheProcessEnvironmentWithoutMirrors:
    """``api.profiles`` keeps, for each name a worker has mirrored into
    ``os.environ``, the value it had before the first mirror still live."""

    @pytest.fixture
    def env(self, monkeypatch):
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "_env_mirrors", {})
        monkeypatch.setenv("ROUND6_SET", "process")
        monkeypatch.delenv("ROUND6_UNSET", raising=False)
        return profiles

    def test_a_mirrored_name_reads_as_it_was(self, env, monkeypatch):
        env.note_env_mirror({"ROUND6_SET": "process", "ROUND6_UNSET": None})
        monkeypatch.setenv("ROUND6_SET", "mirrored")
        monkeypatch.setenv("ROUND6_UNSET", "mirrored")

        stable = env.process_env_without_mirrors()

        assert stable["ROUND6_SET"] == "process"
        assert "ROUND6_UNSET" not in stable
        assert os.environ["ROUND6_SET"] == "mirrored"  # the process is not touched

    def test_a_name_no_worker_mirrored_reads_live(self, env, monkeypatch):
        env.note_env_mirror({"ROUND6_UNSET": None})
        monkeypatch.setenv("ROUND6_SET", "changed by the operator's own code")

        assert env.process_env_without_mirrors()["ROUND6_SET"] == "changed by the operator's own code"

    def test_two_workers_ending_out_of_order_keep_the_first_value(self, env, monkeypatch):
        """The second mirror saw the first one's value as "before"; that is
        not the process's."""
        env.note_env_mirror({"ROUND6_SET": "process"})
        monkeypatch.setenv("ROUND6_SET", "first worker")
        env.note_env_mirror({"ROUND6_SET": "first worker"})
        monkeypatch.setenv("ROUND6_SET", "second worker")

        env.forget_env_mirror(["ROUND6_SET"])  # the first one ends first
        assert env.process_env_without_mirrors()["ROUND6_SET"] == "process"

        env.forget_env_mirror(["ROUND6_SET"])
        assert env._env_mirrors == {}
        assert env.process_env_without_mirrors()["ROUND6_SET"] == "second worker"

    def test_forgetting_a_name_that_was_never_noted_is_harmless(self, env):
        env.forget_env_mirror(["ROUND6_NEVER"])

        assert env._env_mirrors == {}

    def test_the_worker_scope_notes_what_it_mirrors_and_forgets_it(
        self, env, profiles_on_disk
    ):
        (profiles_on_disk[OTHER_PROFILE] / ".env").write_text(
            "API_SERVER_KEY=play-key\nROUND6_SET=play\n", encoding="utf-8"
        )

        with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
            noted = dict(env._env_mirrors)
            stable = env.process_env_without_mirrors()

        assert {"ROUND6_SET", "API_SERVER_KEY", "HERMES_HOME"} <= set(noted)
        assert stable["ROUND6_SET"] == "process"
        assert env._env_mirrors == {}

    def test_a_secret_the_worker_scope_removes_reads_as_it_was(
        self, env, profiles_on_disk, monkeypatch
    ):
        """The scope takes provider credentials the profile does not set out
        of ``os.environ`` while it runs. They are the process's own."""
        home = profiles_on_disk[OTHER_PROFILE]
        secret = sorted(env._profile_secret_env_names(home))[0]
        monkeypatch.setenv(secret, "the launcher's")

        with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
            assert secret not in os.environ  # removed for the worker
            stable = env.process_env_without_mirrors()

        assert stable[secret] == "the launcher's"
        assert os.environ[secret] == "the launcher's"

    def test_the_worker_scope_forgets_when_its_body_raises(self, env, profiles_on_disk):
        with pytest.raises(RuntimeError):
            with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
                raise RuntimeError("the worker failed")

        assert env._env_mirrors == {}

    def test_a_streaming_turn_notes_its_mirror_and_forgets_it(self):
        """The local turn mirrors for as long as the agent runs. Read from the
        source, as ``tests/test_profile_terminal_env.py`` pins the mirror
        itself: the turn cannot be run here without an agent."""
        import inspect

        import api.streaming as streaming

        src = inspect.getsource(streaming)
        note = src.index("note_env_mirror(")
        mirror = src.index("os.environ.update(_safe_profile_runtime_env)")
        restore = src.index("for _key, _old_value in old_profile_env.items():")
        forget = src.index("forget_env_mirror(")
        assert note < mirror < restore < forget


class TestTheViewIsTakenUnderTheWritersLock:
    """Review of ``c2609745``. The view copies the bookkeeping and
    ``os.environ``; a worker that notes and writes between the two copies
    left its values in the view with nothing to undo them. The workers mirror
    and restore under ``api.streaming._ENV_LOCK``, so the view takes it too."""

    @pytest.fixture
    def env(self, profiles_on_disk, monkeypatch):
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "_env_mirrors", {})
        monkeypatch.setenv("ROUND6_SET", "process")
        (profiles_on_disk[OTHER_PROFILE] / ".env").write_text(
            "API_SERVER_KEY=play-key\nROUND6_SET=play\n", encoding="utf-8"
        )
        return profiles

    def test_a_worker_starting_between_the_two_copies_is_not_in_the_view(self, env, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def worker():
            started.set()
            with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
                release.wait(5)

        other = threading.Thread(target=worker)
        real = env._env_mirrors_lock

        class StartsAWorkerOnceTheBookkeepingIsCopied:
            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                result = real.__exit__(*exc)
                if not other.is_alive() and not started.is_set():
                    other.start()
                    started.wait(5)
                    # Long enough for an unblocked worker to note and write.
                    other.join(0.5)
                return result

        monkeypatch.setattr(env, "_env_mirrors_lock", StartsAWorkerOnceTheBookkeepingIsCopied())
        try:
            stable = env.process_env_without_mirrors()
        finally:
            monkeypatch.setattr(env, "_env_mirrors_lock", real)
            release.set()
            other.join(5)

        assert started.is_set()
        assert stable["ROUND6_SET"] == "process"
        assert stable.get("API_SERVER_KEY") != "play-key"

    def test_the_environment_is_copied_before_the_lock_is_let_go(self, env, monkeypatch):
        """Both copies inside it, not only the bookkeeping's."""
        import api.streaming as streaming

        real = streaming._ENV_LOCK

        class WritesAsItIsLetGo:
            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                os.environ["ROUND6_SET"] = "written as the lock was let go"
                return real.__exit__(*exc)

        monkeypatch.setattr(streaming, "_ENV_LOCK", WritesAsItIsLetGo())

        assert env.process_env_without_mirrors()["ROUND6_SET"] == "process"

    def test_the_view_waits_for_a_worker_that_is_mirroring(self, env):
        from api.streaming import _ENV_LOCK

        done = threading.Event()
        reader = threading.Thread(
            target=lambda: (env.process_env_without_mirrors(), done.set())
        )
        with _ENV_LOCK:
            reader.start()
            assert not done.wait(0.3)
        assert done.wait(5)
        reader.join(5)


class TestANameListedTwiceIsCountedOnce:
    """Review of ``c2609745``. A worker's list of names can hold one twice: a
    credential its profile sets is also one of the secrets the scope removes.
    Noted once (a dict) and forgotten twice, the first of two overlapping
    workers to end dropped the value the other still needed."""

    @pytest.fixture
    def env(self, monkeypatch):
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "_env_mirrors", {})
        monkeypatch.setenv("ROUND6_SET", "process")
        return profiles

    def test_forgetting_a_repeated_name_takes_one_count(self, env, monkeypatch):
        env.note_env_mirror({"ROUND6_SET": "process"})
        monkeypatch.setenv("ROUND6_SET", "first worker")
        env.note_env_mirror({"ROUND6_SET": "first worker"})

        env.forget_env_mirror(("ROUND6_SET", "ROUND6_SET"))  # one worker, the name twice

        assert env.process_env_without_mirrors()["ROUND6_SET"] == "process"
        env.forget_env_mirror(("ROUND6_SET", "ROUND6_SET"))
        assert env._env_mirrors == {}

    def test_two_scopes_of_a_profile_that_sets_one_of_the_secrets(
        self, env, profiles_on_disk, monkeypatch
    ):
        """The inner scope ends first; the outer one is still mirroring."""
        home = profiles_on_disk[OTHER_PROFILE]
        secret = sorted(env._profile_secret_env_names(home))[0]
        (home / ".env").write_text(f"API_SERVER_KEY=play-key\n{secret}=plays-own\n", encoding="utf-8")
        monkeypatch.setenv(secret, "the launcher's")

        with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
            with env.profile_env_for_background_worker(OTHER_PROFILE, scope_skill_modules=False):
                pass
            assert os.environ[secret] == "plays-own"  # the outer mirror is live
            stable = env.process_env_without_mirrors()

        assert stable[secret] == "the launcher's"
        assert env._env_mirrors == {}
