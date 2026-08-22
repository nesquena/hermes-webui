r"""Regression tests for #7228 — model picker search misses OpenRouter display names.

Reported symptom: searching the composer model picker for ``Ox Alpha`` (the
display name users see in Hermes Desktop for ``stealth/ox-alpha``) returns
"No models found" even though the catalog contains the model.

Two contributing root causes, both covered here:

  (1) Server: the OpenRouter *curated* branch of get_available_models() ships
      ``{"id": mid, "label": mid}`` — raw id as label. The second tuple element
      from hermes_cli.models.fetch_openrouter_models() is a picker badge
      ("default"/"free"/"recommended"), NOT a display name, so it cannot be
      used. The human-readable names live in the shared OpenRouter metadata
      cache (agent.model_metadata.fetch_model_metadata(), the same data Desktop
      hydrates from). The free-tier augment path right below already derives
      friendly labels from live API names; the curated path must do the same.

  (2) Client: _filterModels() matches with literal substring includes() on the
      lowercased name/id — no separator folding — so "ox alpha" never matches
      "stealth/ox-alpha" (or even "Ox Alpha" once labels ARE hydrated, because
      the query keeps its inner space while haystacks keep their hyphens).
      The fix folds [\s._-]+ runs on both query and haystacks before matching.

The server tests drive get_available_models() end-to-end with both live-fetch
layers monkeypatched (hermetic, no network). The client test executes the real
_foldModelSearchText helper from static/ui.js under node and asserts observable
folding behavior plus its wiring into _filterModels.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

import api.config as config

REPO = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload: dict):
        self._buf = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._buf


def _get_grouped_models() -> list[dict]:
    """Return the `groups` field from get_available_models(), cache-reset."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    result = config.get_available_models()
    return result.get("groups", [])


@pytest.fixture(autouse=True)
def _isolate_openrouter_caches(monkeypatch):
    """Reset caches + force openrouter as the active provider (see #1426 harness)."""
    try:
        from hermes_cli import models as _hm

        monkeypatch.setattr(_hm, "_openrouter_catalog_cache", None, raising=False)
    except Exception:
        pass

    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
            "providers": {"openrouter": {"api_key": "sk-or-test-key"}},
        },
        raising=False,
    )
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _stub_live_layers(monkeypatch, *, curated, metadata=None, meta_raises=False):
    """Stub fetch_openrouter_models + free-tier urlopen + metadata lookup."""
    try:
        from hermes_cli import models as _hm

        monkeypatch.setattr(
            _hm,
            "fetch_openrouter_models",
            lambda *a, **k: list(curated),
            raising=False,
        )
    except Exception:
        pass

    # Free-tier augment: empty catalog so the curated path is fully in charge.
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse({"data": []}),
    )

    fake_meta_module = type(sys)("agent")
    fake_meta_pkg = type(sys)("agent.model_metadata")

    if meta_raises:
        def _raise(*a, **k):
            raise RuntimeError("metadata source unavailable")
        fake_meta_pkg.fetch_model_metadata = _raise
    else:
        fake_meta_pkg.fetch_model_metadata = lambda *a, **k: dict(metadata or {})

    fake_meta_module.model_metadata = fake_meta_pkg
    fake_meta_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", fake_meta_module)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", fake_meta_pkg)


def _openrouter_entries(grouped):
    group = next((g for g in grouped if g.get("provider_id") == "openrouter"), None)
    assert group is not None, "openrouter group must be present"
    return [
        dict(m)
        for bucket in ("models", "extra_models")
        for m in group.get(bucket, [])
    ]


def test_curated_overflow_labels_hydrate_display_names(monkeypatch):
    """Curated entries whose ids resolve in the shared metadata cache must ship
    the human-readable name as their picker label (#7228 part 1)."""
    _stub_live_layers(
        monkeypatch,
        curated=[
            ("anthropic/claude-sonnet-4.6", ""),
            ("stealth/ox-alpha", ""),
            ("x-ai/grok-4.20", ""),
        ],
        metadata={
            "stealth/ox-alpha": {"name": "Ox Alpha", "context_length": 262144},
            # grok intentionally absent -> exercises the raw-id fallback below
        },
    )

    entries = _openrouter_entries(_get_grouped_models())
    by_id = {e["id"]: e["label"] for e in entries}

    assert by_id.get("stealth/ox-alpha") == "Ox Alpha", (
        f"expected hydrated display name for stealth/ox-alpha, got {by_id!r}"
    )


def test_curated_label_falls_back_to_raw_id_without_metadata(monkeypatch):
    """Ids absent from the metadata cache keep the previous raw-id label —
    fail-open, never drop an entry because metadata is missing."""
    _stub_live_layers(
        monkeypatch,
        curated=[("x-ai/grok-4.20", "")],
        metadata={"stealth/ox-alpha": {"name": "Ox Alpha"}},
    )

    entries = _openrouter_entries(_get_grouped_models())
    by_id = {e["id"]: e["label"] for e in entries}

    assert by_id.get("x-ai/grok-4.20") == "x-ai/grok-4.20"


def test_curated_labels_survive_metadata_source_failure(monkeypatch):
    """A raising metadata lookup must not break the picker group; entries fall
    back to raw ids (previous behavior) instead of erroring the endpoint."""
    _stub_live_layers(
        monkeypatch,
        curated=[("stealth/ox-alpha", "")],
        meta_raises=True,
    )

    entries = _openrouter_entries(_get_grouped_models())
    by_id = {e["id"]: e["label"] for e in entries}

    assert by_id.get("stealth/ox-alpha") == "stealth/ox-alpha"


# ── client-side separator folding ─────────────────────────────────────────────


def _run_node_script(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as handle:
            handle.write(script)
            script_path = handle.name
        try:
            result = subprocess.run(
                [node, script_path],
                cwd=REPO,
                text=True,
                capture_output=True,
                timeout=10,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"node behavior check timed out\nstdout:\n{exc.stdout}")
    if result.returncode:
        pytest.fail(
            "node behavior check failed\n"
            f"exit code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _extract_js_function(source: str, name: str) -> str:
    """Extract a top-level `function <name>(...) {...}` declaration by brace match."""
    marker = f"function {name}("
    start = source.index(marker)
    body_open = source.index("{", start)
    depth = 0
    for idx in range(body_open, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _fold_helper_script(assertions: str) -> str:
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _extract_js_function(ui_js, "_foldModelSearchText")
    return f"{helper}\n{assertions}"


def test_fold_helper_normalizes_separators_for_picker_search():
    """"Ox Alpha" (query) must equal "stealth/ox-alpha" haystack tokens after
    folding — observable folding behavior, not a source-string check."""
    assertions = r"""
const cases = [
  ["Ox Alpha", "oxalpha"],
  ["  ox   alpha ".trim(), "oxalpha"],
  ["ox-alpha", "oxalpha"],
  ["ox_alpha", "oxalpha"],
  ["ox.alpha", "oxalpha"],
  ["stealth/ox-alpha", "stealth/oxalpha"],
];
for (const [input, expected] of cases) {
  const got = _foldModelSearchText(input);
  if (got !== expected) {
    console.error(`FAIL fold(${JSON.stringify(input)}) => ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
    process.exit(1);
  }
}
if (_foldModelSearchText("") !== "") { console.error("FAIL empty"); process.exit(1); }
if (_foldModelSearchText(null) !== "") { console.error("FAIL null"); process.exit(1); }
console.log("ok");
"""
    assert _run_node_script(_fold_helper_script(assertions)) == "ok"


def test_filter_models_wires_folded_matching_on_both_sides():
    """_filterModels must compare folded query against folded name AND id."""
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    fn_start = ui_js.index("const _filterModels=(term)=>{")
    fn_src = ui_js[fn_start : ui_js.index("const matches=(m)=>", fn_start)]
    assert "_foldModelSearchText(term)" in fn_src, (
        "_filterModels must fold the query before matching"
    )
    assert "_foldModelSearchText(m.name)" in fn_src, (
        "_filterModels must fold the haystack names"
    )
    assert "_foldModelSearchText(m.id)" in fn_src, (
        "_filterModels must fold the haystack ids"
    )
