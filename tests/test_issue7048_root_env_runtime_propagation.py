"""
Regression tests for issue #7048: root/parent .env propagation into a named
profile's agent runtime env (``get_profile_runtime_env``).

The first fix attempt propagated EVERY non-profile key from the root
``$HERMES_HOME/.env`` into the agent runtime. That leaked WebUI/server auth
secrets and arbitrary ``*_API_KEY`` credentials into named profiles (breaking
the named-profile isolation invariant) and misparsed the supported
``export KEY=value`` dotenv syntax. This suite pins the corrected contract:

  - explicit origin-aware allowlist (SEARXNG_URL, FIRECRAWL_*) — never a
    blanket root→profile merge;
  - precedence: profile-defined key (INCLUDING empty) > existing
    launcher/process value > allowlisted root fallback;
  - canonical dotenv parsing (``export`` prefix handled);
  - default-profile and isolated-profile layouts stay safe.
"""

from pathlib import Path

from api.profiles import get_profile_runtime_env, _parse_dotenv_text

# Ambient vars that could act as launcher/process values or leaks if left set.
_AMBIENT_ENV_KEYS = (
    "SEARXNG_URL",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
    "HERMES_WEBUI_PASSWORD",
    "HERMES_WEBUI_ISOLATED_PROFILE",
    "OPENAI_API_KEY",
    "MY_RANDOM_VAR",
    "MY_PROFILE_KEY",
)


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_root_secret_and_credential_keys_are_never_inherited(tmp_path, monkeypatch):
    """[root-secret exclusion] Non-allowlisted root keys never cross into a
    named profile — server auth secrets, arbitrary credentials, and plain
    non-allowlisted vars stay at the root."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "SEARXNG_URL=http://root-searx:8080\n"
        "HERMES_WEBUI_PASSWORD=server-auth-secret\n"
        "OPENAI_API_KEY=sk-root-secret\n"
        "MY_RANDOM_VAR=not-allowlisted\n"
        "HERMES_WEBUI_ISOLATED_PROFILE=0\n",
    )

    env = get_profile_runtime_env(alpha)

    # The allowlisted operator setting IS shared…
    assert env.get("SEARXNG_URL") == "http://root-searx:8080"
    # …but server auth secrets, arbitrary credentials and non-allowlisted
    # keys are NOT blanket-inherited, and the isolation posture stays
    # operator-only.
    assert "HERMES_WEBUI_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "MY_RANDOM_VAR" not in env
    assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env


def test_allowlisted_firecrawl_prefix_is_shared(tmp_path, monkeypatch):
    """[allowlisted shared variable] FIRECRAWL_* (the #7048 operator settings)
    propagate from the root .env into the named profile runtime env."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "FIRECRAWL_API_KEY=fc-123\nFIRECRAWL_BASE_URL=http://fc:3002\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("FIRECRAWL_API_KEY") == "fc-123"
    assert env.get("FIRECRAWL_BASE_URL") == "http://fc:3002"


def test_launcher_process_value_wins_over_root_fallback(tmp_path, monkeypatch):
    """[launcher precedence] An existing launcher/process value is never
    overridden by the allowlisted root fallback (the returned dict omits the
    key so the process value stands after the merge)."""
    base, alpha = _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("SEARXNG_URL", "http://launcher:8080")
    _write_env(base / ".env", "SEARXNG_URL=http://root:8080\n")

    env = get_profile_runtime_env(alpha)

    assert "SEARXNG_URL" not in env, (
        "root fallback must not override an existing launcher/process value"
    )


def test_empty_profile_key_suppresses_root_inheritance(tmp_path, monkeypatch):
    """[empty-profile suppression] A profile that defines a key as EMPTY
    suppresses root inheritance — the root value must not resurrect it."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(base / ".env", "SEARXNG_URL=http://root:8080\n")
    _write_env(alpha / ".env", "SEARXNG_URL=\n")

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "", (
        "a profile-defined empty key must suppress root inheritance, "
        "not let the root value resurrect"
    )


def test_non_empty_profile_key_wins_over_root(tmp_path, monkeypatch):
    """[non-empty-profile precedence] A non-empty profile value beats the root
    fallback for the same allowlisted key."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(base / ".env", "SEARXNG_URL=http://root:8080\n")
    _write_env(alpha / ".env", "SEARXNG_URL=http://profile:8080\n")

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "http://profile:8080"


def test_default_profile_has_no_root_fallback_layer(tmp_path, monkeypatch):
    """[default-profile behavior] The default/root profile's home IS the base:
    its own .env is already the top layer, so no extra root fallback may be
    layered on top (and its own .env stays unrestricted)."""
    base, _alpha = _layout(tmp_path, monkeypatch)
    _write_env(base / ".env", "SEARXNG_URL=http://default:8080\nMY_RANDOM_VAR=ok\n")

    env = get_profile_runtime_env(base)

    # Own .env read through the normal profile path — no double-source issue.
    assert env.get("SEARXNG_URL") == "http://default:8080"
    # The profile .env path is unrestricted (allowlist only governs ROOT keys).
    assert env.get("MY_RANDOM_VAR") == "ok"


def test_isolated_profile_layout_still_respects_allowlist(tmp_path, monkeypatch):
    """[isolated-profile behavior] In isolated multi-user mode the pinned
    profile home is still */profiles/<name>: the allowlisted root fallback
    applies, but the isolation invariant holds — server auth secrets and
    non-allowlisted root keys never cross into the pinned profile, and the
    posture flag is never propagated."""
    base, alpha = _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_ISOLATED_PROFILE", "1")
    _write_env(
        base / ".env",
        "SEARXNG_URL=http://root:8080\n"
        "HERMES_WEBUI_PASSWORD=server-auth-secret\n"
        "OPENAI_API_KEY=sk-root\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "http://root:8080"
    assert "HERMES_WEBUI_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env


def test_export_prefixed_lines_parse_canonically(tmp_path, monkeypatch):
    """[export parsing] 'export KEY=value' root lines parse as KEY (not
    'export KEY') — both exact-allowlist and prefix-allowlist keys."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "export SEARXNG_URL=http://root-export:8080\n"
        "export FIRECRAWL_API_KEY=fc-export\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "http://root-export:8080", (
        "'export KEY=value' must parse as KEY, not 'export KEY'"
    )
    assert env.get("FIRECRAWL_API_KEY") == "fc-export"


def test_export_prefix_in_profile_env_parses(tmp_path, monkeypatch):
    """The canonical parser also serves the profile .env read: 'export' lines
    parse correctly and protected keys stay filtered."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        alpha / ".env",
        "export MY_PROFILE_KEY=value1\n"
        "export HERMES_WEBUI_ISOLATED_PROFILE=0\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("MY_PROFILE_KEY") == "value1"
    assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env


def test_parse_dotenv_text_handles_export_and_quoting():
    """Direct unit coverage of the canonical parser: comments, export prefix
    (incl. extra whitespace), quoting, and preserved empty values."""
    parsed = _parse_dotenv_text(
        "# comment\n"
        "export KEY_ONE=value1\n"
        "KEY_TWO=\"quoted value\"\n"
        "export  KEY_THREE='single'\n"
        "KEY_FOUR=\n"
        "\n"
    )
    assert parsed == {
        "KEY_ONE": "value1",
        "KEY_TWO": "quoted value",
        "KEY_THREE": "single",
        "KEY_FOUR": "",
    }


def _layout(tmp_path, monkeypatch):
    """Docker-style layout: base ~/.hermes with a root .env + named profiles.

    Returns (base, alpha_home). Ambient vars are cleared first so they cannot
    masquerade as launcher/process values or leaks.
    """
    base = tmp_path / ".hermes"
    profiles_root = base / "profiles"
    alpha = profiles_root / "alpha"
    (profiles_root / "beta").mkdir(parents=True)
    alpha.mkdir(parents=True, exist_ok=True)
    for key in _AMBIENT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return base, alpha
