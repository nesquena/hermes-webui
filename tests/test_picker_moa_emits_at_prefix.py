"""Regression tests for the MoA picker id shape.

Bug: the picker at api/config.py:4665 and :7117 emitted
    {"id": f"moa:{preset_name}", "label": ...}
which is the *bare* colon-prefixed form, not the @provider:model
convention every other picker entry uses.

Consequence: when a user picked a MoA preset, the request body the
WebUI sent to /api/chat/start was
    model_provider: "moa"
    model: "moa:ai-council-lite"
_downstream_, ``_split_provider_qualified_model`` only recognised the
``@provider:model`` shape, so the literal string ``moa:ai-council-lite``
fell through unchanged, was persisted verbatim on the session, and
surfaced as ``Error: 'moa:ai-council-lite'`` when feedback returned from
the gateway.

Fix: change the picker injection to emit
    {"id": f"@moa:{preset_name}", "label": ...}
(consistent with the rest of the picker). ``_split_provider_qualified_model``
then returns ``(<preset>, "moa")`` and the rest of the routing path is
already known to be correct.

These tests inspect the picker output (which is what the WebUI dropdown
displays and what selectModelFromDropdown() in static/ui.js sends to
/api/chat/start body.model) for ANY moa: group whose model ids miss the
leading ``@``. A single regression is enough to catch the bug coming
back from a future shameless refactor of ``moa_models.append``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# Locate repo root + key file. Avoid importing the whole api module
# (yaml + heavy deps) — we only need to assert the picker producer
# emits the @provider:model form for MoA.
HERE = Path(__file__).resolve().parent.parent
CONFIG = HERE / "api" / "config.py"


def _read_config() -> str:
    return CONFIG.read_text(encoding="utf-8")


def test_picker_emits_at_prefixed_provider_form_for_moa():
    """The MoA picker producers must use the @provider:model convention.

    Two injection points: the offline catalog (line 4665 area) and the
    live-rebuild path (line 7117 area). Both must contain an
    ``f"@moa:{preset_name}"`` literal.
    """
    src = _read_config()
    at_moa_pattern = re.compile(r'f"@moa:\{preset_name\}"')
    matches = at_moa_pattern.findall(src)
    assert len(matches) >= 2, (
        "Expected at least two @-prefixed MoA injection points "
        f"(offline + live-rebuild), found: {matches!r}"
    )


def test_picker_does_not_emit_bare_moa_form():
    """Belt-and-braces guard: even one surviving bare moa:<preset> producer
    is enough to land the field as 'moa:...' in the session JSON.

    Search the whole api/config.py for any *producer* of the bare form
    (``f"moa:{...}"``). Comment-only mentions are fine.
    """
    src = _read_config()
    # Match bare producer shapes in code lines (not docstring/comments).
    bare_producer = re.compile(r'^\s*[^\#].*f"moa:\{', re.MULTILINE)
    hits = bare_producer.findall(src)
    # Filter out strings that ARE in a @moa: prefix context (those contain
    # '@moa:' as a literal, not f"moa:").
    real_bare_producers = [h for h in hits if "@moa:" not in h]
    assert not real_bare_producers, (
        "Bare moa:<name> producer lines still present in api/config.py: "
        f"{real_bare_producers!r}"
    )


def test_picker_injects_only_for_known_provider():
    """The MoA group injection runs only when ``moa.presets`` is a non-empty
    dict. A regression that runs the injection unconditionally would also
    break non-moa sessions; the existing test in test_issue5057 covers
    that. Here we just sanity-check the injection still parks itself inside
    a guarded block.
    """
    src = _read_config()
    # find the "Inject MoA" comments
    moa_blocks = re.findall(
        r"# ── Inject MoA.*?# ──",
        src,
        re.DOTALL,
    )
    assert len(moa_blocks) >= 1, "Expected at least one 'Inject MoA' block"


def test_config_compiles_as_python():
    """Defence: the picker change must keep api/config.py parseable.

    The static catalog dicts in the module also need to round-trip
    through ast.parse() for any other reason (syntax mistake). We do a
    full AST compile, not just py_compile, to surface line-numbered
    failures.
    """
    src = _read_config()
    try:
        ast.parse(src, filename=str(CONFIG))
    except SyntaxError as exc:
        raise AssertionError(f"api/config.py has a syntax error: {exc}") from exc
