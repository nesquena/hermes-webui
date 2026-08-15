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

import itertools
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


# ── THE grammar table — asserted against BOTH implementations ────────────────
#
# Re-review 2026-08-14 (round 2): the two sides used to be independent
# implementations that measurably disagreed on bracketed hosts — JS accepted any
# `/^[0-9A-Fa-f:.]+$/` inner, so `[dead:beef]:80` was an authority in the picker
# and not on the backend, which is exactly the label/route split this PR exists
# to remove. This single table now drives the Python parametrize below AND the
# node cross-check in `test_endpoint_authority_grammar_js_matches_python`, so the
# two implementations cannot silently drift again on any covered row — and
# `test_endpoint_authority_grammar_js_matches_python_over_corpus` extends that
# agreement to a generated shape space, because a table only proves its rows.
_AUTHORITY_GRAMMAR_CASES = [
    ("llm:8080", True),                  # single-label Docker/LAN name
    ("localhost:11434", True),           # loopback alias
    ("10.8.71.41:8080", True),           # IPv4 literal
    ("proxy.internal:8443", True),       # dotted DNS name
    ("ollama.internal.:8443", True),     # trailing root dot: a legal FQDN
    ("a:80", True),                      # one-character host
    ("[::1]:11434", True),               # bracketed IPv6 literal
    ("[fe80::1%25eth0]:8080", True),     # bracketed IPv6 + zone id
    ("[1:2:3:4:5:6:7:8]:443", True),     # fully spelled-out IPv6
    ("[::ffff:1.2.3.4]:443", True),      # IPv6 with IPv4-mapped tail
    ("[::1.2.3.4]:443", True),           # elision + IPv4 tail, nothing between
    ("::1:11434", False),                # UNBRACKETED IPv6: ambiguous, rejected
    ("[not-ipv6]:8080", False),          # brackets around a non-IPv6 host
    ("[dead:beef]:80", False),           # bracketed, colon-bearing, NOT an address
    ("[:::::]:80", False),               # more than one '::' elision
    ("[1:2:3:4:5:6:7:8:9]:443", False),  # nine hextets
    ("[12345::1]:443", False),           # hextet longer than 4 digits
    ("[::1.2.3.4.5]:443", False),        # five-octet IPv4 tail
    ("[1.2.3.4::]:80", False),           # dotted quad must be LAST, not in the head
    ("[]:80", False),                    # empty brackets
    ("my-key:some-model", False),        # named slug + model, not an authority
    ("gw:0", False),                     # port out of range
    ("gw:65536", False),                 # port out of range
    ("gw:080808", False),                # more than 5 digits
    ("-bad:80", False),                  # hyphen-fenced host
    ("bad-:80", False),
    (".bad:80", False),
    ("host name:80", False),             # whitespace is not a host character
    ("llm", False),                      # no port at all
    (" llm:8080", True),                 # surrounding whitespace is trimmed first
    ("llm:8080\t", True),
]


@pytest.mark.parametrize("rest,expected", _AUTHORITY_GRAMMAR_CASES)
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

# Both node drivers below slice the real `static/ui.js` (a classic script, not a
# module) and eval the pieces they need, so they exercise the shipped source
# rather than a copy of it. One preamble, used by both — the predicate and its
# module-level regexes are what both drivers have in common.
_EXTRACT_PREAMBLE = r"""
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
function extractConst(name) {
  // A single-line top-level `const NAME=...;`, re-spelled as `var`: a direct
  // eval() keeps LEXICAL declarations inside the eval's own scope, while
  // sloppy-mode function declarations leak into the caller's — which is why
  // extractFunc() above needs no such rewrite and this does.
  const re = new RegExp('^const ' + name + '=.*$', 'm');
  const m = ui.match(re);
  if (!m) throw new Error(name + ' not found');
  return m[0].replace(/^const /, 'var ');
}
// The predicate's module-level regexes, in dependency order.
eval(extractConst('_PY_WS_CLASS'));
eval(extractConst('_CUSTOM_SLUG_TRIM_RE'));
eval(extractConst('_CUSTOM_SLUG_HOST_REJECT_RE'));
eval(extractFunc('_customSlugIsEndpointAuthority'));
"""

_DRIVER = _EXTRACT_PREAMBLE + r"""
let _dynamicModelLabels = JSON.parse(process.argv[3]);
let _dynamicProviderIds = JSON.parse(process.argv[5]);
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


_PREDICATE_DRIVER = _EXTRACT_PREAMBLE + r"""
const rests = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(rests.map(r => _customSlugIsEndpointAuthority(r))));
"""


def _js_authority(tmp_path, rests):
    """Run static/ui.js's _customSlugIsEndpointAuthority over `rests` via node.

    The inputs go through a file, not argv: the cross-check corpus below is
    thousands of cases and would blow the command-line length limit.
    """
    driver = tmp_path / "predicate.js"
    driver.write_text(_PREDICATE_DRIVER, encoding="utf-8")
    payload = tmp_path / "rests.json"
    payload.write_text(json.dumps(rests), encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), str(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assert_js_matches_python(tmp_path, rests):
    """Assert both implementations answer identically for every rest in `rests`."""
    js_results = _js_authority(tmp_path, rests)
    py_results = [config._custom_slug_rest_is_endpoint_authority(rest) for rest in rests]
    drift = [
        (r, j, p)
        for r, j, p in zip(rests, js_results, py_results, strict=True)
        if j is not p
    ]
    assert not drift, f"JS/Python grammar drift ({len(drift)} of {len(rests)}): {drift[:20]}"
    return py_results


@requires_node
def test_endpoint_authority_grammar_js_matches_python(tmp_path):
    """ONE grammar, two implementations, asserted to agree row for row.

    The maintainer asked for a single unambiguous grammar rather than mirrored
    heuristics. `static/ui.js` cannot import `api/config.py`, so the enforceable
    form of "single" is this: every row of `_AUTHORITY_GRAMMAR_CASES` must get the
    same answer from both. Round 1 shipped a JS bracketed-host check that was a
    looser approximation (`/^[0-9A-Fa-f:.]+$/`) and disagreed on `[dead:beef]:80`
    and `[:::::]:80`; this test is what makes that class of drift fail CI.
    """
    rests = [rest for rest, _ in _AUTHORITY_GRAMMAR_CASES]
    py_results = _assert_js_matches_python(tmp_path, rests)
    assert py_results == [want for _, want in _AUTHORITY_GRAMMAR_CASES]


def _ipv6_cross_check_corpus():
    """Every 1-3 piece bracketed literal over adversarial atoms and separators.

    A table of hand-picked rows only proves the rows. The bracketed branch is
    where the two implementations diverged twice (round 1: any `[0-9A-Fa-f:.]+`
    inner passed in JS; round 2 of this review: JS read a dotted quad in the HEAD
    of an elided address as an IPv4 tail, so it accepted `[1.2.3.4::]:80` which
    `ipaddress` rejects). Both were shapes no hand-written table happened to
    hold, so the cross-check enumerates the shape space instead.
    """
    atoms = ["", "0", "ffff", "fffff", "g", "1.2.3.4", "1.2.3", "%eth0"]
    inners: set[str] = set()
    for count in (1, 2, 3):
        for parts in itertools.product(atoms, repeat=count):
            for joins in itertools.product((":", "::"), repeat=count - 1):
                inner = "".join(
                    part + join
                    for part, join in zip(parts, (*joins, ""), strict=True)
                )
                inners.update((inner, f"::{inner}", f"{inner}::"))
    return [f"[{inner}]:80" for inner in sorted(inners)]


def _whitespace_cross_check_corpus():
    """Hosts and ports fenced by, or holding, each disputed whitespace code point.

    `str.strip()`/`re \\s` and `String.trim()`/JS `\\s` are NOT the same set:
    U+001C-U+001F and U+0085 are whitespace only to Python, U+FEFF only to JS.
    Leaving each language to its own definition made the two grammars disagree
    (Python called `a\\ufeffb:1` an authority, JS did not), which is why
    `static/ui.js` spells Python's set out in `_PY_WS_CLASS`.
    """
    disputed = (
        "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0"
        "\u1680\u2000\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    )
    rests = []
    for char in disputed:
        rests += [
            f"{char}llm:8080",
            f"llm:8080{char}",
            f"ll{char}m:8080",
            f"llm:80{char}80",
            f"[::1]:11434{char}",
            f"[::{char}1]:11434",
        ]
    return rests


@requires_node
@pytest.mark.parametrize(
    "corpus",
    [
        pytest.param(_ipv6_cross_check_corpus(), id="bracketed-ipv6-shape-space"),
        pytest.param(_whitespace_cross_check_corpus(), id="disputed-whitespace"),
    ],
)
def test_endpoint_authority_grammar_js_matches_python_over_corpus(tmp_path, corpus):
    """The two implementations agree over a whole generated space, not just a table."""
    py_results = _assert_js_matches_python(tmp_path, corpus)
    # Guard against a degenerate corpus that would pass by answering False to
    # everything on both sides.
    assert any(py_results) and not all(py_results)


@requires_node
def test_bracketed_non_ipv6_host_labels_the_same_on_both_sides(tmp_path):
    """End to end for the class that used to diverge: brackets are not enough.

    `[dead:beef]` is colon-bearing and hex-only but is not an IPv6 address, so it
    is NOT an endpoint authority — the #1776 peel applies and the model keeps the
    numeric segment. Backend and picker must say the same thing, or the label
    reads `qwen3` while the request routes to model `80:qwen3`.
    """
    cases = ["@custom:[dead:beef]:80:qwen3", "@custom:[not-ipv6]:8080:qwen3"]
    assert _labels(tmp_path, {}, cases) == ["80:qwen3", "8080:qwen3"]
    for model_id, want_model, want_provider in (
        ("@custom:[dead:beef]:80:qwen3", "80:qwen3", "custom:[dead:beef]"),
        ("@custom:[not-ipv6]:8080:qwen3", "8080:qwen3", "custom:[not-ipv6]"),
    ):
        model, provider, _ = _resolve_with_cfg(model_id, provider="openai")
        assert (model, provider) == (want_model, want_provider)


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
