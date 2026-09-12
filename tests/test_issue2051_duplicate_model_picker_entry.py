"""Regression: the composer model picker lists one configured model twice (#2051).

The picker showed two rows for a single configured model, ``acme/example-model``
and ``custom/acme/example-model``, while ``GET /api/models`` returned a ``Custom``
group holding exactly one model and ``#modelSelect`` ended up with exactly one
matching ``<option>``. Both rows are produced client-side.

``configured_model_badges`` carries every routing spelling of a configured model.
For one custom-provider model that is three keys::

    acme/example-model            (bare/canonical)
    custom/acme/example-model     (provider-qualified)
    @custom:acme/example-model    (routable)

``renderModelDropdown()`` walks that map and pushes a synthetic ``_modelData``
row for every key that ``_isEquivalentConfiguredModelEntry()`` does not recognise
as an alias of a row the catalog already produced. That predicate recognised two
of the three spellings; it missed ``<provider>/<model>`` because
``_normalizeConfiguredModelKey()`` strips only one leading slash segment —
deliberately, since #3360 requires ``vendor_a/x`` and ``vendor_b/y/x`` to stay
distinct. So::

    acme/example-model         -> example.model
    custom/acme/example-model  -> acme/example.model   (different key)

No match, so the alias was pushed as a second row: one model, two picker entries.
``_deduplicateModelPickerOptions()`` cannot catch this — it dedupes ``<option>``
elements inside ``<select>``, and this row never becomes an ``<option>``.

The contract pinned here: ``<provider>/<model>`` is recognised as an alias of
``<model>`` under the same proof the ``@<provider>:<model>`` rule already uses.
The badge declares a provider, the key begins with that provider's prefix, and an
existing picker row from that same provider normalises equal to the remainder.
Two genuinely different models never satisfy the last clause, so the change can
only drop a duplicate — never hide a distinct model.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def js(*names):
    """Real source of the named ui.js functions — never a re-implementation."""
    return "\n".join(extract_function(UI_JS, name) for name in names)


def run_node(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout
    return out.stdout


HARNESS = """
const assert=require('assert');
var window={};
"""

# One Custom group holding one model, with three badge spellings of it.
CATALOG_ROW = "{value:'acme/example-model',providerId:'custom'}"
BADGE = "{role:'primary',label:'Primary',provider:'custom'}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_provider_qualified_badge_alias_is_not_a_second_picker_row():
    """All three spellings of one configured model collapse to one row (#2051)."""
    script = HARNESS + js(
        "_normalizeConfiguredModelKey",
        "_isEquivalentConfiguredModelEntry",
    ) + f"""
const entries=[{CATALOG_ROW}];
const badge={BADGE};

// All three routing spellings name the same model the catalog already rendered.
for(const alias of ['acme/example-model',
                    'custom/acme/example-model',
                    '@custom:acme/example-model']){{
  assert.strictEqual(_isEquivalentConfiguredModelEntry(alias,badge,entries), true,
    'badge alias '+alias+' must not become a second picker row (#2051)');
}}
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_genuinely_different_model_is_never_suppressed():
    """The new rule must not hide a model the catalog does not have."""
    script = HARNESS + js(
        "_normalizeConfiguredModelKey",
        "_isEquivalentConfiguredModelEntry",
    ) + f"""
const entries=[{CATALOG_ROW}];
const badge={BADGE};

// Same provider prefix, different model -> must still render its own row.
assert.strictEqual(
  _isEquivalentConfiguredModelEntry('custom/acme/other-model',badge,entries), false,
  'a different model under the same provider must keep its own row');

// Right model name, but no catalog row owned by that provider -> not proven
// equivalent, so keep it.
assert.strictEqual(
  _isEquivalentConfiguredModelEntry(
    'custom/acme/example-model',
    {{role:'primary',label:'Primary',provider:'otherprovider'}},
    [{{value:'acme/example-model',providerId:'custom'}}]), false,
  'a same-named model owned by a different provider must keep its own row');

// A badge with no provider cannot use the prefix proof at all.
assert.strictEqual(
  _isEquivalentConfiguredModelEntry('custom/acme/example-model',
    {{role:'primary',label:'Primary'}},entries), false,
  'a provider-less badge must not be collapsed by the prefix rule');

// The prefix must be a real path segment, not a substring of the first segment.
assert.strictEqual(
  _isEquivalentConfiguredModelEntry('customer/acme/example-model',badge,entries), false,
  'only a literal `<provider>/` prefix counts');
console.log('OK');
"""
    run_node(script)


def test_prefix_rule_mirrors_the_at_provider_rule_structurally():
    """The `<provider>/` proof must require the same three clauses as `@<provider>:`.

    Structural, not behavioural: it pins that the new branch is gated on the
    badge-declared provider, on a literal prefix match, and on an existing entry
    of that same provider — so it cannot grow into a loose label comparison.
    """
    src = extract_function(UI_JS, "_isEquivalentConfiguredModelEntry")
    assert "const slashPrefix=provider?`${provider}/`:'';" in src, (
        "the provider-qualified alias rule must derive its prefix from the "
        "badge-declared provider"
    )
    assert "rawId.toLowerCase().startsWith(slashPrefix)" in src, (
        "the rule must require a literal `<provider>/` prefix on the badge key"
    )
    assert "String(entry.providerId||'').toLowerCase()===provider" in src, (
        "the rule must require an existing picker row owned by the same provider"
    )
    # It must run before the `@provider:` early return, which would otherwise
    # short-circuit every non-`@` alias to false.
    assert src.index("slashPrefix") < src.index(
        "if(!prefix||!rawId.toLowerCase().startsWith(prefix)) return false;"
    ), "the `<provider>/` rule must be reached before the `@<provider>:` early return"
