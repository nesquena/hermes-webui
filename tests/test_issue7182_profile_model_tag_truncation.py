"""Regression tests for issue #7182 — profile parsing truncated tagged models.

``api/profiles.py`` kept a positional ``rsplit(":", 1)`` long after the shared
grammar in ``config._parse_provider_qualified_model_id()`` became the single
parser for ``@provider:model`` values (#1776, #1948, #2047, #6722, #6723).

Splitting at the LAST colon amputates any model name that carries its own tag.
An Ollama model is the common shape: ``@ollama:qwen3.8:27b-mtp-q8_0`` resolved
to model ``27b-mtp-q8_0`` with provider ``ollama:qwen3.8``, and the truncated
name was then sent upstream, which 404'd. ``_split_webui_provider_model_value``
also PERSISTS its result into profile config, so the mangled name outlived the
session that produced it.

The two shapes below are the ones a positional split cannot separate:
``@ollama:qwen3.8:27b-mtp-q8_0`` is a single-segment provider with a tagged
model, while ``@custom:backup:model-a`` is a two-segment provider ID with a
plain model. Guessing either way breaks the other, which is why both live here.
"""

import pytest

from api.config import _parse_provider_qualified_model_id
from api.profiles import (
    _split_webui_provider_model_value,
    _strip_webui_provider_prefix,
)


# (qualified value, expected bare model, expected provider)
QUALIFIED_MODEL_CASES = [
    # The reported #7182 case: Ollama name:tag behind a single-segment provider.
    ("@ollama:qwen3.8:27b-mtp-q8_0", "qwen3.8:27b-mtp-q8_0", "ollama"),
    # Untagged model through the same provider still resolves.
    ("@ollama:llama4", "llama4", "ollama"),
    # Named custom provider — provider keeps both segments.
    ("@custom:backup:model-a", "model-a", "custom:backup"),
    # Named custom provider AND a tagged model.
    ("@custom:mykey:model-a:free", "model-a:free", "custom:mykey"),
    # host:port custom provider IDs.
    ("@custom:192.168.1.5:11434:llama4", "llama4", "custom:192.168.1.5:11434"),
    ("@openrouter:meta/llama-4:free", "meta/llama-4:free", "openrouter"),
]


class TestIssue7182ReportedShape:
    """The exact assertions the issue pinned, kept verbatim."""

    def test_split_preserves_ollama_tag(self):
        assert _split_webui_provider_model_value(
            "@ollama:qwen3.8:27b-mtp-q8_0", None
        ) == ("qwen3.8:27b-mtp-q8_0", "ollama")

    def test_strip_preserves_ollama_tag(self):
        assert (
            _strip_webui_provider_prefix("@ollama:qwen3.8:27b-mtp-q8_0")
            == "qwen3.8:27b-mtp-q8_0"
        )

    def test_split_preserves_multi_segment_custom_provider(self):
        assert _split_webui_provider_model_value(
            "@custom:mykey:model-a:free", None
        ) == ("model-a:free", "custom:mykey")


class TestProfilesUseTheSharedGrammar:
    """profiles' helpers must agree with the shared config parser, exactly."""

    @pytest.mark.parametrize(
        "value,expected_model,expected_provider", QUALIFIED_MODEL_CASES
    )
    def test_split_matches_shared_parser(self, value, expected_model, expected_provider):
        assert _parse_provider_qualified_model_id(value) == (
            expected_model,
            expected_provider,
        ), f"shared parser disagrees for {value!r}"
        assert _split_webui_provider_model_value(value, None) == (
            expected_model,
            expected_provider,
        ), f"profiles splitter disagrees for {value!r}"

    @pytest.mark.parametrize(
        "value,expected_model,expected_provider", QUALIFIED_MODEL_CASES
    )
    def test_strip_matches_shared_parser(self, value, expected_model, expected_provider):
        assert _strip_webui_provider_prefix(value) == expected_model

    def test_explicit_provider_argument_still_wins(self):
        """An explicitly supplied provider is not overridden by the parsed hint."""
        assert _split_webui_provider_model_value(
            "@ollama:qwen3.8:27b-mtp-q8_0", "ollama"
        ) == ("qwen3.8:27b-mtp-q8_0", "ollama")

    def test_no_last_colon_split_remains(self):
        """A positional rsplit would yield the bare tag; the shared grammar does not."""
        model, _provider = _split_webui_provider_model_value(
            "@ollama:qwen3.8:27b-mtp-q8_0", None
        )
        assert model != "27b-mtp-q8_0"


class TestUnqualifiedValuesUntouched:
    """Values that are not @provider-qualified must pass through unchanged."""

    @pytest.mark.parametrize(
        "value",
        ["qwen3.8:27b-mtp-q8_0", "llama4", "", "  ", "@nocolon"],
    )
    def test_strip_passthrough(self, value):
        assert _strip_webui_provider_prefix(value) == value.strip()

    def test_split_passthrough_keeps_plain_model(self):
        assert _split_webui_provider_model_value("qwen3.8:27b-mtp-q8_0", "ollama") == (
            "qwen3.8:27b-mtp-q8_0",
            "ollama",
        )
