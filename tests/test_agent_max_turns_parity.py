"""Regression checks for WebUI AIAgent iteration-budget parity.

WebUI streaming agents must honor Hermes' configured agent.max_turns. Otherwise
browser-originated long-running tasks silently fall back to AIAgent's constructor
default and hit the "maximum number of tool-calling iterations" summary path even
when the operator raised the global Hermes budget.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _compute_agent_cache_signature_source() -> str:
    """Return the source of the `_compute_agent_cache_signature()` helper.

    The cache-signature blob used to be inlined in the streaming send path; it
    now lives in this helper so the initial send and both self-heal retry paths
    derive the signature from the same final runtime bundle.
    """
    start = STREAMING_PY.index("def _compute_agent_cache_signature(")
    end = STREAMING_PY.index("\ndef ", start)
    return STREAMING_PY[start:end]


def _signature_blob() -> str:
    """Return the `_json.dumps([...])` field list the signature hashes."""
    helper = _compute_agent_cache_signature_source()
    blob_start = helper.index("_sig_blob = _json.dumps")
    blob_end = helper.index("], sort_keys=True)", blob_start)
    return helper[blob_start:blob_end]


def _production_signature_calls() -> list[tuple[int, str]]:
    """Return `(offset, source)` for every production signature call site.

    Paren-balanced so the whole multi-line keyword-argument list is captured,
    and every call site is returned so a retry path cannot silently drop a
    field that the initial send still passes.
    """
    marker = "_agent_sig = _compute_agent_cache_signature("
    calls: list[tuple[int, str]] = []
    pos = STREAMING_PY.find(marker)
    while pos != -1:
        depth = 0
        for idx in range(pos + len(marker) - 1, len(STREAMING_PY)):
            char = STREAMING_PY[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append((pos, STREAMING_PY[pos:idx + 1]))
                    break
        else:
            raise AssertionError("unterminated _compute_agent_cache_signature( call")
        pos = STREAMING_PY.find(marker, pos + 1)
    return calls


def test_streaming_agent_reads_agent_max_turns_from_config():
    assert "_agent_cfg_for_iterations" in STREAMING_PY
    assert "_agent_cfg_for_iterations.get('max_turns')" in STREAMING_PY
    assert "_cfg.get('max_turns')" in STREAMING_PY


def test_streaming_agent_passes_max_iterations_to_aiagent():
    assert "if 'max_iterations' in _agent_params and _max_iterations_cfg is not None:" in STREAMING_PY
    assert "_agent_kwargs['max_iterations'] = _max_iterations_cfg" in STREAMING_PY


def test_streaming_agent_cache_signature_includes_max_iterations():
    assert "max_iterations_cfg or ''" in _signature_blob(), (
        "_compute_agent_cache_signature() must hash max_iterations_cfg, or a "
        "max_turns change reuses the cached agent built on the old budget."
    )

    calls = _production_signature_calls()
    assert calls, "streaming.py no longer calls _compute_agent_cache_signature()"
    for _offset, call in calls:
        assert "max_iterations_cfg=_max_iterations_cfg" in call, (
            "every signature call site (initial send and both self-heal "
            "retries) must pass the resolved max_iterations budget:\n" + call
        )
