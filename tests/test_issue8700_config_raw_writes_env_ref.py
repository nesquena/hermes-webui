"""#8700: the three config writers #8129 left out must keep a ``${VAR}``
reference on disk instead of the value it resolves to.

#8129 (RM-20261010-8129 option A) converted the config writers to save the RAW
YAML so a reference survives a re-save. Three paths were left for a separate
issue because they need more than ``_preserve_env_ref`` on the field being
written: the value they persist is DERIVED (a provider record's URL, a
displayed MCP URL, an edited JSON object), so the reference to keep lives
somewhere other than the field itself.

Every case here drives the real writer (public API function or HTTP route)
against a temp config.yaml and then reads the YAML back FROM DISK, asserting
the reference is still there and the resolved secret never reached the file.

The three paths, one section each:
  1. ``set_hermes_default_model`` derived endpoint (api/config.py)
  2. MCP server re-save (api/routes.py ``_handle_mcp_server_update``)
  3. ``extra_body`` edited in place (api/config.py ``_apply_advanced_model_options``)
"""

import io
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

SECRET = "sk-tes...-123"
MCP_TOKEN = "mcp-test-SYNTHETIC-789"

RAW_MCP_URL = "${HERMES_TEST_MCP_URL}"
MCP_URL = f"https://mcp.example.test/sse?key={MCP_TOKEN}"

SEED = f"""\
model:
  default: gpt-4o
  provider: openai
  api_key: ${{HERMES_TEST_SECRET}}
providers:
  openai:
    api_key: ${{HERMES_TEST_SECRET}}
mcp_servers:
  bus:
    url: {RAW_MCP_URL}
    headers:
      X-Alt: ${{env:HERMES_TEST_SECRET}}
  local:
    command: synthetic-mcp
    args:
      - --token
      - ${{HERMES_TEST_TOKEN}}
display:
  show_reasoning: false
"""


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _assert_no_secret(path: Path, writer: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text, f"{writer} wrote the expanded secret into config.yaml"
    assert MCP_TOKEN not in text, f"{writer} wrote the expanded MCP token into config.yaml"


class _FakeHandler:
    """Minimal request handler for routes.handle_post / direct handlers."""

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
    import api.config as config
    import api.routes as routes

    path = tmp_path / "config.yaml"
    path.write_text(SEED, encoding="utf-8")
    monkeypatch.setenv("HERMES_TEST_SECRET", SECRET)
    monkeypatch.setenv("HERMES_TEST_TOKEN", "tok-test-SYNTHETIC-456")
    monkeypatch.setenv("HERMES_TEST_MCP_URL", MCP_URL)

    def _test_config_path():
        return path

    monkeypatch.setattr(config, "_get_config_path", _test_config_path)
    monkeypatch.setattr(routes, "_get_config_path", _test_config_path)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    _reset_config_caches()
    _reload_config()
    yield path
    # Leave the module as later tests expect it (see test_issue8032 fixture).
    _reset_config_caches()
    monkeypatch.undo()
    config.reload_config()


def _reload_config() -> None:
    """Rebuild the runtime config from the temp file, as every request would.

    ``set_hermes_default_model`` resolves the provider through the module-level
    ``cfg`` snapshot, so the fixture must repopulate it after each seed.
    """
    from api import config

    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()
    config.reload_config()


def _seed(cfg_path: Path, mutate) -> None:
    raw = _raw(cfg_path)
    mutate(raw)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _reset_config_caches()
    _reload_config()


# ── 2. MCP server re-save (api/routes.py _handle_mcp_server_update) ───────────
# The MCP list shows the EXPANDED url, so an unchanged re-save submits the
# resolved string. Writing it back replaced ``url: ${VAR}`` with its expansion.


def test_mcp_server_url_reference_survives_unchanged_resave(cfg_path):
    import api.routes as routes
    from api import config

    # What the panel is served for this server (the expanded read).
    shown = config._load_yaml_config_file(cfg_path)["mcp_servers"]["bus"]["url"]
    assert shown == MCP_URL

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "bus", {"url": shown, "timeout": 45})
    assert handler.status == 200, handler.json_body()

    raw = _raw(cfg_path)
    assert raw["mcp_servers"]["bus"]["url"] == RAW_MCP_URL, (
        "re-saving an unchanged MCP server baked its resolved url into config.yaml: "
        f"{raw['mcp_servers']['bus']['url']!r}"
    )
    assert raw["mcp_servers"]["bus"]["timeout"] == 45
    _assert_no_secret(cfg_path, "MCP server update")


def test_mcp_server_url_reference_survives_trailing_slash_normalisation(cfg_path):
    """The writer stores url stripped; a reference must compare the same way."""
    import api.routes as routes

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "bus", {"url": MCP_URL + "/  "})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["bus"]["url"] == RAW_MCP_URL


def test_mcp_server_url_edit_is_written_as_given(cfg_path):
    import api.routes as routes

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "bus", {"url": "https://other.example.test/sse"})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["bus"]["url"] == "https://other.example.test/sse"


def test_mcp_server_without_reference_url_is_written_unchanged(cfg_path):
    import api.routes as routes

    _seed(cfg_path, lambda raw: raw["mcp_servers"]["bus"].__setitem__("url", "https://plain.example.test/sse"))

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "bus", {"url": "https://plain.example.test/sse"})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["bus"]["url"] == "https://plain.example.test/sse"


# ── 3. extra_body edited in place (api/config.py
#    _apply_advanced_model_options) ────────────────────────────────────────────
# The advanced dialog posts the DISPLAYED (expanded) extra_body. #8129 kept the
# raw block only when the whole object came back equal, so editing one sibling
# value re-wrote every other leaf as its expansion.

TOKEN_ENV = "tok-test-SYNTHETIC-456"
RAW_EXTRA_BODY = {
    "token": "${HERMES_TEST_SECRET}",
    "temperature": 1,
    "options": {"key": "${HERMES_TEST_TOKEN}", "depth": 1},
}


def _displayed_extra_body(*, temperature):
    """The expanded object the advanced dialog holds, with one edited leaf."""
    return {
        "token": SECRET,
        "temperature": temperature,
        "options": {"key": TOKEN_ENV, "depth": 1},
    }


def test_main_extra_body_edit_keeps_the_untouched_reference_leaves(cfg_path):
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", dict(RAW_EXTRA_BODY)))

    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": _displayed_extra_body(temperature=2)},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]
    assert written["temperature"] == 2
    assert written["token"] == "${HERMES_TEST_SECRET}", (
        "editing a sibling extra_body value baked the resolved secret into config.yaml: "
        f"{written!r}"
    )
    assert written["options"] == {"key": "${HERMES_TEST_TOKEN}", "depth": 1}, (
        f"a nested reference leaf was expanded on an unrelated edit: {written!r}"
    )
    _assert_no_secret(cfg_path, "POST /api/model/set main advanced (edited extra_body)")


def test_auxiliary_extra_body_edit_keeps_the_untouched_reference_leaves(cfg_path):
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": dict(RAW_EXTRA_BODY)}
    }))

    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": _displayed_extra_body(temperature=2)},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]
    assert written["temperature"] == 2
    assert written["token"] == "${HERMES_TEST_SECRET}", f"auxiliary edit expanded it: {written!r}"
    assert written["options"]["key"] == "${HERMES_TEST_TOKEN}"
    _assert_no_secret(cfg_path, "POST /api/model/set auxiliary advanced (edited extra_body)")


def test_extra_body_edited_reference_leaf_is_written_as_given(cfg_path):
    """A leaf the user really changed is persisted, not masked by the old ref."""
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", dict(RAW_EXTRA_BODY)))

    edited = _displayed_extra_body(temperature=2)
    edited["token"] = "sk-user-typed-LEAF"
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": edited},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]
    assert written["token"] == "sk-user-typed-LEAF"
    assert written["options"]["key"] == "${HERMES_TEST_TOKEN}"


def test_extra_body_leaf_removed_by_the_edit_is_removed(cfg_path):
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", dict(RAW_EXTRA_BODY)))

    edited = _displayed_extra_body(temperature=2)
    edited.pop("options")
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": edited},
        },
    )
    assert handler.status == 200, handler.json_body()
    written = _raw(cfg_path)["model"]["extra_body"]
    assert "options" not in written
    assert written["token"] == "${HERMES_TEST_SECRET}"


def test_extra_body_without_references_is_written_unchanged(cfg_path):
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", {"temperature": 1, "top_p": 0.9}))

    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": {"temperature": 2, "top_p": 0.9}},
        },
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["model"]["extra_body"] == {"temperature": 2, "top_p": 0.9}


# ── 1. Derived endpoint in set_hermes_default_model (api/config.py) ───────────
# Choosing a named custom provider as the default model writes the endpoint that
# the provider RECORD resolves to. When that record holds ``base_url: ${URL}``
# the derived value arrives expanded, and #8129's ``_preserve_env_ref`` on
# ``model.base_url`` could only keep a reference already stored THERE — a
# reference in the record was never recovered, so the expansion (token
# included) landed on disk.

RAW_PROVIDER_URL = "${HERMES_TEST_NAMED_URL}"
PROVIDER_URL = f"https://llm.example.test/v1?key={SECRET}"


def _seed_named_provider(cfg_path: Path, *, raw_url=RAW_PROVIDER_URL) -> None:
    def mutate(raw):
        raw["custom_providers"] = [
            {"name": "Secret Endpoint", "model": "synthetic-model", "base_url": raw_url}
        ]

    _seed(cfg_path, mutate)


def test_default_model_derived_endpoint_keeps_the_provider_reference(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_NAMED_URL", PROVIDER_URL)
    _seed_named_provider(cfg_path)

    handler = _post(
        "/api/default-model", {"model": "synthetic-model", "provider": "custom:secret-endpoint"}
    )
    assert handler.status == 200, handler.json_body()

    model = _raw(cfg_path)["model"]
    assert model["default"] == "synthetic-model"
    assert model["provider"] == "custom:secret-endpoint"
    assert model["base_url"] == RAW_PROVIDER_URL, (
        "the default-model save baked the provider's resolved endpoint into config.yaml: "
        f"{model.get('base_url')!r}"
    )
    assert SECRET not in cfg_path.read_text(encoding="utf-8")


def test_default_model_derived_endpoint_keeps_the_provider_reference_without_provider(
    cfg_path, monkeypatch
):
    """New Chat / picker saves that send no provider still resolve the record."""
    monkeypatch.setenv("HERMES_TEST_NAMED_URL", PROVIDER_URL)
    _seed_named_provider(cfg_path)

    handler = _post("/api/default-model", {"model": "synthetic-model"})
    assert handler.status == 200, handler.json_body()

    model = _raw(cfg_path)["model"]
    assert str(model.get("provider") or "").startswith("custom:")
    assert model["base_url"] == RAW_PROVIDER_URL, model
    assert SECRET not in cfg_path.read_text(encoding="utf-8")


def test_default_model_keeps_its_own_reference_when_it_already_resolves_there(
    cfg_path, monkeypatch
):
    """#8032 behaviour that must not regress: model.base_url's own ${VAR} wins."""
    monkeypatch.setenv("HERMES_TEST_NAMED_URL", PROVIDER_URL)
    monkeypatch.setenv("HERMES_TEST_MODEL_URL", PROVIDER_URL)

    def mutate(raw):
        raw["custom_providers"] = [
            {"name": "Secret Endpoint", "model": "synthetic-model", "base_url": RAW_PROVIDER_URL}
        ]
        raw["model"]["base_url"] = "${HERMES_TEST_MODEL_URL}"

    _seed(cfg_path, mutate)

    handler = _post(
        "/api/default-model", {"model": "synthetic-model", "provider": "custom:secret-endpoint"}
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["model"]["base_url"] == "${HERMES_TEST_MODEL_URL}"
    assert SECRET not in cfg_path.read_text(encoding="utf-8")


def test_default_model_endpoint_edit_is_written_as_given(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_NAMED_URL", PROVIDER_URL)
    _seed_named_provider(cfg_path)

    handler = _post(
        "/api/default-model",
        {
            "model": "synthetic-model",
            "provider": "custom:secret-endpoint",
            "advanced": {"base_url": "https://typed.example.test/v1"},
        },
    )
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["model"]["base_url"] == "https://typed.example.test/v1"


def test_default_model_provider_without_reference_writes_the_plain_endpoint(cfg_path):
    _seed_named_provider(cfg_path, raw_url="https://plain.example.test/v1")

    handler = _post(
        "/api/default-model", {"model": "synthetic-model", "provider": "custom:secret-endpoint"}
    )
    assert handler.status == 200, handler.json_body()
    model = _raw(cfg_path)["model"]
    assert model["base_url"] == "https://plain.example.test/v1"

