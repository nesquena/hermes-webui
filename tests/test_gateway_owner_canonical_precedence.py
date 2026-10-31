"""Owner key precedence through real dotenv scopes and both dispatch lanes."""
import pytest

from tests.test_gateway_runs_model_options_boundary import (
    _capture_gateway_wire_body,
    _restored_process_env,
)


@pytest.mark.parametrize("owner", ["", "work"])
@pytest.mark.parametrize("use_runs", [False, True])
@pytest.mark.parametrize("shape", ["canonical", "alias_with_launcher", "alias_derived"])
def test_owner_canonical_collision_and_rotation(monkeypatch, tmp_path, owner, use_runs, shape):
    from api import gateway_chat, profiles

    home = tmp_path / "profiles" / owner if owner else tmp_path
    home.mkdir(parents=True, exist_ok=True)
    url = "http://owner-gw.test"
    canonical = "HERMES_WEBUI_GATEWAY_API_KEY"
    launcher = {
        "HERMES_WEBUI_GATEWAY_BASE_URL": url,
        "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1" if use_runs else "0",
    }
    if shape != "alias_derived":
        launcher[canonical] = "launcher-L"
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(home))
    monkeypatch.setattr(profiles, "_INITIAL_GATEWAY_DEPLOYMENT_ENV", launcher)
    monkeypatch.setattr(profiles, "_INITIAL_PROCESS_ENV", dict(launcher))
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(profiles, "_PROCESS_OWNER_DOTENV", {})
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(gateway_chat, "_GATEWAY_DEPLOYMENT_ENV", dict(launcher))

    with _restored_process_env():
        import os
        os.environ.pop(canonical, None)
        os.environ.pop("API_SERVER_KEY", None)
        os.environ.update(launcher)
        for value in ("owner-O", "owner-O2"):
            spelling = canonical if shape == "canonical" else "API_SERVER_KEY"
            (home / ".env").write_text(f"{spelling}={value}\n", encoding="utf-8")
            profiles._reload_dotenv(home)
            expected = "launcher-L" if shape == "alias_with_launcher" else value
            # Simulate the stale startup-normalized alias cache without changing
            # the explicit launcher provenance or the fresh on-disk owner input.
            if shape == "alias_derived":
                gateway_chat._GATEWAY_DEPLOYMENT_ENV[canonical] = "stale-derived"
            captured = {}
            _capture_gateway_wire_body(
                None, use_runs_api=use_runs, session_profile=owner or None,
                real_profile_scope=True, gateway_env=launcher, captured=captured,
            )
            path = "/v1/runs" if use_runs else "/v1/chat/completions"
            assert captured["url"] == url + path
            assert captured["headers"]["Authorization"] == f"Bearer {expected}"
            assert gateway_chat._gateway_endpoint_for_profile(owner) == (url, expected)
