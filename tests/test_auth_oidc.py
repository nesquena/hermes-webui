"""Tests for #6603 — ambiguous legacy OIDC allowlist scalar warning.

Scope: _resolve_oidc_config warning detection and _enforce_allowlist decisions.
No whitespace splitting under any heuristic; the warning is diagnostic only.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import api.auth_oidc as auth_oidc
from api.auth_oidc import _enforce_allowlist, _normalize_allow_values, OIDCAuthError


@pytest.fixture(autouse=True)
def _clear_oidc_environment(monkeypatch):
    for name in (
        "HERMES_WEBUI_OIDC_ISSUER",
        "HERMES_WEBUI_OIDC_CLIENT_ID",
        "HERMES_WEBUI_OIDC_ALLOW_CLAIM",
        "HERMES_WEBUI_OIDC_ALLOW_VALUES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_warned_cache():
    auth_oidc._warned_allow_values.clear()
    yield
    auth_oidc._warned_allow_values.clear()


def _startup_warning(monkeypatch, allow_values):
    import api.auth as auth

    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: {
            "webui_oidc": {
                "issuer": "https://issuer.example",
                "client_id": "webui-client",
                "allow_claim": "email",
                "allow_values": allow_values,
            }
        },
    )
    return auth.get_oidc_startup_warning()


@pytest.mark.parametrize("allow_values", [[], [""]])
def test_startup_warning_treats_empty_allowlist_shapes_as_missing(monkeypatch, allow_values):
    warning = _startup_warning(monkeypatch, allow_values)

    assert warning is not None
    assert "allow_values" in warning
    assert auth_oidc._ALLOW_VALUES_WHITESPACE_WARNING not in warning


def test_startup_warning_accepts_multi_word_list(monkeypatch):
    assert _startup_warning(monkeypatch, ["Hermes Users"]) is None


def test_startup_warning_accepts_comma_scalar(monkeypatch):
    assert _startup_warning(monkeypatch, "alice@example.com,bob@example.com") is None


def test_startup_warning_reports_whitespace_scalar_migration(monkeypatch):
    warning = _startup_warning(monkeypatch, "alice@example.com bob@example.com")

    assert warning is not None
    assert "partially configured" not in warning
    assert auth_oidc._ALLOW_VALUES_WHITESPACE_WARNING in warning


def test_startup_warning_uses_environment_allow_values_precedence(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_VALUES", "")

    warning = _startup_warning(monkeypatch, ["alice@example.com"])

    assert warning is not None
    assert "allow_values" in warning


def _resolve(monkeypatch, *, env=None, cfg_list=None):
    """Call _resolve_oidc_config with controlled allow_values input.

    Pass env= for a string env-var value, cfg_list= for a list from config.
    Pass neither to simulate absent/None.
    """
    monkeypatch.delenv("HERMES_WEBUI_OIDC_ALLOW_VALUES", raising=False)
    if env is not None:
        monkeypatch.setenv("HERMES_WEBUI_OIDC_ALLOW_VALUES", env)
        webui_cfg = {}
    elif cfg_list is not None:
        webui_cfg = {"allow_values": cfg_list}
    else:
        webui_cfg = {}
    with patch("api.auth_oidc.get_config", return_value={"webui_oidc": webui_cfg}):
        return auth_oidc._resolve_oidc_config()


# ---------------------------------------------------------------------------
# reproduction
# ---------------------------------------------------------------------------

def test_legacy_scalar_warns(monkeypatch, caplog):
    """Exact scalar from #6603 issue emits a warning naming the setting and forms."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env="alice@example.com bob@example.com")

    assert any(
        "HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records
    ), "expected warning naming HERMES_WEBUI_OIDC_ALLOW_VALUES"

    warning_text = " ".join(r.message for r in caplog.records)
    assert "comma" in warning_text.lower(), "warning must mention comma-delimited form"
    assert "yaml array" in warning_text.lower() or "yaml" in warning_text.lower(), (
        "warning must mention YAML array form"
    )

    # allowlist unchanged — one combined value, claim still denied
    assert cfg["allow_values"] == ["alice@example.com bob@example.com"]
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "alice@example.com"},
            allow_claim="email",
            allow_values=cfg["allow_values"],
        )


# ---------------------------------------------------------------------------
# authorization unchanged
# ---------------------------------------------------------------------------

def test_enforce_decisions_unchanged(monkeypatch):
    """_enforce_allowlist allow/deny decisions are correct for every standard input shape."""
    # no allow_claim → always passes
    _enforce_allowlist({"email": "anyone"}, allow_claim="", allow_values=[])

    # matching value → allowed
    _enforce_allowlist(
        {"email": "alice@example.com"},
        allow_claim="email",
        allow_values=["alice@example.com", "bob@example.com"],
    )

    # non-matching value → denied
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "eve@example.com"},
            allow_claim="email",
            allow_values=["alice@example.com", "bob@example.com"],
        )

    # absent claim → denied
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"sub": "1234"},
            allow_claim="email",
            allow_values=["alice@example.com"],
        )

    # allow_claim set, allow_values empty → any non-empty claim value passes
    _enforce_allowlist(
        {"email": "anyone@example.com"},
        allow_claim="email",
        allow_values=[],
    )

    # whitespace scalar resolves to one combined value; partial claim denied
    combined_values = _normalize_allow_values("alice@example.com bob@example.com")
    assert combined_values == ["alice@example.com bob@example.com"]
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "alice@example.com"},
            allow_claim="email",
            allow_values=combined_values,
        )


# ---------------------------------------------------------------------------
# multi-word group preserved — issue requirement 5a
# ---------------------------------------------------------------------------

def test_multi_word_group(monkeypatch, caplog):
    """'Hermes Users' stays one allow_values entry; a 'Hermes' fragment is denied."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env="Hermes Users")

    # normalization preserves the full group name as one value
    assert cfg["allow_values"] == ["Hermes Users"]

    # full group name → allowed
    _enforce_allowlist(
        {"groups": "Hermes Users"},
        allow_claim="groups",
        allow_values=cfg["allow_values"],
    )

    # fragment → denied
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"groups": "Hermes"},
            allow_claim="groups",
            allow_values=cfg["allow_values"],
        )

    # warning emitted (operator should confirm intent)
    assert any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# no fragment authorization — issue requirement 4
# ---------------------------------------------------------------------------

def test_no_fragment_authorized(monkeypatch):
    """Team@Corp Admin@Corp as one value; Admin@Corp claim is denied."""
    combined = _normalize_allow_values("Team@Corp Admin@Corp")
    assert combined == ["Team@Corp Admin@Corp"]

    # single-string claim
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"groups": "Admin@Corp"},
            allow_claim="groups",
            allow_values=combined,
        )

    # array claim (simulates groups claim as list, matching _claim_values behavior)
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"groups": ["Admin@Corp"]},
            allow_claim="groups",
            allow_values=combined,
        )

    # exact combined value is the only thing that passes
    _enforce_allowlist(
        {"groups": "Team@Corp Admin@Corp"},
        allow_claim="groups",
        allow_values=combined,
    )


# ---------------------------------------------------------------------------
# canonical forms silent — negative space
# ---------------------------------------------------------------------------

def test_canonical_no_warning(monkeypatch, caplog):
    """Comma, newline, and list configurations emit no warning."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        # comma-delimited scalar
        cfg = _resolve(monkeypatch, env="alice@example.com,bob@example.com")
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "comma scalar must not emit a warning"
        )

        caplog.clear()

        # newline-delimited scalar
        cfg = _resolve(monkeypatch, env="alice@example.com\nbob@example.com")
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "newline scalar must not emit a warning"
        )

        caplog.clear()

        # YAML array (list from config)
        cfg = _resolve(monkeypatch, cfg_list=["alice@example.com", "bob@example.com"])
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "list/array config must not emit a warning"
        )

        caplog.clear()

        # single token with no inner whitespace
        cfg = _resolve(monkeypatch, env="alice@example.com")
        assert cfg["allow_values"] == ["alice@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "single token without inner whitespace must not emit a warning"
        )

        caplog.clear()

        # single token with only leading/trailing whitespace (strip removes it, no inner ws)
        cfg = _resolve(monkeypatch, env="  alice@example.com  ")
        assert cfg["allow_values"] == ["alice@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "leading/trailing-only whitespace must not emit a warning"
        )

        caplog.clear()

        # leading/trailing tab only
        cfg = _resolve(monkeypatch, env="\talice@example.com\t")
        assert cfg["allow_values"] == ["alice@example.com"]
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "leading/trailing-only tab must not emit a warning"
        )

        caplog.clear()

        # absent
        cfg = _resolve(monkeypatch)
        assert cfg["allow_values"] == []
        assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
            "absent value must not emit a warning"
        )


# ---------------------------------------------------------------------------
# mode/state matrix
# ---------------------------------------------------------------------------

def test_allow_values_matrix(monkeypatch, caplog):
    """Every configured shape resolves to its declared allowlist and warning state."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        # row 1: whitespace-only scalar, multiple intended values → one combined, warning
        cfg = _resolve(monkeypatch, env="alice@example.com bob@example.com")
        assert cfg["allow_values"] == ["alice@example.com bob@example.com"]
        assert any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records)
        caplog.clear()
        auth_oidc._warned_allow_values.clear()

        # row 2: whitespace-only scalar, one intended multi-word group → one value, warning
        cfg = _resolve(monkeypatch, env="Hermes Users")
        assert cfg["allow_values"] == ["Hermes Users"]
        assert any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records)
        caplog.clear()
        auth_oidc._warned_allow_values.clear()

        # row 3: comma scalar → split values, no warning
        cfg = _resolve(monkeypatch, env="alice@example.com,bob@example.com")
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not caplog.records
        caplog.clear()

        # row 4: newline scalar → split values, no warning
        cfg = _resolve(monkeypatch, env="alice@example.com\nbob@example.com")
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not caplog.records
        caplog.clear()

        # row 5: list/YAML array → each element, no warning
        cfg = _resolve(monkeypatch, cfg_list=["alice@example.com", "bob@example.com"])
        assert cfg["allow_values"] == ["alice@example.com", "bob@example.com"]
        assert not caplog.records
        caplog.clear()

        # row 6: empty/absent → empty allowlist, no warning
        cfg = _resolve(monkeypatch)
        assert cfg["allow_values"] == []
        assert not caplog.records


# ---------------------------------------------------------------------------
# warning emitted once — not per request
# ---------------------------------------------------------------------------

def test_warning_not_per_request(monkeypatch, caplog):
    """Repeated config resolution with the same whitespace scalar emits the warning once."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        _resolve(monkeypatch, env="alice@example.com bob@example.com")
        _resolve(monkeypatch, env="alice@example.com bob@example.com")
        _resolve(monkeypatch, env="alice@example.com bob@example.com")

    matching = [
        r for r in caplog.records
        if "HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message
    ]
    assert len(matching) == 1, (
        f"expected exactly 1 warning, got {len(matching)}"
    )


# ---------------------------------------------------------------------------
# Unicode whitespace
# ---------------------------------------------------------------------------

def test_tab_separated_scalar_warns(monkeypatch, caplog):
    """Tab-separated scalar emits the same warning as a space-separated one."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env="alice@example.com\tbob@example.com")

    assert cfg["allow_values"] == ["alice@example.com\tbob@example.com"]
    assert any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
        "tab-separated scalar must emit a warning"
    )
    # claim still denied
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "alice@example.com"},
            allow_claim="email",
            allow_values=cfg["allow_values"],
        )


def test_nbsp_separated_scalar_warns(monkeypatch, caplog):
    """Non-breaking-space-separated scalar emits a warning."""
    nbsp_val = "alice@example.com bob@example.com"
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env=nbsp_val)

    assert cfg["allow_values"] == [nbsp_val]
    assert any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
        "NBSP-separated scalar must emit a warning"
    )
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "alice@example.com"},
            allow_claim="email",
            allow_values=cfg["allow_values"],
        )


def test_leading_trailing_whitespace_only_no_warn(monkeypatch, caplog):
    """Single token with only leading/trailing whitespace does not warn."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env="\t  alice@example.com  \t")

    assert cfg["allow_values"] == ["alice@example.com"]
    assert not any("HERMES_WEBUI_OIDC_ALLOW_VALUES" in r.message for r in caplog.records), (
        "leading/trailing-only whitespace must not emit a warning"
    )


# ---------------------------------------------------------------------------
# mixed comma-plus-space scalar
# ---------------------------------------------------------------------------

def test_mixed_comma_space_scalar_warns(monkeypatch, caplog):
    """Mixed comma-plus-space scalar warns; the element with inner whitespace silently denies."""
    with caplog.at_level(logging.WARNING, logger="api.auth_oidc"):
        cfg = _resolve(monkeypatch, env="alice@x.com bob@x.com, carol@x.com")

    assert cfg["allow_values"] == ["alice@x.com bob@x.com", "carol@x.com"]
    warning_text = " ".join(r.message for r in caplog.records)
    assert "HERMES_WEBUI_OIDC_ALLOW_VALUES" in warning_text, (
        "mixed comma-plus-space scalar must emit a warning naming HERMES_WEBUI_OIDC_ALLOW_VALUES"
    )
    # The message must describe what was detected, not say the value contains no commas.
    assert "internal whitespace" in warning_text, (
        "warning must describe internal whitespace in entries, not claim the value has no commas"
    )
    # carol authenticates; alice and bob are denied (their combined address has inner whitespace)
    _enforce_allowlist(
        {"email": "carol@x.com"},
        allow_claim="email",
        allow_values=cfg["allow_values"],
    )
    with pytest.raises(OIDCAuthError):
        _enforce_allowlist(
            {"email": "alice@x.com"},
            allow_claim="email",
            allow_values=cfg["allow_values"],
        )


# ---------------------------------------------------------------------------
# unlisted input shapes
# ---------------------------------------------------------------------------

def test_normalize_allow_values_unlisted_shapes():
    """_normalize_allow_values returns the expected value for every non-standard input shape."""
    assert _normalize_allow_values(None) == []
    assert _normalize_allow_values(True) == ["True"]
    assert _normalize_allow_values(False) == ["False"]
    assert _normalize_allow_values(42) == ["42"]
    assert _normalize_allow_values(3.14) == ["3.14"]
    assert _normalize_allow_values(("alice@example.com", "bob@example.com")) == [
        "alice@example.com", "bob@example.com"
    ]
    assert set(_normalize_allow_values({"alice@example.com", "bob@example.com"})) == {
        "alice@example.com", "bob@example.com"
    }
    assert _normalize_allow_values("alice\rbob") == ["alice\rbob"]  # bare CR stays combined
    assert _normalize_allow_values("") == []
    assert _normalize_allow_values("   ") == []
