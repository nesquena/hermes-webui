"""#9225: the credential field is chosen BEFORE its value is read.

``api_key_env`` is the compatibility spelling of ``key_env`` and takes over
ONLY when the ``key_env`` field itself is absent or blank. When a record
declares ``key_env`` explicitly, that variable is the record's credential
source — even when the variable is unset or empty. Falling back to
``api_key_env`` in that case silently sends a DIFFERENT account's key to the
endpoint the record names, which is a credential leak rather than a
misconfiguration report.

This is the Agent's own rule (``hermes_cli/runtime_provider_custom.py``
``_key_env_secret`` reads ``entry.get("key_env") or entry.get("api_key_env")``
— it picks the NAME first, then reads that one variable) and the rule
``_normalized_raw_provider_record`` applies to ``providers.<id>`` records. The
WebUI ladder read the value first and chained its rungs with ``if not
api_key:``, so a declared-but-unresolved ``key_env`` silently became the
``api_key_env`` variable instead of an unset credential.

What is pinned here:

* both fields declared and both variables set -> ``key_env`` wins;
* ``key_env`` declared but its variable unset -> NO key at all (the declared
  source stays authoritative; the compat spelling must not take over);
* ``key_env`` declared but its variable empty -> NO key at all;
* ``key_env`` absent/blank -> ``api_key_env`` still works on its own;
* the same three rules on the ``providers.<id>`` path, as a direct contrast.

Only synthetic env vars are used: no real ``.env`` is read, no provider is
contacted, and no credential value is printed.
"""
import api.config as config
import api.providers as providers

URL = "https://p.field-precedence.test/v1"
PRIMARY_VAR = "P_PRIMARY_KEY_VAR"
COMPAT_VAR = "P_COMPAT_KEY_VAR"
PRIMARY_KEY = "primary-field-key"
COMPAT_KEY = "compat-field-key"


def _bind_legacy_entry(monkeypatch, **fields):
    """Bind a config whose single legacy ``custom_providers`` entry is ``p``."""
    entry = {"name": "p", "base_url": URL}
    entry.update(fields)
    cfg = {"custom_providers": [entry]}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    # api/providers.py holds its own module-level reference to get_config.
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    # The CUSTOM_<SLUG>_API_KEY convention fallback must stay out of the way so
    # each test pins the spelling under test, not an ambient process key.
    monkeypatch.delenv("CUSTOM_P_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM:P_API_KEY", raising=False)
    return entry


def _bind_native_record(monkeypatch, **fields):
    """Bind a config whose single ``providers['custom:p']`` record is ``p``."""
    record = {"base_url": URL}
    record.update(fields)
    cfg = {"providers": {"custom:p": record}}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    monkeypatch.delenv("CUSTOM_P_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM:P_API_KEY", raising=False)
    return record


# ── the rule: the field is chosen before its value is read ──────────────────


def test_legacy_both_fields_declared_primary_wins(monkeypatch):
    """Both spellings resolve; ``key_env`` is the declared primary."""
    _bind_legacy_entry(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(PRIMARY_VAR, PRIMARY_KEY)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == PRIMARY_KEY, "the compat spelling took over from the declared primary"
    assert base_url == URL


def test_legacy_primary_declared_but_unset_does_not_fall_back(monkeypatch):
    """``key_env`` names a variable that is NOT set: no key, no compat fallback.

    The declared source stays authoritative. Reading the compat variable here
    would send another account's key to the endpoint ``key_env`` names.
    """
    _bind_legacy_entry(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.delenv(PRIMARY_VAR, raising=False)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key is None, (
        "an unset declared key_env must not hand the record to the compat "
        "spelling; the credential source is the declared field"
    )
    assert base_url == URL


def test_legacy_primary_declared_but_empty_does_not_fall_back(monkeypatch):
    """An EMPTY declared variable is as unset as an absent one."""
    _bind_legacy_entry(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(PRIMARY_VAR, "   ")
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key is None, (
        "an empty declared key_env must not hand the record to the compat "
        "spelling"
    )


def test_legacy_compat_alone_still_resolves(monkeypatch):
    """``api_key_env`` on its own keeps working (#8959): no ``key_env`` declared."""
    _bind_legacy_entry(monkeypatch, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == COMPAT_KEY
    assert base_url == URL


def test_legacy_blank_primary_field_lets_the_compat_spelling_take_over(monkeypatch):
    """A BLANK ``key_env`` field is not a declaration: the compat spelling takes over."""
    _bind_legacy_entry(monkeypatch, key_env="   ", api_key_env=COMPAT_VAR)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == COMPAT_KEY


# ── the same rule on the ``providers.<id>`` path (direct contrast) ──────────


def test_native_both_fields_declared_primary_wins(monkeypatch):
    _bind_native_record(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(PRIMARY_VAR, PRIMARY_KEY)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == PRIMARY_KEY
    assert base_url == URL


def test_native_primary_declared_but_unset_does_not_fall_back(monkeypatch):
    _bind_native_record(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.delenv(PRIMARY_VAR, raising=False)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key is None, (
        "the providers.<id> path must apply the same field-first rule"
    )


def test_native_compat_alone_still_resolves(monkeypatch):
    """``providers.<id>`` with only ``api_key_env``: the respelling still works."""
    _bind_native_record(monkeypatch, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == COMPAT_KEY
    assert base_url == URL


# ── the presentation layer follows the same rule ────────────────────────────


def test_presentation_layer_does_not_borrow_the_compat_key(monkeypatch):
    """``_get_provider_api_key`` must report the declared source as unresolved."""
    _bind_legacy_entry(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.delenv(PRIMARY_VAR, raising=False)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    assert providers._get_provider_api_key("custom:p") is None
    assert providers._provider_has_key("custom:p") is False


def test_presentation_layer_uses_the_compat_key_when_it_is_the_only_spelling(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=COMPAT_VAR)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    assert providers._get_provider_api_key("custom:p") == COMPAT_KEY
    assert providers._provider_has_key("custom:p") is True


# ── the bundle keeps a declared-but-unresolved credential unresolved ─────────


def test_bundle_reports_no_key_when_the_declared_primary_is_unset(monkeypatch):
    """A declared-but-unresolved credential is not keyless: it stays a declared source."""
    _bind_legacy_entry(monkeypatch, key_env=PRIMARY_VAR, api_key_env=COMPAT_VAR)
    monkeypatch.delenv(PRIMARY_VAR, raising=False)
    monkeypatch.setenv(COMPAT_VAR, COMPAT_KEY)

    bundle = config.resolve_custom_provider_bundle("custom:p", lookup_only=True)

    assert bundle["api_key"] is None, (
        "the bundle resolved the compat variable for a record that declared "
        "another variable"
    )
    assert bundle["keyless"] is False
