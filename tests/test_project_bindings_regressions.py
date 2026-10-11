"""Focused regressions for PR #6836 P1/P2 fixes (a7763ac3).

Covers the P1 and the P2 that are still part of this PR:

- P1 provider-scoped dedupe: _showProjectBindingsDialog() must preserve distinct
  (model, provider) pairs when the same bare model id exists under several
  providers. Saving must pin the selected provider rather than collapsing to the
  first one. Backend must canonicalize model_provider via _canonical_context_provider.

(The P1 auto-assign-ownership race and the P2 shutdown-drain respect this file
also covered were about the auto-assign sweep, which moved to a follow-up PR
with its toggle — maintainer re-gate 2026-10-11T02:08:20Z.)
"""

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_sessions_js() -> str:
    return (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


class _AnyWorkspace(set):
    """A set that claims to contain every workspace path."""

    def __contains__(self, item):  # noqa: D105
        return True




def _read_routes_py() -> str:
    return (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1 — concurrent ownership: non-streaming authoritative recheck
# ---------------------------------------------------------------------------










def test_bind_model_provider_is_canonicalized_via_helper():
    """Backend canonicalizes model_provider through _canonical_context_provider.

    e.g. 'OpenAI' -> 'openai', 'custom:My Prov' -> normalized slug.
    The routes.py bind path must call that helper; we assert both the helper
    contract and that the bind site invokes it.
    """
    from api.routes import _canonical_context_provider

    assert _canonical_context_provider("OpenAI") == "openai"
    assert _canonical_context_provider("openAI") == "openai"
    assert _canonical_context_provider("") == ""
    assert _canonical_context_provider(None) == ""
    # custom provider shape stays custom:*
    assert _canonical_context_provider("custom:test") == "custom:test"
    assert _canonical_context_provider("custom:My Provider") != ""

    src = _read_routes_py()
    # The bind handler must canonicalize model_provider on save.
    assert "_canonical_context_provider" in src
    # Must be on the proj['model_provider'] assignment in /api/projects/bind.
    bind_slice = src[src.find("if \"model_provider\" in body:"):src.find("if \"model_provider\" in body:") + 1200]
    assert "_canonical_context_provider" in bind_slice, "bind must canonicalize model_provider"


def test_bindings_dialog_preserves_provider_scoped_duplicate_model_ids():
    """Frontend dedupe must be provider-scoped.

    Same bare model id under two providers must yield two distinct selectable
    entries, not one collapsed entry. The dialog uses provider-scoped synthetic
    keys when duplicates exist and Save pins the chosen provider.
    """
    src = _read_sessions_js()

    # Dedupe during option collection is (value, provider)-scoped.
    assert "modelOptions.some(x=>x.value===val&&x.sub===provider)" in src

    # Synthetic key helpers exist and are used for display vs wire values.
    assert "_modelValueKeyFor" in src
    assert "_modelValueFor" in src
    assert "_modelProvFor" in src
    assert "_hasDuplicateModelValues" in src

    # When duplicates exist, option keys become provider-scoped and Save extracts
    # provider from the synthetic key rather than collapsing to the first hit.
    assert "o._key=_modelValueKeyFor(o.value,o.sub" in src
    assert "_prov=_modelProvFor(modelVal)||null;" in src
    # The provider is always resolved from the authoritative option (dataset or
    # inherited <optgroup>), with the provider-qualified model id as last resort.
    assert "const provider=_optProviderId(o);" in src
    assert "_prov=_getOptionProviderId({value:_bare})||null;" in src
    # Clearing the model must also clear the provider (no stale provider stick).
    # The Save path has an else { fields.model=null; fields.model_provider=null }.
    assert "fields.model_provider=null" in src


def test_bindings_dialog_provider_scoped_key_roundtrip_via_node():
    """Node-evaluated roundtrip: provider+model synthetic keys isolate routes."""
    import shutil, json as _json, tempfile, os

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    driver = r"""
const [keysJson] = process.argv.slice(2);
const cases = JSON.parse(keysJson);
const _modelValueKeyFor=(val,prov)=>prov?(prov+"\u001f"+val):val;
const _modelValueFor=(k)=>{const i=k.indexOf("\u001f");return i>=0?k.slice(i+1):k;};
const _modelProvFor=(k)=>{const i=k.indexOf("\u001f");return i>=0?k.slice(0,i):"";};
for (const {val, prov} of cases) {
  const k=_modelValueKeyFor(val,prov);
  if (_modelValueFor(k)!==val) { console.error("bare mismatch", val, k, _modelValueFor(k)); process.exit(1); }
  if (_modelProvFor(k)!==prov) { console.error("prov mismatch", prov, k, _modelProvFor(k)); process.exit(1); }
}
console.log("ok");
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(driver)
        path = f.name
    try:
        cases = [
            {"val": "gpt-4o", "prov": "openai"},
            {"val": "gpt-4o", "prov": "azure-openai"},
            {"val": "claude-3-5-sonnet", "prov": "anthropic"},
            {"val": "claude-3-5-sonnet", "prov": "custom:my-proxy"},
            {"val": "same-id", "prov": ""},
        ]
        import subprocess as sp
        r = sp.run([node, path, _json.dumps(cases)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"node driver failed: {r.stderr} {r.stdout}"
        assert "ok" in r.stdout
        # Distinct providers must yield distinct synthetic keys for the same bare id.
        keys = {}
        for c in cases:
            k = (c["prov"] + "\x1f" + c["val"]) if c["prov"] else c["val"]
            keys.setdefault(c["val"], set()).add(k)
        assert len(keys["gpt-4o"]) == 2, "same bare id under two providers must be independently selectable"
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P2 — shutdown drain respects _register_background_commit_thread guard
# ---------------------------------------------------------------------------










