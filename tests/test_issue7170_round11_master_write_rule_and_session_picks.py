"""#7170 round-11: the two master interactions reported by droopanu (2026-11-06).

The PR was carried in production on top of master ``6124ed3c94`` (which
includes #8129 and #8253). Two defects showed up there; both are pinned here.

Finding 1 — ``set_reasoning_effort`` vs #8129's write rule.
    The PR's ``set_reasoning_effort`` loaded the env-EXPANDED config
    (``_load_yaml_config_file``) and saved it back. A config.yaml holding
    ``${VAR}`` references therefore got the RESOLVED secret written into it.
    Fix: load the RAW document (``_load_yaml_config_file_raw``) and save that.

Finding 2 — session picks after #8253.
    Master saves a writable session's pick through
    ``write_reasoning_effort(effort, profile_home)``, which only set or popped
    ``agent.reasoning_effort``. The per-model ``agent.reasoning_overrides``
    this PR writes were never updated on that path, so a stale override
    (e.g. ``gpt-5.4: low``) kept winning for later sessions of that model,
    and "Default" did not clear it. Fix: both writers share one
    ``_apply_reasoning_effort`` helper; ``write_reasoning_effort`` takes the
    model context; the route's session branch passes it.

Every test drives the REAL production function / route — no copy of the
production expression — so the assertions observe the same bytes the fix
changes.
"""

import io
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

SECRET = "sk-tes...-123"
REF = "${HERMES_TEST_SECRET}"


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class _FakeHandler:
    """Minimal request handler for routes.handle_post."""

    def __init__(self, body: dict | None = None):
        body_bytes = json.dumps(body or {}).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _post(path: str, body: dict) -> _FakeHandler:
    from api.routes import handle_post

    handler = _FakeHandler(body)
    handle_post(handler, urlparse(f"http://127.0.0.1{path}"))
    return handler


def _reset_config_caches():
    from api import config

    with config._cfg_lock:
        config._cfg_cache.clear()
        config.cfg = config._cfg_cache
        config._cfg_fingerprint = None
        config._cfg_mtime = 0.0
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    """A config.yaml whose secrets stay behind ``${VAR}`` references."""
    import api.config as config
    import api.routes as routes

    path = tmp_path / "config.yaml"
    path.write_text(
        "model:\n"
        "  default: gpt-4o\n"
        "  provider: openai\n"
        f"  api_key: {REF}\n"
        "providers:\n"
        "  openai:\n"
        f"    api_key: {REF}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_TEST_SECRET", SECRET)
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(routes, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    _reset_config_caches()
    yield path
    _reset_config_caches()
    monkeypatch.undo()
    config.reload_config()


@pytest.fixture
def profile_home(tmp_path):
    """A profile home whose config.yaml carries a per-model override."""
    home = tmp_path / "profile"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "reasoning_effort": "low",
                    "reasoning_overrides": {"gpt-5.4": "low"},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return home


# ── Finding 1: the reasoning write must not bake a resolved secret ──────────


def test_reasoning_effort_route_keeps_env_references(cfg_path):
    """A model-context /api/reasoning POST must not resolve ${VAR} on disk."""
    handler = _post("/api/reasoning", {"effort": "high", "model": "gpt-5.4"})
    assert handler.status == 200, handler.json_body()
    text = cfg_path.read_text(encoding="utf-8")
    assert SECRET not in text, "the reasoning route wrote the expanded secret"
    assert REF in text, "the reasoning route dropped the ${VAR} reference"
    # The selection itself must still land — this is not a no-op write.
    assert _raw(cfg_path)["agent"]["reasoning_effort"] == "high"


def test_set_reasoning_effort_keeps_env_references(cfg_path):
    """The same holds for a direct call on the writer itself."""
    from api import config

    config.set_reasoning_effort("high", model_id="gpt-5.4")
    text = cfg_path.read_text(encoding="utf-8")
    assert SECRET not in text, "set_reasoning_effort wrote the expanded secret"
    assert REF in text, "set_reasoning_effort dropped the ${VAR} reference"


def test_write_reasoning_effort_keeps_env_references(profile_home):
    """The session-path writer obeys the same write rule."""
    from api import config

    (profile_home / "config.yaml").write_text(
        f"model:\n  api_key: {REF}\n", encoding="utf-8"
    )
    config.write_reasoning_effort("high", profile_home)
    text = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert SECRET not in text, "write_reasoning_effort wrote the expanded secret"
    assert REF in text, "write_reasoning_effort dropped the ${VAR} reference"


# ── Finding 2: a session pick must not leave a stale per-model override ─────


def test_session_pick_supersedes_the_stale_override(profile_home):
    """A 'high' session pick must not lose to a stale 'gpt-5.4: low'."""
    from api import config

    config.write_reasoning_effort("high", profile_home, "gpt-5.4", "openai")

    overrides = _raw(profile_home / "config.yaml")["agent"]["reasoning_overrides"]
    assert overrides.get("gpt-5.4") == "high", (
        f"the stale override still wins: {overrides!r}"
    )


def test_default_clears_the_per_model_override(profile_home):
    """'Default' must remove the override, not just the global key."""
    from api import config

    config.write_reasoning_effort("", profile_home, "gpt-5.4", "openai")

    agent = _raw(profile_home / "config.yaml")["agent"]
    assert "gpt-5.4" not in agent.get("reasoning_overrides", {}), (
        f"'Default' did not clear the per-model override: {agent!r}"
    )
    assert "reasoning_effort" not in agent, (
        f"'Default' did not clear the global key: {agent!r}"
    )


def test_effective_effort_follows_the_session_pick(profile_home):
    """End-to-end: the effective effort for the model is the picked one."""
    from api import config

    config.write_reasoning_effort("high", profile_home, "gpt-5.4", "openai")

    snapshot = config.get_config_snapshot_for_profile_home(profile_home)
    effective = config.configured_reasoning_effort_for_model(
        snapshot, model_id="gpt-5.4", provider_id="openai"
    )
    assert effective == "high", (
        f"the stale per-model override still wins: effective effort is {effective!r}"
    )


def test_effective_effort_returns_to_the_provider_default(profile_home):
    """After 'Default' the provider default applies again for that model."""
    from api import config

    config.write_reasoning_effort("", profile_home, "gpt-5.4", "openai")

    snapshot = config.get_config_snapshot_for_profile_home(profile_home)
    effective = config.configured_reasoning_effort_for_model(
        snapshot, model_id="gpt-5.4", provider_id="openai"
    )
    assert effective == "", (
        f"'Default' did not restore the provider default: {effective!r}"
    )


def test_both_writers_share_one_agent_block_rule():
    """The two writers must not drift: one helper mutates the agent block."""
    from api import config

    source = Path(config.__file__).read_text(encoding="utf-8")
    for func in ("set_reasoning_effort", "write_reasoning_effort"):
        body = source.split(f"def {func}(", 1)[1]
        body = body.split("\ndef ", 1)[0]
        assert "_apply_reasoning_effort(" in body, (
            f"{func} no longer mutates the agent block through "
            f"_apply_reasoning_effort"
        )


def test_route_session_branch_passes_the_model_context():
    """The session branch must hand the model context to the writer."""
    import ast
    from pathlib import Path as _Path

    import api.routes as routes

    source = _Path(routes.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "write_reasoning_effort"
        ):
            calls.append(node)
    assert calls, "routes.py no longer calls write_reasoning_effort"
    # The session branch is the one that passes a profile home.
    session_calls = [
        c for c in calls if len(c.args) >= 2 and c.args[1] is not None
    ]
    assert session_calls, "the session branch no longer passes a profile home"
    for call in session_calls:
        names = [a.id for a in call.args if isinstance(a, ast.Name)]
        assert "model_id" in names and "provider_id" in names, (
            f"the session branch does not pass the model context: "
            f"positional args={names}"
        )
