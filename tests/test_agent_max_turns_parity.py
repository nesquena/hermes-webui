"""Regression checks for WebUI AIAgent iteration-budget parity.

WebUI streaming agents must honor Hermes' configured agent.max_turns. Otherwise
browser-originated long-running tasks silently fall back to AIAgent's constructor
default and hit the "maximum number of tool-calling iterations" summary path even
when the operator raised the global Hermes budget.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def test_streaming_agent_reads_agent_max_turns_from_config():
    assert "_agent_cfg_for_iterations" in STREAMING_PY
    assert "_agent_cfg_for_iterations.get('max_turns')" in STREAMING_PY
    assert "_cfg.get('max_turns')" in STREAMING_PY


def test_streaming_agent_passes_max_iterations_to_aiagent():
    assert (
        "if 'max_iterations' in _agent_params and _max_iterations_cfg is not None:"
        in STREAMING_PY
    )
    assert "_agent_kwargs['max_iterations'] = _max_iterations_cfg" in STREAMING_PY


def test_streaming_agent_cache_signature_includes_max_iterations():
    sig_start = STREAMING_PY.index("_sig_blob = _json.dumps")
    sig_block = STREAMING_PY[
        sig_start : STREAMING_PY.index("], sort_keys=True)", sig_start)
    ]
    assert "_max_iterations_cfg or ''" in sig_block
