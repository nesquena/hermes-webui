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


def test_mcp_server_url_reference_survives_surrounding_whitespace_resave(cfg_path):
    """An unchanged resave that differs only by surrounding whitespace keeps the reference on disk."""
    import api.routes as routes

    handler = _FakeHandler()
    routes._handle_mcp_server_update(handler, "bus", {"url": f"  {MCP_URL}  \n"})
    assert handler.status == 200, handler.json_body()
    assert _raw(cfg_path)["mcp_servers"]["bus"]["url"] == RAW_MCP_URL
    _assert_no_secret(cfg_path, "MCP server update with surrounding whitespace")


def test_mcp_server_url_slash_edits_in_both_directions_are_written_as_given(
    cfg_path, monkeypatch
):
    """Path and query slash edits in both directions are persisted exactly as submitted (#8740 CR r4)."""
    import api.routes as routes
    from api import config

    cases = [
        # Direction A: slash added
        ("bus", MCP_URL, MCP_URL + "/", "query slash added"),
        ("path_add", "https://host.example.test/mcp", "https://host.example.test/mcp/", "path slash added"),
        # Direction B: slash removed
        ("query_rem", "https://host.example.test/mcp?token=abc/", "https://host.example.test/mcp?token=abc", "query slash removed"),
        ("path_rem", "https://host.example.test/mcp/", "https://host.example.test/mcp", "path slash removed"),
    ]

    for srv_name, stored_val, submitted_url, desc in cases:
        env_var = f"HERMES_TEST_MCP_{srv_name.upper()}"
        monkeypatch.setenv(env_var, stored_val)
        _seed(
            cfg_path,
            lambda raw, name=srv_name, var=env_var: raw["mcp_servers"].__setitem__(
                name, {"url": f"${{{var}}}"}
            ),
        )

        handler = _FakeHandler()
        routes._handle_mcp_server_update(handler, srv_name, {"url": submitted_url})
        assert handler.status == 200, f"{desc}: {handler.json_body()}"

        written = _raw(cfg_path)["mcp_servers"][srv_name]["url"]
        assert written == submitted_url, f"slash edit lost for {desc}: got {written!r}"
        assert config.get_config()["mcp_servers"][srv_name]["url"] == submitted_url, (
            f"resolved URL mismatch for {desc}"
        )


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


# ── Extra body list row matching (#8740 CR) ──────────────────────────────────


def test_extra_body_reordered_rows_follow_their_identity(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "alpha", "token": "${HERMES_TEST_A}"},
            {"name": "beta", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    reordered = {
        "items": [
            {"name": "beta", "token": "val-beta-456"},
            {"name": "alpha", "token": "val-alpha-123"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert written[0] == {"name": "beta", "token": "${HERMES_TEST_B}"}
    assert written[1] == {"name": "alpha", "token": "${HERMES_TEST_A}"}
    text = cfg_path.read_text(encoding="utf-8")
    assert "val-alpha-123" not in text
    assert "val-beta-456" not in text
    _assert_no_secret(cfg_path, "POST /api/model/set reordered extra_body list")


def test_auxiliary_extra_body_reordered_rows_follow_their_identity(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "alpha", "token": "${HERMES_TEST_A}"},
            {"name": "beta", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    reordered = {
        "items": [
            {"name": "beta", "token": "val-beta-456"},
            {"name": "alpha", "token": "val-alpha-123"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]["items"]
    assert written[0] == {"name": "beta", "token": "${HERMES_TEST_B}"}
    assert written[1] == {"name": "alpha", "token": "${HERMES_TEST_A}"}
    text = cfg_path.read_text(encoding="utf-8")
    assert "val-alpha-123" not in text
    assert "val-beta-456" not in text
    _assert_no_secret(cfg_path, "POST /api/model/set auxiliary reordered extra_body list")


def test_extra_body_reordered_anonymous_rows_reject_without_writing(cfg_path, monkeypatch):
    synth_common = "synth-common-val-999"
    monkeypatch.setenv("HERMES_TEST_A", synth_common)
    monkeypatch.setenv("HERMES_TEST_B", synth_common)

    raw_eb = {
        "temperature": 1,
        "items": [
            {"token": "${HERMES_TEST_A}"},
            {"token": "${HERMES_TEST_B}"},
        ],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    before_bytes = cfg_path.read_bytes()

    reordered = {
        "temperature": 2,
        "items": [
            {"token": synth_common},
            {"token": synth_common},
        ],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 400, handler.json_body()
    err = handler.json_body().get("error", "")
    assert err.startswith("advanced model options: extra_body list rows")
    assert cfg_path.read_bytes() == before_bytes


def test_extra_body_appended_row_keeps_surviving_references(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "alpha", "token": "${HERMES_TEST_A}"},
            {"name": "beta", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    appended = {
        "items": [
            {"name": "alpha", "token": "val-alpha-123"},
            {"name": "beta", "token": "val-beta-456"},
            {"name": "gamma", "token": "literal-xyz"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": appended},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert len(written) == 3
    assert written[0] == {"name": "alpha", "token": "${HERMES_TEST_A}"}
    assert written[1] == {"name": "beta", "token": "${HERMES_TEST_B}"}
    assert written[2] == {"name": "gamma", "token": "literal-xyz"}
    _assert_no_secret(cfg_path, "POST /api/model/set append extra_body list")


def test_extra_body_removed_row_keeps_surviving_references(cfg_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "alpha", "token": "${HERMES_TEST_A}"},
            {"name": "beta", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    removed = {
        "items": [
            {"name": "alpha", "token": "val-alpha-123"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": removed},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert len(written) == 1
    assert written[0] == {"name": "alpha", "token": "${HERMES_TEST_A}"}
    _assert_no_secret(cfg_path, "POST /api/model/set remove extra_body list")


def test_extra_body_list_references_survive_environment_divergence(cfg_path, monkeypatch):
    from api import config

    raw_eb = {
        "items": [
            {"name": "alpha", "token": "${HERMES_TEST_A}"},
            {"name": "beta", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    monkeypatch.setenv("HERMES_TEST_A", "diverged-alpha-789")
    monkeypatch.setenv("HERMES_TEST_B", "diverged-beta-012")

    shown = config._load_yaml_config_file(cfg_path)["model"]["extra_body"]
    assert shown == {
        "items": [
            {"name": "alpha", "token": "diverged-alpha-789"},
            {"name": "beta", "token": "diverged-beta-012"},
        ]
    }

    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": shown},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert written[0] == {"name": "alpha", "token": "${HERMES_TEST_A}"}
    assert written[1] == {"name": "beta", "token": "${HERMES_TEST_B}"}
    _assert_no_secret(cfg_path, "POST /api/model/set divergence extra_body list")


def test_extra_body_row_with_only_references_keeps_siblings_when_one_leaf_is_edited(
    cfg_path, monkeypatch
):
    """A row edited in one leaf still keeps its other references (#8740 CR)."""
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", {
        "rows": [{"a": "${HERMES_TEST_A}", "b": "${HERMES_TEST_B}"}]
    }))

    edited = {"rows": [{"a": "typed-by-the-user", "b": "val-beta-456"}]}
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

    written = _raw(cfg_path)["model"]["extra_body"]["rows"]
    assert written == [{"a": "typed-by-the-user", "b": "${HERMES_TEST_B}"}], (
        f"editing one leaf of a reference-only row baked its siblings: {written!r}"
    )
    _assert_no_secret(cfg_path, "POST /api/model/set edited reference-only row")


def test_extra_body_list_without_references_is_written_as_given(cfg_path):
    raw_eb = {
        "items": [
            {"name": "alpha", "token": "plain-alpha"},
            {"name": "beta", "token": "plain-beta"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    reordered = {
        "items": [
            {"name": "beta", "token": "plain-beta"},
            {"name": "alpha", "token": "plain-alpha"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert written[0] == {"name": "beta", "token": "plain-beta"}
    assert written[1] == {"name": "alpha", "token": "plain-alpha"}


# ── Shapes 1–3 and reference-valued identity (#8740 CR r3) ───────────────────


def test_extra_body_shape1_keeps_references_when_outer_temperature_changes(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("N", "alpha")
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "temperature": 1,
        "items": [{"name": "${N}", "token": "${TOKEN}"}],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    submitted = {
        "temperature": 2,
        "items": [{"name": "alpha", "token": "secret"}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]
    assert written == {
        "temperature": 2,
        "items": [{"name": "${N}", "token": "${TOKEN}"}],
    }
    text = cfg_path.read_text(encoding="utf-8")
    assert "alpha" not in text
    assert "secret" not in text


def test_auxiliary_extra_body_shape1_keeps_references_when_outer_temperature_changes(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("N", "alpha")
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "temperature": 1,
        "items": [{"name": "${N}", "token": "${TOKEN}"}],
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    submitted = {
        "temperature": 2,
        "items": [{"name": "alpha", "token": "secret"}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]
    assert written == {
        "temperature": 2,
        "items": [{"name": "${N}", "token": "${TOKEN}"}],
    }
    text = cfg_path.read_text(encoding="utf-8")
    assert "alpha" not in text
    assert "secret" not in text


def test_extra_body_shape2_keeps_reference_when_multiple_non_ref_leaves_edited(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "rows": [{"token": "${TOKEN}", "temperature": 1, "depth": 1}],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    submitted = {
        "rows": [{"token": "secret", "temperature": 2, "depth": 2}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["rows"]
    assert written == [{"token": "${TOKEN}", "temperature": 2, "depth": 2}]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret" not in text


def test_auxiliary_extra_body_shape2_keeps_reference_when_multiple_non_ref_leaves_edited(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "rows": [{"token": "${TOKEN}", "temperature": 1, "depth": 1}],
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    submitted = {
        "rows": [{"token": "secret", "temperature": 2, "depth": 2}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]["rows"]
    assert written == [{"token": "${TOKEN}", "temperature": 2, "depth": 2}]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret" not in text


def test_extra_body_shape3_keeps_reference_when_non_ref_key_removed(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "rows": [{"token": "${TOKEN}", "temperature": 1}],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    submitted = {
        "rows": [{"token": "secret"}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["rows"]
    assert written == [{"token": "${TOKEN}"}]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret" not in text


def test_auxiliary_extra_body_shape3_keeps_reference_when_non_ref_key_removed(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("TOKEN", "secret")

    raw_eb = {
        "rows": [{"token": "${TOKEN}", "temperature": 1}],
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    submitted = {
        "rows": [{"token": "secret"}],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]["rows"]
    assert written == [{"token": "${TOKEN}"}]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret" not in text


def test_extra_body_residue_keeps_untouched_reference_when_reference_and_numerics_edited(
    cfg_path, monkeypatch
):
    """Residue match: editing one reference leaf and two numeric leaves keeps the untouched reference (#8740 CR r4)."""
    monkeypatch.setenv("REF_EDITED", "secret-edited-111")
    monkeypatch.setenv("REF_UNTOUCHED", "secret-untouched-222")

    raw_eb = {
        "rows": [
            {
                "token": "${REF_EDITED}",
                "other": "${REF_UNTOUCHED}",
                "temperature": 1,
                "depth": 1,
            }
        ],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    submitted = {
        "rows": [
            {
                "token": "brand-new-literal-token",
                "other": "secret-untouched-222",
                "temperature": 2,
                "depth": 2,
            }
        ],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["rows"]
    assert written == [
        {
            "token": "brand-new-literal-token",
            "other": "${REF_UNTOUCHED}",
            "temperature": 2,
            "depth": 2,
        }
    ]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret-untouched-222" not in text
    assert "secret-edited-111" not in text
    assert "brand-new-literal-token" in text


def test_auxiliary_extra_body_residue_keeps_untouched_reference_when_reference_and_numerics_edited(
    cfg_path, monkeypatch
):
    """Auxiliary residue match: editing one reference and two numeric leaves keeps the untouched reference (#8740 CR r4)."""
    monkeypatch.setenv("REF_EDITED", "secret-edited-111")
    monkeypatch.setenv("REF_UNTOUCHED", "secret-untouched-222")

    raw_eb = {
        "rows": [
            {
                "token": "${REF_EDITED}",
                "other": "${REF_UNTOUCHED}",
                "temperature": 1,
                "depth": 1,
            }
        ],
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    submitted = {
        "rows": [
            {
                "token": "brand-new-literal-token",
                "other": "secret-untouched-222",
                "temperature": 2,
                "depth": 2,
            }
        ],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]["rows"]
    assert written == [
        {
            "token": "brand-new-literal-token",
            "other": "${REF_UNTOUCHED}",
            "temperature": 2,
            "depth": 2,
        }
    ]
    text = cfg_path.read_text(encoding="utf-8")
    assert "secret-untouched-222" not in text
    assert "secret-edited-111" not in text
    assert "brand-new-literal-token" in text


def test_extra_body_residue_ambiguous_candidates_reject_without_writing(
    cfg_path, monkeypatch
):
    """When multiple raw rows qualify as residue candidates, write is rejected without modifying disk (#8740 CR r4)."""
    monkeypatch.setenv("REF_A", "secret-a")
    monkeypatch.setenv("REF_B", "secret-b")
    monkeypatch.setenv("REF_COMMON", "shared-untouched-secret")

    raw_eb = {
        "rows": [
            {"token": "${REF_A}", "key": "${REF_COMMON}", "temperature": 1, "depth": 1},
            {"token": "${REF_B}", "key": "${REF_COMMON}", "temperature": 3, "depth": 3},
        ],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    before_bytes = cfg_path.read_bytes()

    submitted = {
        "rows": [
            {
                "token": "brand-new-literal-token",
                "key": "shared-untouched-secret",
                "temperature": 2,
                "depth": 2,
            }
        ],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": submitted},
        },
    )
    assert handler.status == 400, handler.json_body()
    err = handler.json_body().get("error", "")
    assert err.startswith("advanced model options: extra_body list rows")
    assert cfg_path.read_bytes() == before_bytes


def test_extra_body_reordered_rows_follow_reference_valued_identity(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_NAME_A", "alpha")
    monkeypatch.setenv("HERMES_TEST_NAME_B", "beta")
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "${HERMES_TEST_NAME_A}", "token": "${HERMES_TEST_A}"},
            {"name": "${HERMES_TEST_NAME_B}", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    reordered = {
        "items": [
            {"name": "beta", "token": "val-beta-456"},
            {"name": "alpha", "token": "val-alpha-123"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["model"]["extra_body"]["items"]
    assert written[0] == {"name": "${HERMES_TEST_NAME_B}", "token": "${HERMES_TEST_B}"}
    assert written[1] == {"name": "${HERMES_TEST_NAME_A}", "token": "${HERMES_TEST_A}"}
    text = cfg_path.read_text(encoding="utf-8")
    assert "val-alpha-123" not in text
    assert "val-beta-456" not in text
    _assert_no_secret(cfg_path, "POST /api/model/set reordered extra_body list ref identity")


def test_auxiliary_extra_body_reordered_rows_follow_reference_valued_identity(
    cfg_path, monkeypatch
):
    monkeypatch.setenv("HERMES_TEST_NAME_A", "alpha")
    monkeypatch.setenv("HERMES_TEST_NAME_B", "beta")
    monkeypatch.setenv("HERMES_TEST_A", "val-alpha-123")
    monkeypatch.setenv("HERMES_TEST_B", "val-beta-456")

    raw_eb = {
        "items": [
            {"name": "${HERMES_TEST_NAME_A}", "token": "${HERMES_TEST_A}"},
            {"name": "${HERMES_TEST_NAME_B}", "token": "${HERMES_TEST_B}"},
        ]
    }
    _seed(cfg_path, lambda raw: raw.__setitem__("auxiliary", {
        "vision": {"provider": "openai", "model": "gpt-4o", "extra_body": raw_eb}
    }))

    reordered = {
        "items": [
            {"name": "beta", "token": "val-beta-456"},
            {"name": "alpha", "token": "val-alpha-123"},
        ]
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "auxiliary",
            "task": "vision",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 200, handler.json_body()

    written = _raw(cfg_path)["auxiliary"]["vision"]["extra_body"]["items"]
    assert written[0] == {"name": "${HERMES_TEST_NAME_B}", "token": "${HERMES_TEST_B}"}
    assert written[1] == {"name": "${HERMES_TEST_NAME_A}", "token": "${HERMES_TEST_A}"}
    text = cfg_path.read_text(encoding="utf-8")
    assert "val-alpha-123" not in text
    assert "val-beta-456" not in text
    _assert_no_secret(cfg_path, "POST /api/model/set aux reordered extra_body list ref identity")


def test_extra_body_colliding_reference_valued_identities_reject_without_writing(
    cfg_path, monkeypatch
):
    synth_common = "synth-common-val-999"
    monkeypatch.setenv("HERMES_TEST_A", synth_common)
    monkeypatch.setenv("HERMES_TEST_B", synth_common)

    raw_eb = {
        "temperature": 1,
        "items": [
            {"name": "${HERMES_TEST_A}"},
            {"name": "${HERMES_TEST_B}"},
        ],
    }
    _seed(cfg_path, lambda raw: raw["model"].__setitem__("extra_body", raw_eb))

    before_bytes = cfg_path.read_bytes()

    reordered = {
        "temperature": 2,
        "items": [
            {"name": synth_common},
            {"name": synth_common},
        ],
    }
    handler = _post(
        "/api/model/set",
        {
            "scope": "main",
            "provider": "openai",
            "model": "gpt-4o",
            "advanced": {"extra_body": reordered},
        },
    )
    assert handler.status == 400, handler.json_body()
    err = handler.json_body().get("error", "")
    assert err.startswith("advanced model options: extra_body list rows")
    assert cfg_path.read_bytes() == before_bytes


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

