"""Safety tests for the WebUI Settings-managed arbitrary .env key editor.

These cover:

* valid key write+read
* invalid keys rejected (lowercase, spaces, empty, reserved names)
* special chars round-trip through the atomic writer (spaces, ``=``,
  ``#``, single + double quotes, backslash) without .env injection
* duplicate key updates in place — one entry per key, no duplicates
* atomic-on-failure: an exception during write does not corrupt the
  pre-existing .env file (we patch ``_write_env_file`` to raise mid-cycle)

Tests use ``tmp_path`` for the .env file and never touch the real
``~/.hermes/.env``. They patch ``api.env_settings._get_hermes_home`` to
return ``tmp_path`` so the module-under-test resolves the isolated file.
"""

from __future__ import annotations

import io
import json
import os
import textwrap
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self, body_bytes: bytes = b"") -> None:
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self  # type: ignore[assignment]
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        # First-password setup is gated to loopback clients when auth is
        # disabled. These tests run with auth disabled, so model loopback
        # explicitly to mirror test_auth_settings_safety.py.
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.sent_headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def write(self, data) -> None:  # type: ignore[no-untyped-def]
        self.body.extend(data)

    def json_body(self) -> dict:
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture(autouse=True)
def _isolate_env_settings_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point ``api.env_settings`` at a per-test tmp directory.

    Keeps the suite from ever reading or writing the operator's real
    ``~/.hermes/.env``. Auth is also forced off because the real
    bootstrap may have set a password in CI.
    """
    import api.auth as auth
    import api.env_settings as env_settings

    monkeypatch.setattr(env_settings, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    os.environ.pop("HERMES_WEBUI_PASSWORD", None)
    yield
    # Defensive: scrub any keys this test wrote into process env via
    # _write_env_file's os.environ mutation.
    for key in (
        "TEST_ENV_VALID",
        "TEST_ENV_SPECIAL",
        "TEST_ENV_DUPLICATE",
        "TEST_ENV_DELETE",
        "TEST_ENV_ATOMIC",
        "TEST_ENV_QUOTED",
    ):
        os.environ.pop(key, None)
    os.environ.pop("HERMES_WEBUI_PASSWORD", None)


# ── Pure-module tests (no HTTP) ────────────────────────────────────────


def test_validate_key_accepts_well_formed_names() -> None:
    from api.env_settings import validate_key

    assert validate_key("FOO") == "FOO"
    assert validate_key("_FOO") == "_FOO"
    assert validate_key("A_1_B_2") == "A_1_B_2"
    assert validate_key("X" * 128) == "X" * 128


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "1FOO",          # digit prefix
        "foo",           # lowercase
        "FOO BAR",       # space
        "FOO-BAR",       # dash
        "FOO.BAR",       # dot
        "FOO$BAR",       # shell special
        "X" * 129,       # too long
    ],
)
def test_validate_key_rejects_malformed(bad_key: str) -> None:
    from api.env_settings import validate_key

    with pytest.raises(ValueError):
        validate_key(bad_key)


@pytest.mark.parametrize(
    "reserved",
    [
        "HERMES_HOME",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_WEBUI_PORT",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "PYTHONPATH",
        "SHELL",
    ],
)
def test_require_editable_key_rejects_reserved(reserved: str) -> None:
    from api.env_settings import require_editable_key

    with pytest.raises(ValueError):
        require_editable_key(reserved)


def test_quote_value_escapes_special_chars() -> None:
    from api.env_settings import quote_value

    # Spaces, double-quotes, $, =, #, \n, \r, backslashes must all be
    # rejected or escaped inside a single-line literal so a later .env
    # re-parse would recover the exact bytes.
    raw_with_newline = 'hello "world" $PATH=#one=\\two\n'
    with pytest.raises(ValueError):
        # Newlines are still rejected outright — the safer default for an
        # env file is to forbid them entirely.
        quote_value(raw_with_newline)
    raw = 'hello "world" $PATH=#one=\\two'
    quoted = quote_value(raw)
    assert quoted.startswith('"') and quoted.endswith('"')
    # The quoted form should NOT contain any unescaped dollar signs or
    # unescaped backslashes — otherwise a downstream $VAR expansion or
    # accidental escape processing could corrupt the value.
    assert r"\$" in quoted
    assert r"\\" in quoted
    assert r"\"" in quoted


def test_upsert_env_entry_writes_and_round_trips(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry, list_env_keys_with_state
    from api.providers import _load_env_file

    env_path = tmp_path / ".env"
    result = upsert_env_entry(env_path, "TEST_ENV_VALID", "synthetic-value-1")
    assert result["status"] == "saved"
    assert result["key"] == "TEST_ENV_VALID"
    assert result["requires_restart"] is True
    # Stored value matches input exactly.
    stored = _load_env_file(env_path)
    assert stored["TEST_ENV_VALID"] == "synthetic-value-1"
    # list helper returns names only (never values).
    state = list_env_keys_with_state(env_path)
    assert state == [{"name": "TEST_ENV_VALID", "set": True}]
    state_blob = json.dumps(state)
    assert "synthetic-value-1" not in state_blob


def test_upsert_duplicate_updates_in_place_not_duplicated(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry

    env_path = tmp_path / ".env"
    key = "TEST_ENV_DUPLICATE"
    assert upsert_env_entry(env_path, key, "first-value")["status"] == "saved"
    assert upsert_env_entry(env_path, key, "second-value")["status"] == "saved"
    content = env_path.read_text(encoding="utf-8")
    assert content.count(f"{key}=") == 1
    assert content.count("first-value") == 0
    assert content.count("second-value") == 1


def test_upsert_special_chars_round_trip(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry
    from api.providers import _load_env_file

    env_path = tmp_path / ".env"
    raw = "spaces and = signs #hashes 'and quotes' \"too\" back\\slash"
    upsert_env_entry(env_path, "TEST_ENV_SPECIAL", raw)
    assert _load_env_file(env_path)["TEST_ENV_SPECIAL"] == raw


def test_upsert_empty_value_rejected(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry

    env_path = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert_env_entry(env_path, "TEST_ENV_VALID", "")


def test_upsert_newline_value_rejected(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry

    env_path = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert_env_entry(env_path, "TEST_ENV_VALID", "line one\nline two")


def test_upsert_reserved_key_rejected(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry

    env_path = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert_env_entry(env_path, "PATH", "/usr/bin")


def test_upsert_atomic_on_failure_does_not_corrupt(tmp_path: Path) -> None:
    """A simulated mid-write crash must leave the pre-existing .env intact."""
    from api.env_settings import upsert_env_entry
    from api.providers import _write_env_file as real_write

    env_path = tmp_path / ".env"
    # Pre-existing file with a comment, a key, and a blank line that the
    # atomic writer must preserve across a forced failure.
    env_path.write_text(
        textwrap.dedent("""\
            # pre-existing comment
            EXISTING_KEY=keep-me

            OTHER_KEY=also-keep
        """),
        encoding="utf-8",
    )
    original = env_path.read_text(encoding="utf-8")

    # Patch the writer so the second call raises; the first call (the
    # one we trigger with this test) is the one we want to see fail.
    # We also want the .env file to be in the state the writer left it
    # mid-call, so we manually simulate a half-written temp file. The
    # atomic-rename contract guarantees the *original* path is untouched
    # if the rename never fires, so the assertions below must hold.
    def _boom(path, updates):
        # Leave a half-written temp file behind to mirror a real crash,
        # but never touch the original .env path.
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("PARTIAL WRITE\n")
        raise RuntimeError("simulated writer failure")

    import api.env_settings as env_settings

    real_writer = env_settings._write_env_file
    env_settings._write_env_file = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            upsert_env_entry(env_path, "TEST_ENV_ATOMIC", "should-not-stick")
    finally:
        env_settings._write_env_file = real_writer  # type: ignore[assignment]

    # Original file is byte-identical to before.
    assert env_path.read_text(encoding="utf-8") == original
    # New key never made it in.
    assert "TEST_ENV_ATOMIC" not in env_path.read_text(encoding="utf-8")
    assert "should-not-stick" not in env_path.read_text(encoding="utf-8")
    # Pre-existing keys + comments intact.
    assert "# pre-existing comment" in env_path.read_text(encoding="utf-8")
    assert "EXISTING_KEY=keep-me" in env_path.read_text(encoding="utf-8")
    assert "OTHER_KEY=also-keep" in env_path.read_text(encoding="utf-8")


def test_delete_env_entry_removes_key(tmp_path: Path) -> None:
    from api.env_settings import upsert_env_entry, delete_env_entry
    from api.providers import _load_env_file

    env_path = tmp_path / ".env"
    upsert_env_entry(env_path, "TEST_ENV_DELETE", "value-to-remove")
    delete_env_entry(env_path, "TEST_ENV_DELETE")
    assert "TEST_ENV_DELETE" not in _load_env_file(env_path)


def test_delete_reserved_key_rejected(tmp_path: Path) -> None:
    from api.env_settings import delete_env_entry

    env_path = tmp_path / ".env"
    with pytest.raises(ValueError):
        delete_env_entry(env_path, "PATH")


# ── HTTP-level tests against handle_get / handle_post ───────────────────


def _post_env(body: dict) -> _FakeHandler:
    from api.routes import handle_post

    raw = json.dumps(body).encode("utf-8")
    handler = _FakeHandler(body_bytes=raw)
    handle_post(handler, urlparse("http://example.com/api/env/settings"))
    return handler


def _get_env() -> _FakeHandler:
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urlparse("http://example.com/api/env/settings"))
    return handler


def test_post_returns_requires_restart_and_no_value_echo(tmp_path: Path) -> None:
    value = "synthetic-not-echoed-value"
    handler = _post_env({"key": "TEST_ENV_VALID", "value": value})
    assert handler.status == 200
    payload = handler.json_body()
    assert payload["status"] == "saved"
    assert payload["requires_restart"] is True
    assert payload["key"] == "TEST_ENV_VALID"
    # Value must not appear anywhere in the response body.
    assert value not in bytes(handler.body).decode("utf-8")


@pytest.mark.parametrize(
    "bad_key",
    ["lowercase_key", "1NUMBER_FIRST", "FOO BAR", "", "FOO$BAR"],
)
def test_post_rejects_malformed_keys(bad_key: str) -> None:
    handler = _post_env({"key": bad_key, "value": "synthetic"})
    assert handler.status == 400
    assert "error" in handler.json_body()


@pytest.mark.parametrize(
    "reserved",
    ["HERMES_WEBUI_PASSWORD", "HERMES_HOME", "PATH", "LD_PRELOAD"],
)
def test_post_rejects_reserved_keys(reserved: str) -> None:
    handler = _post_env({"key": reserved, "value": "synthetic"})
    assert handler.status == 400
    body = handler.json_body()
    assert "reserved" in body["error"].lower() or "match" in body["error"].lower()


def test_post_rejects_empty_value() -> None:
    handler = _post_env({"key": "TEST_ENV_VALID", "value": ""})
    assert handler.status == 400


def test_post_rejects_newline_value(tmp_path: Path) -> None:
    handler = _post_env({"key": "TEST_ENV_VALID", "value": "line one\nline two"})
    assert handler.status == 400
    # Nothing persisted.
    from api.providers import _load_env_file

    assert "TEST_ENV_VALID" not in _load_env_file(tmp_path / ".env")


def test_post_delete_removes_key(tmp_path: Path) -> None:
    _post_env({"key": "TEST_ENV_DELETE", "value": "to-be-removed"})
    delete_handler = _post_env({"key": "TEST_ENV_DELETE", "delete": True})
    assert delete_handler.status == 200
    assert delete_handler.json_body()["status"] == "deleted"
    from api.providers import _load_env_file

    assert "TEST_ENV_DELETE" not in _load_env_file(tmp_path / ".env")


def test_post_non_boolean_delete_rejected() -> None:
    handler = _post_env({"key": "TEST_ENV_VALID", "value": "x", "delete": "true"})
    assert handler.status == 400


def test_get_returns_names_only_no_values(tmp_path: Path) -> None:
    _post_env({"key": "TEST_ENV_QUOTED", "value": "first-secret-shaped-value"})
    _post_env({"key": "TEST_ENV_VALID", "value": "second-secret-shaped-value"})
    handler = _get_env()
    assert handler.status == 200
    payload = handler.json_body()
    assert isinstance(payload["keys"], list)
    names = sorted(entry["name"] for entry in payload["keys"])
    assert names == ["TEST_ENV_QUOTED", "TEST_ENV_VALID"]
    # Each entry is {name, set} — no values.
    for entry in payload["keys"]:
        assert set(entry.keys()) == {"name", "set"}
        assert entry["set"] is True
    body = bytes(handler.body).decode("utf-8")
    assert "first-secret-shaped-value" not in body
    assert "second-secret-shaped-value" not in body


def test_internal_failure_returns_generic_500_without_value(
    monkeypatch, tmp_path: Path
) -> None:
    """A mid-write crash inside env_settings must NOT echo the value."""
    import api.env_settings as env_settings

    value = "synthetic-value-that-must-not-leak"

    def _boom(path, key, val):
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(env_settings, "upsert_env_entry", _boom)
    handler = _post_env({"key": "TEST_ENV_VALID", "value": value})
    assert handler.status == 500
    body = bytes(handler.body).decode("utf-8")
    assert value not in body
    # Generic message — no value, no key leak in the body either (key is
    # allowed, but the value is what we never want echoed).