"""Dotted Bedrock/Vertex model-label normalization (split out of PR #6607).

``us.anthropic.claude-opus-5`` carries a cross-region routing prefix and a vendor
namespace. Left intact both survive into the human label, so the turn footer
rendered "Us.anthropic.claude Opus 5".

The normalization is implemented twice — inlined in ``_get_label_for_model()``
(api/config.py) and as ``_stripDottedModelPrefix()`` (static/ui.js) — so these
tests are PAIRED: one table drives both sides, and a divergence fails.

The allow-list is closed on purpose. A generic "drop leading letters-only dot
segments" loop rewrote arbitrary uncatalogued IDs (``deepseek.v3`` → "V3",
``foo.bar.baz`` → "BAZ"); those cases are pinned below.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.config import _get_label_for_model  # noqa: E402

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _derive_set_from_config(marker: str) -> set[str]:
    """Read an allow-list out of PRODUCTION source instead of retyping it.

    A retyped copy silently stops matching when the real set grows -- which is
    exactly how ``global`` and then ``luma``/``twelvelabs``/``ibm`` shipped
    mislabeled. Same technique as test_every_catalog_dotted_id_loses_its_routing_prefix.
    """
    import re as _re
    config_src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    start = config_src.index(marker)
    body = config_src[start:config_src.index("}", start)]
    found = {m.lower() for m in _re.findall(r'"([a-z0-9-]+)"', body)}
    assert found, f"could not derive {marker!r} from api/config.py"
    return found


_PRODUCTION_REGIONS = _derive_set_from_config("_regions = {")

# (model_id, expected_normalized_id) — what the dotted-prefix step must leave
# behind, BEFORE the shared cosmetic title-casing that both sides apply after.
STRIP_CASES = [
    # --- documented Bedrock/Vertex shapes: prefix is plumbing --------------
    ("us.anthropic.claude-opus-5", "claude-opus-5"),
    ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "claude-sonnet-4-5-20250929-v1"),
    ("apac.anthropic.claude-haiku-4", "claude-haiku-4"),
    # ``global`` is a routing head the catalog actually ships (six
    # ``global.anthropic.claude-*`` IDs at api/config.py:1901-1909, and the
    # first-party routing notes use it as the canonical Bedrock shape). Omitting
    # it from the region set left every one of those labels reading
    # "Global.anthropic.claude Opus 4 7".
    ("global.anthropic.claude-opus-4-7", "claude-opus-4-7"),
    ("global.anthropic.claude-opus-4-6-v1", "claude-opus-4-6-v1"),
    ("global.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("global.anthropic.claude-opus-4-5-20251101-v1:0",
     "claude-opus-4-5-20251101-v1"),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "claude-sonnet-4-5-20250929-v1"),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0",
     "claude-haiku-4-5-20251001-v1"),
    ("us-gov.anthropic.claude-opus-5", "claude-opus-5"),
    ("mistral.mistral-large-2407-v1:0", "mistral-large-2407-v1"),
    ("amazon.nova-pro-v1:0", "nova-pro-v1"),
    ("meta.llama3-70b-instruct-v1:0", "llama3-70b-instruct-v1"),
    # --- must be left BYTE-INTACT ------------------------------------------
    # Vendor is the whole name: stripping it would render the model as "V3".
    ("deepseek.v3", "deepseek.v3"),
    # Uncatalogued vendor: not our shape, do not touch.
    ("foo.bar.baz", "foo.bar.baz"),
    ("acme.super-model-9", "acme.super-model-9"),
    # A known region head with an unknown vendor is not our shape either.
    ("us.foo.bar", "us.foo.bar"),
    # Version dots must survive.
    ("gpt-4.1", "gpt-4.1"),
    ("qwen3.6-35b", "qwen3.6-35b"),
    ("o1.5-preview", "o1.5-preview"),
    # URI-scheme IDs are paths, not dotted namespaces.
    ("https://host/v1.2/model", "https://host/v1.2/model"),
    # No dot at all.
    ("claude-opus-5", "claude-opus-5"),
]

# End-to-end labels through the real backend function.
LABEL_CASES = [
    ("us.anthropic.claude-opus-5", "Claude Opus 5"),
    ("mistral.mistral-large-2407-v1:0", "Mistral Large 2407 V1"),
    ("gpt-4.1", "GPT 4.1"),
    ("qwen3.6-35b", "Qwen3.6 35B"),
]


@pytest.mark.parametrize("model_id,expected", LABEL_CASES)
def test_backend_label_drops_dotted_plumbing(model_id, expected):
    assert _get_label_for_model(model_id, []) == expected


@pytest.mark.parametrize("model_id", [
    "deepseek.v3", "foo.bar.baz", "acme.super-model-9", "us.foo.bar",
])
def test_backend_label_keeps_uncatalogued_vendor_name(model_id):
    """The vendor word must not be silently deleted from an unknown ID."""
    vendor = model_id.split(".")[0]
    label = _get_label_for_model(model_id, [])
    assert vendor.lower() in label.lower(), (
        f"{model_id!r} lost its vendor: label={label!r}"
    )


def _js_strip(model_ids: list[str]) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    def extract(src: str, name: str) -> str:
        start = src.index(f"function {name}(")
        depth = 0
        started = False
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
                started = True
            elif src[i] == "}":
                depth -= 1
                if started and depth == 0:
                    return src[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    # The two Sets the helper closes over are declared immediately above it.
    sets_start = UI_JS.index("const _BEDROCK_REGION_PREFIXES")
    sets_end = UI_JS.index("function _stripDottedModelPrefix")
    script = "\n".join([
        UI_JS[sets_start:sets_end],
        extract(UI_JS, "_stripDottedModelPrefix"),
        "const ids = JSON.parse(process.argv[1]);",
        "console.log(JSON.stringify(ids.map(_stripDottedModelPrefix)));",
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(model_ids)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


def test_frontend_strip_matches_the_table():
    """The JS half must normalize exactly as specified — same table."""
    ids = [c[0] for c in STRIP_CASES]
    js = _js_strip(ids)
    for (model_id, expected), js_out in zip(STRIP_CASES, js, strict=True):
        assert js_out == expected, (
            f"js drifted for {model_id!r}: js={js_out!r} expected={expected!r}"
        )


def _js_model_labels(model_ids: list[str]) -> dict[str, str]:
    """Drive the REAL ``getModelLabel()`` from static/ui.js under Node.

    ``_js_strip()`` above exercises only ``_stripDottedModelPrefix()``. That is
    the wrong unit for a parity claim: ``getModelLabel()`` applies a whole retry
    chain AFTER the strip (``_dynamicModelLabels`` → ``STATIC_LABELS`` →
    ``'anthropic/' + id`` → a ``claude-`` prettifier). A test that stops at the
    strip cannot observe any of it.

    Only SINKS are stubbed (``_dynamicModelLabels`` starts empty, exactly as it
    is before a catalog fetch; ``_fmtOllamaLabel`` is identity). Every decision
    function is the shipped source, eval'd — so drift fails here instead of
    rendering wrong in the picker.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", _GET_MODEL_LABEL_DRIVER, str(REPO_ROOT / "static" / "ui.js"),
         json.dumps(model_ids)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


# Reuses the boundary-slicing approach already proven in
# tests/test_issue3429_uri_scheme_model_label.py: regex literals inside
# getModelLabel() defeat a naive brace counter, so bound it by the next
# top-level function instead.
_GET_MODEL_LABEL_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
const start = ui.indexOf('function getModelLabel(');
if (start < 0) throw new Error('getModelLabel not found');
const after = ui.indexOf('\nfunction _gatewayProviderName(', start);
if (after < 0) throw new Error('getModelLabel end boundary not found');
// Sinks only: no catalog has been fetched yet, so dynamic labels are empty.
const _dynamicModelLabels = {};
function _fmtOllamaLabel(s){ return s; }
// The dotted normalizer and the two Sets it closes over sit just above
// getModelLabel(); without them the eval'd function ReferenceErrors.
const _stripStart = ui.indexOf('const _BEDROCK_REGION_PREFIXES');
if (_stripStart < 0 || _stripStart > start) throw new Error('strip block not found');
eval(ui.slice(_stripStart, start));
eval(ui.slice(start, after));
const out = {};
for (const m of JSON.parse(process.argv[2])) out[m] = getModelLabel(m);
process.stdout.write(JSON.stringify(out));
"""


def test_frontend_and_backend_agree_that_no_namespace_survives():
    """The real contract, driven through the real ``getModelLabel()``.

    NOT string equality of the two labels. The frontend and the backend are
    deliberately different formatters and always have been: on the unmodified
    base commit ``claude-opus-5`` (no dot, untouched by this change) already
    labels as ``'claude-opus-5'`` in JS and ``'Claude Opus 5'`` in Python, and
    ``openai/gpt-4o`` as ``'GPT-4o'`` vs ``'GPT 4O'``. Asserting equality would
    fail on pre-existing behavior this PR does not touch.

    The reason that divergence is not a bug: ``getModelLabel()`` consults
    ``_dynamicModelLabels`` FIRST (static/ui.js:6991), and that map is populated
    from the server's own ``label`` field (static/ui.js:3483, 3494, 3606). Once a
    catalog is loaded the backend label wins in the UI. The JS formatter is the
    pre-fetch fallback, so its job is to be no WORSE than the backend, not
    byte-identical to it.

    What must hold on both sides — the actual defect this PR fixes — is that no
    routing/vendor namespace leaks into a human label.
    """
    ids = [c[0] for c in STRIP_CASES]
    js_labels = _js_model_labels(ids)

    for model_id, expected_stripped in STRIP_CASES:
        js_label = js_labels[model_id]
        py_label = _get_label_for_model(model_id, [])

        # A namespace head is only "leaked" if it was supposed to be stripped.
        # Cases pinned as byte-intact (deepseek.v3, foo.bar.baz, gpt-4.1) must
        # KEEP their dotted head, so they are checked in the opposite direction:
        # the head must still be present rather than silently deleted.
        if expected_stripped == model_id:
            if "." not in model_id:
                # No dot at all (``claude-opus-5``): the dotted-prefix step is
                # not involved. Nothing for this test to assert beyond the strip
                # being a no-op.
                assert _js_strip([model_id]) == [model_id], (
                    f"dotted strip must be a no-op for {model_id!r}"
                )
                continue
            if "://" in model_id:
                # URI-scheme IDs are paths, not dotted namespaces.
                # getModelLabel() deliberately renders the last meaningful path
                # segment (the #3429 fix, pinned in
                # tests/test_issue3429_uri_scheme_model_label.py), so a full-URL
                # assertion here would contradict that test. What matters for
                # THIS PR is only that the dotted-prefix strip left it alone.
                assert _js_strip([model_id]) == [model_id], (
                    f"dotted strip must not touch the URI-scheme id {model_id!r}"
                )
                continue
            head = model_id.split(".", 1)[0].lower()
            # Only a LETTERS-ONLY head is a namespace candidate. ``gpt-4.1``,
            # ``qwen3.6-35b`` and ``o1.5-preview`` carry a VERSION dot, so their
            # "head" (``gpt-4``) is not a namespace and title-casing legitimately
            # reshapes it ("GPT 4.1"). What must hold for those is that the dot
            # itself survives -- asserted directly below.
            if head.replace("-", "").isalpha():
                assert head in js_label.lower(), (
                    f"{model_id!r} is pinned byte-intact but the frontend deleted "
                    f"its head {head!r}: {js_label!r}"
                )
                assert head in py_label.lower(), (
                    f"{model_id!r} is pinned byte-intact but the backend deleted "
                    f"its head {head!r}: {py_label!r}"
                )
            else:
                assert "." in js_label and "." in py_label, (
                    f"{model_id!r} lost its version dot: js={js_label!r} "
                    f"py={py_label!r}"
                )
            continue

        head = model_id.split(".", 1)[0].lower()
        for side, label in (("frontend", js_label), ("backend", py_label)):
            assert f"{head}." not in label.lower(), (
                f"{side} leaked the routing namespace for {model_id!r}: {label!r}"
            )
            # A REGION head must never survive as a leading word either
            # ("Us anthropic claude Opus 5"). This check does NOT apply to a
            # vendor head, because a vendor name legitimately reappears inside
            # some model names -- ``mistral.mistral-large-2407-v1:0`` correctly
            # labels as "Mistral Large 2407 V1". Same rule as
            # test_known_bedrock_vendors_are_all_covered below.
            if head in _PRODUCTION_REGIONS:
                assert not label.lower().startswith(f"{head} "), (
                    f"{side} kept the region head as a word for {model_id!r}: {label!r}"
                )


def test_frontend_retry_chain_reaches_the_static_label_table():
    """Pin the behavior that makes the JS fallback WORTH having.

    This is the post-strip chain the previous test could not see. Removing the
    retry block at static/ui.js:7053-7071 leaves these rendering as raw IDs
    (``'claude-sonnet-4-5'`` instead of ``'Sonnet 4.5'``) — verified by excising
    the block and re-running this driver.
    """
    cases = {
        # Lands on STATIC_LABELS['anthropic/claude-sonnet-4-5'] after the strip.
        "us.anthropic.claude-sonnet-4-5": "Sonnet 4.5",
        "eu.anthropic.claude-haiku-3-5": "Haiku 3.5",
        # No table entry: the claude- prettifier drops the vendor word, the
        # -YYYYMMDD date pin and the -v1 revision.
        "global.anthropic.claude-opus-4-5-20251101-v1:0": "Opus 4 5",
        "us-gov.anthropic.claude-opus-5": "Opus 5",
    }
    got = _js_model_labels(list(cases))
    for model_id, expected in cases.items():
        assert got[model_id] == expected, (
            f"frontend label drift for {model_id!r}: {got[model_id]!r} != {expected!r}"
        )
    for label in got.values():
        assert "claude-" not in label.lower(), f"vendor word survived: {label!r}"


def test_every_catalog_dotted_id_loses_its_routing_prefix():
    """Catalog-driven guard against region/vendor-set drift.

    The allow-lists and the shipped catalog are two lists that must agree. They
    didn't, twice: first ``global`` was missing (six IDs mislabeled), then
    ``luma``/``twelvelabs``/``ibm`` were missing (real Bedrock vendors rendering
    as "Us.luma.ray 2").

    An earlier version of this test scraped only three-segment
    ``<region>.<vendor>.<model>`` literals and therefore inspected **6** of the
    **75** dotted catalog IDs — reassuring, but nearly blind. This version:

    - scrapes ANY quoted ``id`` value (single or double quotes, any segment count);
    - derives the offending prefixes from the PRODUCTION allow-lists rather than a
      retyped copy, so a set that grows without test updates is still covered;
    - skips version dots (``qwen3.6-plus``, ``gpt-5.4``), which are not namespaces.
    """
    import re as _re

    config_src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")

    # Derive the real allow-lists out of production source, don't retype them.
    def _set_literal(marker: str) -> set[str]:
        start = config_src.index(marker)
        body = config_src[start:config_src.index("}", start)]
        return {m.lower() for m in _re.findall(r'"([a-z0-9-]+)"', body)}

    regions = _set_literal("_regions = {")
    vendors = _set_literal("_vendors = {")
    assert regions and vendors, "could not derive allow-lists from api/config.py"
    namespace_heads = regions | vendors

    ids = {
        i for i in _re.findall(r"""['"]id['"]\s*:\s*['"]([^'"]+)['"]""", config_src)
        if "." in i
    }
    assert len(ids) > 20, f"catalog scrape found only {len(ids)} dotted ids"

    offenders = []
    for model_id in sorted(ids):
        head = model_id.split(".")[0].lower()
        # Only IDs whose head is a KNOWN namespace should be stripped; a version
        # dot such as `qwen3.6-plus` has no namespace head and is left alone.
        if head not in namespace_heads:
            continue
        label = _get_label_for_model(model_id, [])
        if head in label.lower().replace(" ", "."):
            offenders.append((model_id, label))

    assert not offenders, (
        "catalog IDs still carry a routing/vendor prefix in their label — add the "
        f"missing head to the region/vendor sets: {offenders}"
    )


def test_known_bedrock_vendors_are_all_covered():
    """Real Bedrock foundation-model vendors must all be in the allow-list.

    These were shipping mislabeled: `luma.ray-2` rendered as "Luma.ray 2",
    `us.twelvelabs.marengo-embed-2-7` as "Us.twelvelabs.marengo Embed 2 7".

    The assertion is that no DOTTED NAMESPACE survives — not that the vendor word
    is absent, because a vendor legitimately reappears inside some model names
    (``mistral.mistral-large-2407`` → "Mistral Large 2407").
    """
    for model in [
        "luma.ray-2",
        "twelvelabs.marengo-embed-2-7",
        "ibm.granite-3-8b-instruct",
        "anthropic.claude-opus-5",
        "amazon.nova-pro-v1:0",
        "mistral.mistral-large-2407-v1:0",
    ]:
        head = model.split(".")[0]
        label = _get_label_for_model(model, [])
        assert f"{head}." not in label.lower(), (
            f"{model!r} kept its vendor namespace: {label!r}"
        )
        # And with a region prefix in front.
        regional = f"us.{model}"
        rlabel = _get_label_for_model(regional, [])
        assert "us." not in rlabel.lower() and f"{head}." not in rlabel.lower(), (
            f"{regional!r} kept its namespace: {rlabel!r}"
        )



def test_global_region_is_recognized_in_both_implementations():
    """Explicit pin for the reported gap, both sides."""
    label = _get_label_for_model("global.anthropic.claude-opus-4-7", [])
    assert "global" not in label.lower(), label
    assert "anthropic" not in label.lower(), label
    assert _js_strip(["global.anthropic.claude-opus-4-7"]) == [
        "claude-opus-4-7"
    ]


def test_frontend_helper_exists_and_is_used():
    assert "function _stripDottedModelPrefix" in UI_JS
    assert "_stripDottedModelPrefix(_last)" in UI_JS
    # The generic letters-only loop must be gone.
    assert "/^[a-z]+$/i.test(_segs[_i]" not in UI_JS


def test_backend_uses_a_closed_allow_list():
    """Guard the design: the fix must not regress to a generic prefix loop."""
    config_src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    idx = config_src.index("def _get_label_for_model")
    block = config_src[idx:idx + 4000]
    assert "_regions" in block and "_vendors" in block, (
        "the dotted-prefix strip must be gated on explicit provider allow-lists"
    )
    assert "while _i < len(_segs) - 1 and _segs[_i].isalpha()" not in block, (
        "the generic letters-only loop rewrites uncatalogued IDs"
    )
