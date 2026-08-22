import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _RejectNegativeRead:
    def read(self, n=-1):
        if n < 0:
            raise AssertionError("read_body must reject negative Content-Length before read(-1)")
        return b"{}"


def test_read_body_rejects_negative_content_length_without_unbounded_read():
    from api.helpers import read_body

    handler = SimpleNamespace(headers=_Headers({"Content-Length": "-1"}), rfile=_RejectNegativeRead(), close_connection=False)

    with pytest.raises(ValueError, match="Content-Length"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_decodes_chunked_transfer_encoding():
    from api.helpers import read_body

    body = b'{"a": 1}'
    chunked = b"8\r\n" + body + b"\r\n0\r\n\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def test_read_body_rejects_malformed_chunk_size():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b"zz\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Malformed chunk size"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_oversized_chunked_body():
    from api.helpers import MAX_BODY_BYTES, read_body

    size = MAX_BODY_BYTES + 1
    chunked = b"%x\r\n" % size + b"x" * size + b"\r\n0\r\n\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Request body too large"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_incomplete_chunk_body():
    from api.helpers import read_body

    # Declares 8 bytes but the stream ends after 5
    chunked = b"8\r\n{\"a\":"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Incomplete chunk body"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_missing_terminating_chunk():
    from api.helpers import read_body

    # One complete chunk, but no terminating 0-chunk before EOF
    chunked = b"8\r\n{\"a\": 1}\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="missing terminating chunk"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_oversized_chunked_trailers():
    from api.helpers import MAX_CHUNKED_TRAILER_BYTES, read_body

    chunked = b"0\r\n" + b"x" * (MAX_CHUNKED_TRAILER_BYTES + 100) + b"\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="trailers too large"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_empty_chunked_stream():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b""),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="missing terminating chunk"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_eof_in_trailer_section():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b"0\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="EOF in trailer section"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_negative_chunk_size():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b"-1\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Malformed chunk size"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_invalid_chunk_data_delimiter():
    from api.helpers import read_body

    # 8 bytes of data followed by "xx" instead of CRLF
    chunked = b"8\r\n{\"a\": 1}xx"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Invalid chunk data delimiter"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_accepts_multiple_chunks():
    from api.helpers import read_body

    # {"a": 1} split as 4 + 4 bytes
    chunked = b"4\r\n{\"a\"\r\n4\r\n: 1}\r\n0\r\n\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def test_read_body_accepts_chunk_extensions():
    from api.helpers import read_body

    chunked = b"8;foo=bar\r\n{\"a\": 1}\r\n0\r\n\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def test_read_body_accepts_mixed_case_chunked_token():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "Chunked"}),
        rfile=io.BytesIO(b"8\r\n{\"a\": 1}\r\n0\r\n\r\n"),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def test_read_body_rejects_non_chunked_transfer_encoding():
    from api.helpers import read_body

    for header_value in ("gzip, chunked", "xchunked", "identity"):
        handler = SimpleNamespace(
            headers=_Headers({"Transfer-Encoding": header_value}),
            rfile=io.BytesIO(b""),
            close_connection=False,
        )

        with pytest.raises(ValueError, match="Unsupported Transfer-Encoding"):
            read_body(handler)
        assert handler.close_connection is True


def test_read_body_rejects_empty_transfer_encoding_header():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": " "}),
        rfile=io.BytesIO(b""),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Invalid Transfer-Encoding"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_bare_lf_size_line():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b"8\n{\"a\": 1}\r\n0\r\n\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Malformed chunk size line"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_whitespace_padded_chunk_size():
    from api.helpers import read_body

    for padded in (b" 8\r\n{\"a\": 1}\r\n0\r\n\r\n", b"8 \r\n{\"a\": 1}\r\n0\r\n\r\n", b"\t8\r\n{\"a\": 1}\r\n0\r\n\r\n"):
        handler = SimpleNamespace(
            headers=_Headers({"Transfer-Encoding": "chunked"}),
            rfile=io.BytesIO(padded),
            close_connection=False,
        )

        with pytest.raises(ValueError, match="Malformed chunk size"):
            read_body(handler)
        assert handler.close_connection is True


def test_read_body_rejects_bare_lf_trailer_terminator():
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(b"0\r\n\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Malformed trailer line"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_accepts_trailer_fields():
    from api.helpers import read_body

    chunked = b"8\r\n{\"a\": 1}\r\n0\r\nFoo: bar\r\n\r\n"
    handler = SimpleNamespace(
        headers=_Headers({"Transfer-Encoding": "chunked"}),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def _real_http_headers(pairs):
    """Build a genuine http.client.HTTPMessage (what http.server hands the
    handler) so get_all() repeated-header semantics are exercised for real."""
    import http.client

    msg = http.client.HTTPMessage()
    for key, value in pairs:
        msg[key] = value
    return msg


def test_read_body_rejects_content_length_and_transfer_encoding_together():
    """CL.TE / TE.CL smuggling guard: a request carrying BOTH Content-Length
    and Transfer-Encoding must be refused with the connection closed, so
    trailing bytes cannot be replayed as a smuggled second request."""
    from api.helpers import read_body

    body = b'{"a": 1}'
    chunked = b"8\r\n" + body + b"\r\n0\r\n\r\n"
    handler = SimpleNamespace(
        headers=_real_http_headers([("Transfer-Encoding", "chunked"), ("Content-Length", "8")]),
        rfile=io.BytesIO(chunked),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Ambiguous framing"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_duplicate_transfer_encoding_hiding_a_coding():
    """A repeated Transfer-Encoding header must not let a non-chunked coding
    slip past a .get()-only check. get_all() sees every line, so
    'Transfer-Encoding: chunked' + 'Transfer-Encoding: gzip' is rejected."""
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_real_http_headers([("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "gzip")]),
        rfile=io.BytesIO(b"0\r\n\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Unsupported Transfer-Encoding"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_rejects_duplicate_te_chunked_then_chunked():
    """Two Transfer-Encoding: chunked lines still collapse to >1 coding and are
    rejected — multiple codings are never accepted even if all are 'chunked'."""
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_real_http_headers([("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")]),
        rfile=io.BytesIO(b"0\r\n\r\n"),
        close_connection=False,
    )

    with pytest.raises(ValueError, match="Unsupported Transfer-Encoding codings"):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_empty_transfer_encoding_header_is_not_treated_as_absent():
    """An exactly-empty Transfer-Encoding header is still a present TE header;
    it must take the TE path (and be rejected as having no valid coding) rather
    than silently falling through to Content-Length parsing."""
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_real_http_headers([("Transfer-Encoding", ""), ("Content-Length", "2")]),
        rfile=io.BytesIO(b"{}"),
        close_connection=False,
    )

    # Present-but-empty TE with a Content-Length is ambiguous framing → rejected.
    with pytest.raises(ValueError):
        read_body(handler)
    assert handler.close_connection is True


def test_read_body_plain_content_length_still_works_with_real_headers():
    """Non-chunked Content-Length requests remain unchanged with real headers."""
    from api.helpers import read_body

    handler = SimpleNamespace(
        headers=_real_http_headers([("Content-Length", "8")]),
        rfile=io.BytesIO(b'{"a": 1}'),
        close_connection=False,
    )

    assert read_body(handler) == {"a": 1}
    assert handler.close_connection is False


def test_session_save_rejects_unsafe_session_id(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    session = models.Session(session_id="../escape", workspace=str(tmp_path), messages=[])

    with pytest.raises(ValueError, match="session_id"):
        session.save()

    numeric_session = models.Session(session_id=123, workspace=str(tmp_path), messages=[])
    with pytest.raises(ValueError, match="session_id"):
        numeric_session.save()

    assert not (tmp_path / "escape.json").exists()


def test_bespoke_telemetry_body_readers_reject_invalid_lengths_without_unbounded_read():
    import api.routes as routes

    for reader in (routes._read_csp_report_payload, routes._read_client_event_payload):
        handler = SimpleNamespace(headers=_Headers({"Content-Length": "-1"}), rfile=_RejectNegativeRead(), close_connection=False)
        payload = reader(handler)
        assert handler.close_connection is True
        assert payload.get("discarded") == "invalid_content_length" or payload.get("reason") == "invalid_content_length"


def test_bespoke_telemetry_body_readers_close_connection_on_oversize():
    import api.routes as routes

    cases = [
        (routes._read_csp_report_payload, routes._CSP_REPORT_MAX_BODY_BYTES + 1),
        (routes._read_client_event_payload, routes._CLIENT_EVENT_MAX_BODY_BYTES + 1),
    ]
    for reader, size in cases:
        handler = SimpleNamespace(headers=_Headers({"Content-Length": str(size)}), rfile=_RejectNegativeRead(), close_connection=False)
        payload = reader(handler)
        assert handler.close_connection is True
        assert payload.get("discarded") == "body_too_large" or payload.get("reason") == "body_too_large"


def test_auth_sessions_have_lock_and_success_can_clear_login_attempts(monkeypatch, tmp_path):
    import api.auth as auth

    assert hasattr(auth, "_SESSIONS_LOCK"), "auth session dict mutations must be lock-protected"
    assert hasattr(auth, "_clear_login_attempts"), "successful login needs to clear failed attempt bucket"

    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", tmp_path / ".login_attempts.json")
    auth._login_attempts.clear()
    auth._login_attempts["127.0.0.1"] = [1.0, 2.0, 3.0, 4.0]

    auth._clear_login_attempts("127.0.0.1")

    assert "127.0.0.1" not in auth._login_attempts


def _english_i18n_keys():
    text = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    match = re.search(r"en:\s*\{([\s\S]*?)\n\s*\},\n\s*[a-z]{2}:", text)
    assert match, "could not find English locale block"
    return set(re.findall(r"^\s*([A-Za-z0-9_]+):", match.group(1), re.M))


def _literal_i18n_refs():
    refs = set()
    for path in (ROOT / "static").glob("*.js"):
        if path.name == "i18n.js":
            continue
        text = path.read_text(encoding="utf-8")
        refs.update(re.findall(r"\bt\(\s*['\"]([A-Za-z0-9_]+)['\"]", text))
        refs.update(re.findall(r"data-i18n(?:-[a-z]+)?=['\"]([A-Za-z0-9_]+)['\"]", text))
    return {key for key in refs if not key.endswith("_")}


def test_static_literal_i18n_keys_exist_in_english_locale():
    missing = sorted(_literal_i18n_refs() - _english_i18n_keys())

    assert missing == []


def test_critical_boot_storage_access_is_guarded():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    theme_script = re.search(r"<script>\(function\(\)\{[\s\S]*?hermes-theme[\s\S]*?\}\)\(\)</script>", index)
    font_script = re.search(r"<script>\(function\(\)\{[\s\S]*?hermes-font-size[\s\S]*?\}\)\(\)</script>", index)
    assert theme_script and "try" in theme_script.group(0)
    assert font_script and "try" in font_script.group(0)
    assert "try{localStorage.removeItem('hermes-webui-server-stopped')" in boot
    assert "try { localStorage.setItem('hermes-lang', resolved); } catch" in i18n
    assert "try { stored = localStorage.getItem('hermes-lang'); } catch" in i18n


def test_stale_session_recovery_preserves_subpath_mount_root():
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "history.replaceState(null,'','/')" not in sessions
    assert "_appRootPath" in sessions


def test_session_url_builder_strips_legacy_session_query_alias():
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    helper = sessions[sessions.index("function _sessionUrlForSid"):sessions.index("function _setActiveSessionUrl")]
    assert "current.searchParams.delete('session');" in helper
    assert "current.searchParams.delete('session_id');" in helper


def test_cross_profile_session_deep_links_switch_profile_instead_of_self_healing():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert '"code": "session_profile_mismatch"' in routes
    assert 'if method == "GET" and path == "/api/session":' in routes
    assert "function _sessionProfileMismatchFromError" in sessions
    assert "_switchProfileForSessionLoad(profileMismatch.profile)" in sessions
    assert "skipProfileResolve:true" in sessions


def test_service_worker_precaches_same_origin_vendor_shell_assets():
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    assert "./static/vendor/smd.min.js" in sw
    assert "./static/vendor/katex/0.16.22/katex.min.css" in sw
    assert "./static/vendor/katex/0.16.22/katex.min.js" in sw


def test_cancel_session_stream_closes_local_eventsource_on_failure_path():
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    helper = boot[boot.index("async function cancelSessionStream"):boot.index("async function _savedSessionShouldStaySidebarOnly")]

    assert "closeLiveStream(sid,streamId" in helper or "closeLiveStream(sid, streamId" in helper
    assert "catch(e){/* cancel request failed - cleanup below still runs */}" not in helper
