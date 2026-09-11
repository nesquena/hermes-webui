"""
Regression tests for issue #7048: root/parent .env propagation into a named
profile's agent runtime env (``get_profile_runtime_env``).

The first fix attempt propagated EVERY non-profile key from the root
``$HERMES_HOME/.env`` into the agent runtime. That leaked WebUI/server auth
secrets and arbitrary ``*_API_KEY`` credentials into named profiles (breaking
the named-profile isolation invariant) and misparsed the supported
``export KEY=value`` dotenv syntax. This suite pins the corrected contract:

  - explicit origin-aware allowlist (SEARXNG_URL + non-secret FIRECRAWL_*
    config keys; never FIRECRAWL_API_KEY or any *_API_KEY) — never a
    blanket root→profile merge;
  - precedence: profile-defined key (INCLUDING empty) > existing
    launcher/process value > allowlisted root fallback;
  - canonical dotenv parsing (``export`` prefix handled);
  - default-profile and isolated-profile layouts stay safe.
"""

import base64
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import api.profiles as profiles
from api.profiles import get_profile_runtime_env, _parse_dotenv_text

# Ambient vars that could act as launcher/process values or leaks if left set.
_AMBIENT_ENV_KEYS = (
    "SEARXNG_URL",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "FIRECRAWL_GATEWAY_URL",
    "FIRECRAWL_BROWSER_TTL",
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
    named profile — server auth secrets, arbitrary credentials (including the
    password=True FIRECRAWL_API_KEY), and plain non-allowlisted vars stay at
    the root."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "SEARXNG_URL=http://root-searx:8080\n"
        "HERMES_WEBUI_PASSWORD=server-auth-secret\n"
        "OPENAI_API_KEY=«redacted:sk-…»\n"
        "FIRECRAWL_API_KEY=fc-root-secret\n"
        "MY_RANDOM_VAR=not-allowlisted\n"
        "HERMES_WEBUI_ISOLATED_PROFILE=0\n",
    )

    env = get_profile_runtime_env(alpha)

    # The allowlisted operator setting IS shared…
    assert env.get("SEARXNG_URL") == "http://root-searx:8080"
    # …but server auth secrets, arbitrary credentials (incl. FIRECRAWL_API_KEY)
    # and non-allowlisted keys are NOT blanket-inherited, and the isolation
    # posture stays operator-only.
    assert "HERMES_WEBUI_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "FIRECRAWL_API_KEY" not in env
    assert "MY_RANDOM_VAR" not in env
    assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env


def test_allowlisted_firecrawl_config_keys_are_shared_but_key_is_not(tmp_path, monkeypatch):
    """[allowlisted shared variable] The non-secret FIRECRAWL_* operator
    settings (API/GATEWAY URLs, browser TTL — password=False upstream)
    propagate from the root .env into the named profile runtime env, while
    FIRECRAWL_API_KEY (password=True) stays at the root."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "FIRECRAWL_API_URL=http://fc:3002\n"
        "FIRECRAWL_GATEWAY_URL=http://fc-gw:3003\n"
        "FIRECRAWL_BROWSER_TTL=600\n"
        "FIRECRAWL_API_KEY=fc-123\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("FIRECRAWL_API_URL") == "http://fc:3002"
    assert env.get("FIRECRAWL_GATEWAY_URL") == "http://fc-gw:3003"
    assert env.get("FIRECRAWL_BROWSER_TTL") == "600"
    # The Firecrawl credential is classified password=True and must never be
    # blanket-inherited by a named profile (#3961 isolation invariant).
    assert "FIRECRAWL_API_KEY" not in env


def test_launcher_process_value_wins_over_root_fallback(tmp_path, monkeypatch):
    """[launcher precedence] A value the operator exported before the server
    started is present both in the import-time baseline and in the process env;
    the allowlisted root fallback must not override it (the returned dict omits
    the key so the launcher value stands after the merge)."""
    base, alpha = _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("SEARXNG_URL", "http://launcher:8080")
    monkeypatch.setitem(profiles._INITIAL_ENV, "SEARXNG_URL", "http://launcher:8080")
    _write_env(base / ".env", "SEARXNG_URL=http://root:8080\n")

    env = get_profile_runtime_env(alpha)

    assert "SEARXNG_URL" not in env, (
        "root fallback must not override a launcher-provided process value"
    )
    assert os.environ["SEARXNG_URL"] == "http://launcher:8080"


def test_live_process_env_mirror_does_not_mask_root_fallback(tmp_path, monkeypatch):
    """[live env is not a launcher value] A key mirrored into os.environ after
    import (a concurrent turn applying its own env to the process env) must not
    be mistaken for a launcher-provided value: the profile still resolves the
    root layer instead of silently losing the key (#7048)."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(base / ".env", "SEARXNG_URL=http://root:8080\n")
    monkeypatch.setenv("SEARXNG_URL", "http://mirrored-by-another-turn:8080")

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "http://root:8080", (
        "a value another turn mirrored into the process env must not suppress "
        "the root fallback"
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
    'export KEY') — for both exact-allowlist keys, while an exported
    FIRECRAWL_API_KEY credential is still refused."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "export SEARXNG_URL=http://root-export:8080\n"
        "export FIRECRAWL_API_URL=http://fc-export:3002\n"
        "export FIRECRAWL_API_KEY=fc-export-secret\n",
    )

    env = get_profile_runtime_env(alpha)

    assert env.get("SEARXNG_URL") == "http://root-export:8080", (
        "'export KEY=value' must parse as KEY, not 'export KEY'"
    )
    assert env.get("FIRECRAWL_API_URL") == "http://fc-export:3002"
    assert "FIRECRAWL_API_KEY" not in env


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


# ---------------------------------------------------------------------------
# #7048 follow-up: one dotenv grammar for both loaders (WebUI <-> ctl.sh)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CTL_SH = _REPO_ROOT / "ctl.sh"

# Every construct the launcher's loader has to agree on with this parser.
_CTL_PARITY_ENV_TEXT = "\n".join(
    [
        "# a comment line",
        "",
        "export\tPARITY_TAB=after-tab",
        "export PARITY_SPACE=after-space",
        'PARITY_DQ="quoted value"',
        'PARITY_DQ_ESCAPES="line\\nbreak\\tend"',
        'PARITY_DQ_QUOTE="say \\"hi\\""',
        "PARITY_SQ='literal \\n kept'",
        'PARITY_DQ_COMMENT="kept" # trailing comment',
        "PARITY_UNQUOTED=plain # trailing comment",
        "PARITY_UNQUOTED_HASH=a#b",
        "PARITY_EMPTY=",
        'PARITY_EMPTY_DQ=""',
        "PARITY_TRAILING=back\\",
        "UID=1000",
        "PARITY_BAD KEY=whitespace-stripped",
        "1BAD=invalid",
        "no-equals-sign",
    ]
)

_CTL_PARITY_KEYS = (
    "PARITY_TAB",
    "PARITY_SPACE",
    "PARITY_DQ",
    "PARITY_DQ_ESCAPES",
    "PARITY_DQ_QUOTE",
    "PARITY_SQ",
    "PARITY_DQ_COMMENT",
    "PARITY_UNQUOTED",
    "PARITY_UNQUOTED_HASH",
    "PARITY_EMPTY",
    "PARITY_EMPTY_DQ",
    "PARITY_BADKEY",
    "PARITY_TRAILING",
)


def _ctl_sh_loader_source() -> str:
    """Extract ``_apply_env_file_safely`` (and the awk helper it calls) from ctl.sh."""
    source = _CTL_SH.read_text(encoding="utf-8")
    start = source.index("_apply_env_file_safely() {")
    end = source.index("\n}\n", start) + len("\n}\n")
    body = source[start:end]
    if "awk" not in body:  # pragma: no cover - launcher refactor guard
        pytest.skip("ctl.sh loader implementation changed; parity harness needs review")
    return body


def test_parser_grammar_matches_launcher_expectations():
    """[grammar parity] A .env the launcher resolves must resolve identically here."""
    parsed = _parse_dotenv_text(_CTL_PARITY_ENV_TEXT)

    assert parsed["PARITY_TAB"] == "after-tab"
    assert parsed["PARITY_SPACE"] == "after-space"
    assert parsed["PARITY_DQ"] == "quoted value"
    assert parsed["PARITY_DQ_ESCAPES"] == "line\nbreak\tend"
    assert parsed["PARITY_DQ_QUOTE"] == 'say "hi"'
    assert parsed["PARITY_SQ"] == "literal \\n kept"
    assert parsed["PARITY_DQ_COMMENT"] == "kept"
    assert parsed["PARITY_UNQUOTED"] == "plain"
    assert parsed["PARITY_UNQUOTED_HASH"] == "a#b"
    assert parsed["PARITY_TRAILING"] == "back\\"
    # Whitespace inside the key is stripped (launcher: ${key//[[:space:]]/}).
    assert parsed["PARITY_BADKEY"] == "whitespace-stripped"
    # Valid keys keep an empty value instead of being dropped: an empty profile
    # value is what suppresses inheritance from the layer below (#7048).
    assert parsed["PARITY_EMPTY"] == ""
    assert parsed["PARITY_EMPTY_DQ"] == ""
    # Non-key lines and bash read-only identity keys are never exported.
    assert "1BAD" not in parsed
    assert "no-equals-sign" not in parsed
    assert "UID" not in parsed


def test_parser_matches_ctl_sh_env_loader(tmp_path):
    """[cross-loader parity] Run the real ctl.sh loader over the same .env and
    require the same present/absent set and the same byte-exact values."""
    if not _CTL_SH.exists():  # pragma: no cover - repo layout guard
        pytest.skip("ctl.sh not present")

    env_file = tmp_path / "parity.env"
    env_file.write_text(_CTL_PARITY_ENV_TEXT + "\n", encoding="utf-8")

    key_list = " ".join(_CTL_PARITY_KEYS)
    script = f"""set -uo pipefail
{_ctl_sh_loader_source()}
_apply_env_file_safely {shlex.quote(str(env_file))}
for k in {key_list}; do
  if [[ -v $k ]]; then
    printf '%s\\tset\\t%s\\n' "$k" "$(printf %s "${{!k}}" | base64 -w0)"
  else
    printf '%s\\tunset\\t\\n' "$k"
  fi
done
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"ctl.sh loader probe failed: {result.stderr}"

    launcher: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, state, encoded = line.split("\t")
        if state == "set":
            launcher[key] = base64.b64decode(encoded).decode("utf-8")

    parsed = _parse_dotenv_text(_CTL_PARITY_ENV_TEXT)
    here = {k: parsed[k] for k in _CTL_PARITY_KEYS if k in parsed}

    assert here == launcher, (
        "WebUI dotenv parser diverged from ctl.sh::_apply_env_file_safely "
        f"(webui={here!r} launcher={launcher!r})"
    )


def test_reload_dotenv_uses_shared_grammar(tmp_path, monkeypatch):
    """[live-env reload] _reload_dotenv must resolve a .env with the same
    grammar as the root/profile layers: an `export` with a tab used to land as a
    key literally named "export\\tCTL_PARITY_TAB", a quoted value with an inline
    comment kept the quote and the comment, and escapes were left unexpanded."""
    base = tmp_path / ".hermes"
    base.mkdir()
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    for key in (
        "CTL_PARITY_TAB",
        "CTL_PARITY_COMMENT",
        "CTL_PARITY_ESCAPES",
        "CTL_PARITY_BROKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    _write_env(
        base / ".env",
        "export\tCTL_PARITY_TAB=after-tab\n"
        'CTL_PARITY_COMMENT="kept value" # trailing comment\n'
        'CTL_PARITY_ESCAPES="line\\nbreak"\n'
        "CTL_PARITY_BROKEN not a kv line\n",
    )

    profiles._reload_dotenv(base)

    assert os.environ["CTL_PARITY_TAB"] == "after-tab"
    assert os.environ["CTL_PARITY_COMMENT"] == "kept value"
    assert os.environ["CTL_PARITY_ESCAPES"] == "line\nbreak"
    assert "CTL_PARITY_BROKEN" not in os.environ


def test_named_profile_turn_scrubs_root_scope_keys(tmp_path, monkeypatch):
    """[overlapping turns] Only the root .env defines ROOT_SCOPE_KEY; a beta turn
    mirrors its value into the shared process env. During the alpha turn that key
    must be scrubbed (and the pre-turn value handed back for restore) instead of
    leaking root-scoped values into a named-profile turn."""
    base, alpha = _layout(tmp_path, monkeypatch)
    _write_env(
        base / ".env",
        "ROOT_SCOPE_KEY=root-value\n"
        "SEARXNG_URL=http://root:8080\n"
        "BACKEND_API_KEY=root-secret\n"
        "HERMES_HOME=/srv/root\n"
        "PATH=/usr/bin:/bin\n",
    )
    _write_env(alpha / ".env", "ALPHA_KEY=a\n")

    safe_runtime_env = {"ALPHA_KEY": "a", "HERMES_HOME": str(alpha)}
    root_only = profiles._root_only_env_names_for_profile(alpha, safe_runtime_env)

    assert "ROOT_SCOPE_KEY" in root_only
    assert "BACKEND_API_KEY" in root_only  # root credential scope never inherited
    assert "SEARXNG_URL" not in root_only  # allowlisted share keeps being inherited
    assert "HERMES_HOME" not in root_only  # profile-home machinery owns this key
    assert "PATH" not in root_only  # shell identity is never touched
    assert "ALPHA_KEY" not in root_only  # the profile's own key is not root-scoped

    process_env = dict(os.environ)
    process_env["ROOT_SCOPE_KEY"] = "mirrored-by-a-beta-turn"
    process_env["BACKEND_API_KEY"] = "mirrored-by-a-beta-turn"

    previous = profiles._apply_profile_env_to_process(
        process_env,
        safe_runtime_env,
        secret_env_names=set(),
        root_only_env_names=root_only,
    )

    assert "ROOT_SCOPE_KEY" not in process_env
    assert "BACKEND_API_KEY" not in process_env
    assert previous["ROOT_SCOPE_KEY"] == "mirrored-by-a-beta-turn"
    assert previous["BACKEND_API_KEY"] == "mirrored-by-a-beta-turn"
    assert "ALPHA_KEY" in previous  # profile's own key is still snapshotted


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
