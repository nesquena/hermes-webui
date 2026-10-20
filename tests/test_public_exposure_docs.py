"""Binding tests for `docs/public-exposure.md` — hosting the WebUI for others.

The WebUI can be exposed to people who are not you: behind a reverse proxy,
over HTTPS, with a shared password, or with an SSO proxy that asserts the
authenticated user in a trusted header and maps their groups onto Hermes
profiles. Every knob that makes that safe is an environment variable, and
every one of them is read straight from `os.getenv` deep inside the request
path — with no schema, no validation layer, and no config file to check
against.

That makes the documentation the only place where the contract between an
operator and the server is written down. If the docs and the code drift
apart, the failure mode is silent and dangerous: an operator sets
`HERMES_WEBUI_TRUSTED_PROXY_CIDRS` expecting the proxy to be trusted, the
code ignores it, and the WebUI silently falls back to a stricter policy (or,
worse, a broader one) with nothing in the logs to say so.

So this file pins the documentation to the code in both directions:

1. Every `HERMES_WEBUI_*` variable the guide names must actually be read by
   the server. A documented variable that does not exist is a dead promise.
2. Every variable that public exposure depends on must be documented. A
   variable that exists but is undocumented is a footgun with no discoverable
   name.
3. The numbers the guide quotes for the login rate limiter must equal the
   constants in `api/auth.py`. The limiter is the only thing standing between
   a public deployment and a password-guessing script, so "5 attempts per
   60 seconds" is a security-relevant claim, not a cosmetic one.
4. The guide must stay reachable from the README and the contract index —
   an unlinked guide is not documentation.

Reference: upstream issue #8211 asked for a hosted WebUI for a project's
users. The hosted page it pointed at was taken down and the request was
withdrawn, so the useful upstream contribution is the supported
self-hosting path: how to put the WebUI in front of other people safely.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "public-exposure.md"
README = REPO / "README.md"
CONTRACTS = REPO / "docs" / "CONTRACTS.md"
AUTH_PY = REPO / "api" / "auth.py"

# Sources that can read an environment variable at runtime. A variable is
# "real" only if the server actually looks at it.
SOURCE_FILES = (
    "api/auth.py",
    "api/auth_oidc.py",
    "api/config.py",
    "api/routes.py",
    "api/sse_chunked.py",
    "server.py",
    "bootstrap.py",
)

# The variables a public deployment is built on. Grouped by what the
# operator is trying to achieve, in the order the guide introduces them.
REQUIRED_ENV_VARS = {
    # Binding and basic auth
    "HERMES_WEBUI_HOST",
    "HERMES_WEBUI_PORT",
    "HERMES_WEBUI_PASSWORD",
    # Origin / CSRF policy
    "HERMES_WEBUI_ALLOWED_ORIGINS",
    # Forwarded-header trust (proxy in front of the WebUI)
    "HERMES_WEBUI_TRUST_FORWARDED_HOST",
    "HERMES_WEBUI_TRUST_FORWARDED_FOR",
    "HERMES_WEBUI_TRUST_FORWARDED_PROTO",
    "HERMES_WEBUI_TRUSTED_PROXY_CIDRS",
    # TLS termination in the WebUI itself
    "HERMES_WEBUI_TLS_CERT",
    "HERMES_WEBUI_TLS_KEY",
    "HERMES_WEBUI_SECURE",
    # Multi-user: SSO proxy asserts identity in a trusted header
    "HERMES_WEBUI_TRUSTED_AUTH_HEADER",
    "HERMES_WEBUI_TRUSTED_GROUPS_HEADER",
    "HERMES_WEBUI_GROUP_PROFILE_MAP",
    "HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL",
}


def _doc_text() -> str:
    assert DOC.exists(), (
        "docs/public-exposure.md must exist. It is the supported guide for "
        "exposing the WebUI to users other than the operator (reverse proxy, "
        "HTTPS, SSO header auth, per-group profile binding)."
    )
    return DOC.read_text(encoding="utf-8")


def _source_text() -> str:
    chunks = []
    for rel in SOURCE_FILES:
        path = REPO / rel
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8"))
    assert chunks, "no server source files found to bind the documentation against"
    return "\n".join(chunks)


def test_public_exposure_guide_exists_and_documents_the_three_exposure_scopes():
    """The guide must name the three exposure scopes it separates.

    These are the distinctions an operator has to make before anything else:
    who can reach the WebUI at all (tailnet / LAN / public internet), and
    therefore how much trust the transport and the auth layer need.
    """
    text = _doc_text().lower()
    for scope in ("tailscale", "lan", "public"):
        assert scope in text, (
            f"docs/public-exposure.md must cover the {scope!r} exposure scope. "
            "Operators need to know which trust boundary they are choosing."
        )


def test_documented_env_vars_are_read_by_the_server():
    """Every `HERMES_WEBUI_*` name in the guide must exist in the code.

    Guards against documenting a variable that was renamed or never existed:
    operators copy these names verbatim, so a stale name is a silent no-op.
    """
    doc = _doc_text()
    source = _source_text()

    documented = set(re.findall(r"HERMES_WEBUI_[A-Z0-9_]+", doc))
    assert documented, "docs/public-exposure.md must name the environment variables it documents"

    unknown = sorted(v for v in documented if v not in source)
    assert unknown == [], (
        "docs/public-exposure.md documents environment variables that no server "
        f"source file reads: {unknown}. Either the variable was renamed or the "
        "guide documents a knob that does not exist."
    )


def test_required_public_exposure_env_vars_are_documented():
    """Every variable public exposure depends on must be in the guide.

    The inverse of the check above: a variable the server honours but the
    guide never mentions is undiscoverable, and the operator reaches the
    failure the hard way.
    """
    doc = _doc_text()
    source = _source_text()

    missing = sorted(v for v in REQUIRED_ENV_VARS if v not in doc)
    assert missing == [], (
        "docs/public-exposure.md must document these environment variables: "
        f"{missing}. Each is read by the server and each changes the trust "
        "boundary of a public deployment."
    )

    # The required set must not drift into documenting something that does
    # not exist either — the guide and this list have to agree.
    not_real = sorted(v for v in REQUIRED_ENV_VARS if v not in source)
    assert not_real == [], (
        f"these variables are listed as required but are not read by the "
        f"server: {not_real}. Update this test and the guide together."
    )


def test_guide_quotes_the_real_login_rate_limit_numbers():
    """The rate-limiter numbers in the guide must equal the code's constants.

    `api/auth.py` owns the limiter. The guide quotes the same numbers so an
    operator can reason about a public endpoint's exposure. If either side
    moves, this test fails instead of the guide quietly going stale.
    """
    source = AUTH_PY.read_text(encoding="utf-8")

    max_match = re.search(r"^_LOGIN_MAX_ATTEMPTS\s*=\s*(\d+)", source, re.MULTILINE)
    window_match = re.search(r"^_LOGIN_WINDOW\s*=\s*(\d+)", source, re.MULTILINE)
    assert max_match and window_match, (
        "api/auth.py must define _LOGIN_MAX_ATTEMPTS and _LOGIN_WINDOW as "
        "module-level integer constants so the documented numbers can be "
        "bound to the code."
    )
    max_attempts = int(max_match.group(1))
    window_seconds = int(window_match.group(1))

    doc = _doc_text()
    assert str(max_attempts) in doc, (
        f"docs/public-exposure.md must state the login limit of {max_attempts} "
        f"attempts (from api/auth.py _LOGIN_MAX_ATTEMPTS)."
    )
    assert str(window_seconds) in doc, (
        f"docs/public-exposure.md must state the login limit window of "
        f"{window_seconds} seconds (from api/auth.py _LOGIN_WINDOW)."
    )


def test_guide_warns_before_exposing_and_names_the_trusted_proxy_exactness():
    """The guide must carry the two warnings that prevent a public leak.

    1. Set a password (or SSO) BEFORE widening the bind address. The server
       prints a startup warning but still binds.
    2. `HERMES_WEBUI_TRUSTED_PROXY_CIDRS` must list the proxy's exact
       address. Loopback is trusted implicitly; every other network has to
       be named, and a malformed entry is skipped rather than trusted.
    """
    doc = _doc_text()
    lowered = doc.lower()

    assert "hermes_webui_password" in lowered or "sso" in lowered, (
        "docs/public-exposure.md must tell the operator to enable a password "
        "or SSO before binding beyond loopback."
    )
    assert "trusted_proxy_cidrs" in lowered, (
        "docs/public-exposure.md must document HERMES_WEBUI_TRUSTED_PROXY_CIDRS "
        "and the requirement to list the proxy's exact address."
    )
    assert "loopback" in lowered, (
        "docs/public-exposure.md must state that loopback is trusted "
        "implicitly, which is why a same-host proxy needs no CIDR entry."
    )
    assert "exact" in lowered, (
        "docs/public-exposure.md must warn that the trusted-proxy list must "
        "contain the proxy's exact address, not a wide range."
    )


def test_guide_is_reachable_from_the_readme_and_the_contract_index():
    """The guide must be linked from the README and the contract index.

    A guide nobody can find does not answer the question that brought
    someone to the repository in the first place.
    """
    _doc_text()

    readme = README.read_text(encoding="utf-8")
    assert "docs/public-exposure.md" in readme, (
        "README.md must link docs/public-exposure.md so operators looking for "
        "public-exposure guidance find it."
    )

    contracts = CONTRACTS.read_text(encoding="utf-8")
    assert "docs/public-exposure.md" in contracts, (
        "docs/CONTRACTS.md must index docs/public-exposure.md alongside the "
        "other operational references."
    )
