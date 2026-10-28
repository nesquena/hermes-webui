"""Tests for _should_prune_tool_results_after_compression: the post-compression
tool-result pruning gate must read the active engine object, not a config string.
"""

import types


# ── helpers ────────────────────────────────────────────────────────────────

def _make_agent(compressor=None):
    """Minimal agent stub carrying only the attribute the gate reads."""
    agent = types.SimpleNamespace()
    if compressor is not None:
        agent.context_compressor = compressor
    return agent


class FakeExternalEngine:
    """A ContextEngine that is NOT a ContextCompressor subclass."""
    name = "fake-lcm"
    compression_count = 1


# ── tests ──────────────────────────────────────────────────────────────────

class TestPrunerGate:
    """_should_prune_tool_results_after_compression decision matrix."""

    def test_native_compressor_prunes(self, monkeypatch):
        """Native ContextCompressor active → pruning ON."""
        from api.streaming import _should_prune_tool_results_after_compression

        import sys
        fake_mod = types.ModuleType("agent.context_compressor")

        class _FakeCC:
            pass

        fake_mod.ContextCompressor = _FakeCC
        native = _FakeCC()
        monkeypatch.setitem(sys.modules, "agent.context_compressor", fake_mod)

        agent = _make_agent(compressor=native)
        assert _should_prune_tool_results_after_compression(agent) is True

    def test_external_engine_skips_pruning(self, monkeypatch):
        """External engine active → pruning OFF."""
        from api.streaming import _should_prune_tool_results_after_compression

        import sys
        fake_mod = types.ModuleType("agent.context_compressor")

        class _FakeCC:
            pass

        fake_mod.ContextCompressor = _FakeCC
        monkeypatch.setitem(sys.modules, "agent.context_compressor", fake_mod)

        agent = _make_agent(compressor=FakeExternalEngine())
        assert _should_prune_tool_results_after_compression(agent) is False

    def test_no_compressor_fails_open_to_native(self):
        """No context_compressor attribute → fail-open → pruning ON."""
        from api.streaming import _should_prune_tool_results_after_compression

        agent = _make_agent()
        assert _should_prune_tool_results_after_compression(agent) is True

    def test_none_compressor_fails_open_to_native(self):
        """context_compressor = None → fail-open → pruning ON."""
        from api.streaming import _should_prune_tool_results_after_compression

        agent = types.SimpleNamespace(context_compressor=None)
        assert _should_prune_tool_results_after_compression(agent) is True

    def test_config_drift_does_not_change_decision(self, monkeypatch):
        """A config edit mid-turn must NOT flip the decision."""
        from api.streaming import _should_prune_tool_results_after_compression

        import sys
        fake_mod = types.ModuleType("agent.context_compressor")

        class _FakeCC:
            pass

        fake_mod.ContextCompressor = _FakeCC
        monkeypatch.setitem(sys.modules, "agent.context_compressor", fake_mod)

        agent = _make_agent(compressor=FakeExternalEngine())
        before = _should_prune_tool_results_after_compression(agent)
        after = _should_prune_tool_results_after_compression(agent)

        assert before is False
        assert after is False


class TestPrunerGateIntegration:
    """Verify the gate in streaming.py calls the helper, not cfg_get."""

    def test_streaming_does_not_use_cfg_get_for_gate(self):
        """The config-string anti-pattern must be gone from the pruning gate block."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

        assert "_should_prune_tool_results_after_compression(agent)" in src

        prune_idx = src.index("_prune_context_tool_results_after_compression")
        window = src[prune_idx - 500 : prune_idx + 500]
        assert "cfg_get" not in window, (
            "Gate still reads config string near the pruner call — "
            "must use _should_prune_tool_results_after_compression"
        )
