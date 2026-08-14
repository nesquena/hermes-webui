"""Shared qualified-ID grammar for @custom: model labels (deep-review 2026-08-13).

PR #6657 defect 2: `getModelLabel()` used an unconditional colon split, which
misparsed a supported host-port provider ID such as `@custom:localhost:1234:qwen3`
into `1234:qwen3`. The fallback now mirrors api/config.py's
`_parse_provider_qualified_model_id` grammar (rsplit + endpoint-authority-aware),
while the operator-supplied catalog label always wins when present.

Re-review 2026-08-14: the endpoint-authority predicate used to demand an IP
literal, `localhost`, or a dotted name, so it rejected the producer's own
single-label output — `_custom_endpoint_slugs_for_base_url("http://llm:8080/v1")`
emits `custom:llm:8080`, and `@custom:llm:8080:qwen3` then mis-peeled into
provider `custom:llm` with model `8080:qwen3`. Both halves of this file assert the
producer→parser chain end to end, plus explicit IPv6 behavior and the
authoritative longest-known-provider-prefix path that resolves the one shape the
grammar alone cannot disambiguate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api.config as config

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")


# ── Backend: producer → parser, on the production resolve path ───────────────


def _resolve_with_cfg(model_id, *, provider="custom", base_url=None, custom_providers=None):
    """Call resolve_model_provider() against a patched cfg snapshot."""
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg["model"] = {"provider": provider}
    if base_url:
        config.cfg["model"]["base_url"] = base_url
    config.cfg["custom_providers"] = custom_providers or []
    try:
        return config.resolve_model_provider(model_id)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def test_single_label_host_port_slug_is_producible():
    """The producer emits `custom:llm:8080` for a single-label Docker/LAN host."""
    slugs = config._custom_endpoint_slugs_for_base_url("http://llm:8080/v1")
    assert "custom:llm:8080" in slugs


def test_single_label_host_port_slug_parses_as_one_provider():
    """Re-review gap 1: `custom:llm:8080` must not peel into `custom:llm`."""
    model, provider, _ = _resolve_with_cfg(
        "@custom:llm:8080:qwen3", base_url="http://llm:8080/v1"
    )
    assert provider == "custom:llm:8080", (
        f"single-label host:port slug must stay whole, got {provider!r}"
    )
    assert model == "qwen3", f"model must not absorb the port, got {model!r}"


def test_single_label_host_port_slug_keeps_model_tag():
    """The tagged-model variant: only the port colon belongs to the provider."""
    model, provider, _ = _resolve_with_cfg(
        "@custom:llm:8080:qwen3:free", base_url="http://llm:8080/v1"
    )
    assert provider == "custom:llm:8080"
    assert model == "qwen3:free"


def test_single_label_host_port_slug_parses_without_matching_config():
    """The grammar is self-contained: no configured endpoint is required."""
    model, provider, _ = _resolve_with_cfg("@custom:llm:8080:qwen3", provider="openai")
    assert provider == "custom:llm:8080"
    assert model == "qwen3"


@pytest.mark.parametrize(
    "rest,expected",
    [
        ("llm:8080", True),               # single-label Docker/LAN name
        ("localhost:11434", True),        # loopback alias
        ("10.8.71.41:8080", True),        # IPv4 literal
        ("proxy.internal:8443", True),    # dotted DNS name
        ("a:80", True),                   # one-character host
        ("[::1]:11434", True),            # bracketed IPv6 literal
        ("[fe80::1%25eth0]:8080", True),  # bracketed IPv6 + zone id
        ("::1:11434", False),             # UNBRACKETED IPv6: ambiguous, rejected
        ("[not-ipv6]:8080", False),       # brackets around a non-IPv6 host
        ("my-key:some-model", False),     # named slug + model, not an authority
        ("gw:0", False),                  # port out of range
        ("gw:65536", False),              # port out of range
        ("gw:080808", False),             # more than 5 digits
        ("-bad:80", False),               # hyphen-fenced host
        ("bad-:80", False),
        (".bad:80", False),
        ("host name:80", False),          # whitespace is not a host character
        ("llm", False),                   # no port at all
    ],
)
def test_endpoint_authority_grammar(rest, expected):
    """One grammar, asserted directly: host shape is not a name heuristic."""
    assert config._custom_slug_rest_is_endpoint_authority(rest) is expected


def test_ipv6_bracketed_form_is_the_parseable_spelling():
    """IPv6, explicitly: the producer emits the bracketed form and it parses."""
    slugs = config._custom_endpoint_slugs_for_base_url("http://[::1]:11434/v1")
    assert "custom:[::1]:11434" in slugs
    model, provider, _ = _resolve_with_cfg(
        "@custom:[::1]:11434:qwen3", base_url="http://[::1]:11434/v1"
    )
    assert provider == "custom:[::1]:11434"
    assert model == "qwen3"


def test_ipv6_bracketed_form_keeps_model_tag():
    model, provider, _ = _resolve_with_cfg(
        "@custom:[::1]:11434:qwen3:free", base_url="http://[::1]:11434/v1"
    )
    assert provider == "custom:[::1]:11434"
    assert model == "qwen3:free"


def test_ipv6_unbracketed_form_is_not_part_of_the_grammar():
    """Defined behavior for the ambiguous spelling: `::1:11434` is itself a valid
    IPv6 address, so `custom:::1:11434` cannot be split reliably. It is rejected
    by the grammar and left to the peel — documented, not silently guessed. The
    unbracketed spelling stays in the producer's set for legacy MATCHING only."""
    assert config._custom_slug_rest_is_endpoint_authority("::1:11434") is False
    slugs = config._custom_endpoint_slugs_for_base_url("http://[::1]:11434/v1")
    assert "custom:::1:11434" in slugs, "legacy spelling still matches"
    model, provider, _ = _resolve_with_cfg("@custom:::1:11434:qwen3", provider="openai")
    assert provider == "custom:::1"
    assert model == "11434:qwen3"


def test_configured_named_provider_beats_numeric_model_shape():
    """The residual shape ambiguity is resolved by config authority, not luck.

    `@custom:gw:8080:free` is shape-ambiguous: `gw:8080` is a well-formed
    authority, yet `8080:free` is a well-formed tagged model. A configured
    `custom:gw` provider makes the named reading authoritative.
    """
    model, provider, _ = _resolve_with_cfg(
        "@custom:gw:8080:free",
        provider="custom:gw",
        base_url="https://gw.example.com/v1",
        custom_providers=[{"name": "gw", "base_url": "https://gw.example.com/v1"}],
    )
    assert provider == "custom:gw"
    assert model == "8080:free"


def test_configured_endpoint_beats_named_slug_for_same_shape():
    """Same ID, opposite config: a real `http://gw:8080` endpoint wins instead."""
    model, provider, _ = _resolve_with_cfg(
        "@custom:gw:8080:free",
        provider="custom",
        base_url="http://gw:8080/v1",
        custom_providers=[{"name": "gw", "base_url": "http://gw:8080/v1"}],
    )
    assert provider == "custom:gw:8080"
    assert model == "free"


# ── Hostile config: a malformed base_url must not break routing ──────────────

# Two unguarded `urlparse` sites sit on the resolve path and raise for these:
# `.port` in `_custom_endpoint_slugs_for_base_url` (reached by the new
# longest-known-prefix pass, which walks EVERY configured base_url, so one bad
# entry anywhere made every `@custom:` id with >=3 colons raise) and `urlparse`
# itself in `_normalize_base_url_for_match` (a PRE-EXISTING crash on
# upstream/master, reproduced there for `model.base_url = "http://[::1/v1"`).
_MALFORMED_BASE_URLS = [
    "http://gw:notaport/v1",   # port is not an integer -> .port raises
    "http://gw:99999999/v1",   # port out of range -> .port raises
    "http://[::1/v1",          # unterminated bracket -> urlparse itself raises
    "http://a]b/v1",           # stray closing bracket -> urlparse itself raises
    "http://[::1]:notaport/",  # bracketed host, bad port -> .port raises
]


@pytest.mark.parametrize("bad_url", _MALFORMED_BASE_URLS)
def test_malformed_base_url_derives_no_slugs_instead_of_raising(bad_url):
    """The producer answers "matches nothing" for an unparseable authority."""
    assert config._custom_endpoint_slugs_for_base_url(bad_url) == set()


@pytest.mark.parametrize("bad_url", _MALFORMED_BASE_URLS)
def test_malformed_base_url_normalizes_instead_of_raising(bad_url):
    """The match-normalizer degrades to the raw URL rather than throwing."""
    assert config._normalize_base_url_for_match(bad_url) == bad_url.rstrip("/").lower()


def test_unparseable_base_urls_stay_distinct_when_compared():
    """Fail-closed, not fail-open: the #3837 probe-key gate compares two
    normalized base URLs with no emptiness guard, so unparseable URLs must not
    all collapse to one value that makes any two of them look identical."""
    normalized = [config._normalize_base_url_for_match(u) for u in _MALFORMED_BASE_URLS]
    assert all(normalized), "an unparseable URL must not normalize to a blank"
    assert len(set(normalized)) == len(normalized), "distinct URLs must stay distinct"
    assert config._normalize_base_url_for_match(
        "http://[::1/v1"
    ) != config._normalize_base_url_for_match("http://[::2/v1")


@pytest.mark.parametrize("bad_url", _MALFORMED_BASE_URLS)
def test_malformed_configured_base_url_does_not_break_qualified_id_parsing(bad_url):
    """A bad `custom_providers[].base_url` must not 500 unrelated model routing."""
    model, provider, _ = _resolve_with_cfg(
        "@custom:llm:8080:qwen3",
        custom_providers=[{"name": "gw", "base_url": bad_url}],
    )
    assert provider == "custom:llm:8080"
    assert model == "qwen3"


@pytest.mark.parametrize("bad_url", _MALFORMED_BASE_URLS)
def test_malformed_active_model_base_url_does_not_break_qualified_id_parsing(bad_url):
    """Same for the active `model.base_url`, the other source of endpoint slugs."""
    model, provider, _ = _resolve_with_cfg("@custom:llm:8080:qwen3", base_url=bad_url)
    assert provider == "custom:llm:8080"
    assert model == "qwen3"


def test_one_malformed_base_url_does_not_hide_its_valid_siblings():
    """Degrade per-entry, not per-config: the good endpoint still wins the prefix
    pass even when an unrelated provider carries an unparseable base_url."""
    model, provider, _ = _resolve_with_cfg(
        "@custom:gw:8080:free",
        base_url="http://gw:8080/v1",
        custom_providers=[
            {"name": "broken", "base_url": "http://gw:notaport/v1"},
            {"name": "gw", "base_url": "http://gw:8080/v1"},
        ],
    )
    assert provider == "custom:gw:8080"
    assert model == "free"


# ── Frontend: getModelLabel() fallback mirrors the same grammar ──────────────

_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  // Top-level functions in ui.js start at column 0 and close with a bare '}'
  // at column 0. The brace-counter extractor used by other tests is unsafe
  // here: getModelLabel's body contains '{' inside strings, which makes the
  // counter swallow the neighbouring functions.
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const lineStart = ui.lastIndexOf('\n', start) + 1;
  const lines = ui.slice(lineStart).split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    out.push(lines[i]);
    if (i > 0 && lines[i] === '}') break;
  }
  return out.join('\n');
}
let _dynamicModelLabels = JSON.parse(process.argv[3]);
let _dynamicProviderIds = JSON.parse(process.argv[5]);
eval(extractFunc('_customSlugIsEndpointAuthority'));
eval(extractFunc('_customModelFromQualifiedId'));
eval(extractFunc('getModelLabel'));
const cases = JSON.parse(process.argv[4]);
const result = cases.map(c => getModelLabel(c));
process.stdout.write(JSON.stringify(result));
"""


def _labels(tmp_path, dynamic_labels, cases, provider_ids=None):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [
            NODE,
            str(driver),
            str(UI_JS_PATH),
            json.dumps(dynamic_labels),
            json.dumps(cases),
            json.dumps(provider_ids or {}),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@requires_node
def test_qualified_id_grammar_without_dynamic_labels(tmp_path):
    """Dynamic labels absent: the fallback mirrors the backend grammar."""
    cases = [
        "@custom:my-key:some-model:free",                       # named provider + colon tag
        "@custom:localhost:1234:qwen3",                         # host-port endpoint slug
        "@custom:10.8.71.41:8080:Qwen3",                        # IPv4 endpoint slug
        "@custom:claude-code:us.anthropic.claude-opus-4-5-1-v1:0",  # namespaced id + :0 tail
        "@custom:ai_gateway/Qwen3.6-35B-A3B",                   # legacy slash form
        "@custom:qwen397b-64k",                                 # bare model, no separator
    ]
    expected = [
        "some-model:free",
        "qwen3",
        "Qwen3",
        "us.anthropic.claude-opus-4-5-1-v1:0",
        "Qwen3.6-35B-A3B",
        "qwen397b-64k",
    ]
    assert _labels(tmp_path, {}, cases) == expected


@requires_node
def test_single_label_host_port_id_labels_without_dynamic_labels(tmp_path):
    """Re-review gap 1, frontend half: `custom:llm:8080` is one provider, so the
    label is `qwen3` — never `8080:qwen3`."""
    cases = ["@custom:llm:8080:qwen3", "@custom:llm:8080:qwen3:free"]
    assert _labels(tmp_path, {}, cases) == ["qwen3", "qwen3:free"]


@requires_node
def test_ipv6_ids_label_without_dynamic_labels(tmp_path):
    """IPv6, explicitly: the bracketed spelling parses; the ambiguous
    unbracketed one degrades to the documented peel instead of guessing."""
    cases = [
        "@custom:[::1]:11434:qwen3",
        "@custom:[::1]:11434:qwen3:free",
        "@custom:::1:11434:qwen3",
    ]
    assert _labels(tmp_path, {}, cases) == ["qwen3", "qwen3:free", "11434:qwen3"]


@requires_node
def test_authoritative_provider_id_resolves_shape_ambiguity(tmp_path):
    """Server-reported provider_id metadata beats the shape grammar, mirroring
    the backend's longest-known-provider-prefix pass."""
    ambiguous = "@custom:gw:8080:free"
    # Shape alone reads `gw:8080` as an authority.
    assert _labels(tmp_path, {}, [ambiguous]) == ["free"]
    # /api/models said the provider is `custom:gw`, so the tail is the model.
    assert _labels(tmp_path, {}, [ambiguous], {"custom:gw": True}) == ["8080:free"]
    # …and when it says `custom:gw:8080`, the authority reading is confirmed.
    assert _labels(tmp_path, {}, [ambiguous], {"custom:gw:8080": True}) == ["free"]


@requires_node
def test_bare_custom_group_id_does_not_swallow_the_whole_slug(tmp_path):
    """The unnamed `custom` group's provider_id prefixes every `@custom:` id, so
    matching it would return the slug itself as the label. Only a slug that names
    something may win the prefix pass."""
    provider_ids = {"custom": True}
    cases = ["@custom:localhost:1234:qwen3", "@custom:my-key:some-model:free"]
    assert _labels(tmp_path, {}, cases, provider_ids) == ["qwen3", "some-model:free"]


@requires_node
def test_dynamic_catalog_label_wins_when_present(tmp_path):
    """Dynamic labels present: the operator-supplied catalog label is returned
    verbatim, no id parsing involved."""
    labels = {"@custom:my-key:some-model:free": "Meu Modelo Free"}
    cases = ["@custom:my-key:some-model:free", "@custom:localhost:1234:qwen3"]
    assert _labels(tmp_path, labels, cases) == ["Meu Modelo Free", "qwen3"]
