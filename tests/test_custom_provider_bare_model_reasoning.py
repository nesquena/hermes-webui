"""Regression tests: custom providers with non-slash model names expose reasoning efforts.

Custom API aggregators (e.g. New API, One API) route requests using their own
naming conventions — bare names like ``deepseek-v4-flash`` or dot-separated
names like ``moonshotai.kimi-k2.5`` — rather than the OpenRouter-style
``vendor/model`` slash format that the heuristic prefix list was written for.

Before this fix, ``resolve_model_reasoning_efforts`` always returned ``[]`` for
these combinations, hiding the reasoning effort selector in the UI even though
the underlying models fully support thinking/reasoning.
"""

import pytest

import api.config as cfg


# ── bare model names (no slash or dot prefix) ────────────────────────────────

def test_deepseek_v4_flash_bare_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek-v4-flash",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "deepseek-v4-flash via custom provider should expose reasoning efforts"
    )


def test_deepseek_r1_bare_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek-r1",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek.v3.2",
        "deepseek_v3_2",
        "vendor.deepseek.v3.2",
        "deepseek.v4-flash",
        "deepseek_v4_flash",
    ],
)
def test_deepseek_separator_variants_custom_provider(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} via custom provider should expose reasoning efforts"
    )


# ── dot-separated model names (vendor.model) ─────────────────────────────────

def test_kimi_dot_separated_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "moonshotai.kimi-k2.5",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "moonshotai.kimi-k2.5 via custom provider should expose reasoning efforts"
    )


def test_qwen3_dot_separated_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "qwen.qwen3-vl-235b-a22b-instruct",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


# ── "thinking" keyword in model name ─────────────────────────────────────────

def test_thinking_keyword_in_model_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "vendor.some-model-thinking-preview",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "model name containing 'thinking' should always expose reasoning efforts"
    )


def test_reasoning_keyword_in_model_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "vendor.model-reasoning-v1",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


# ── non-reasoning models must stay hidden ─────────────────────────────────────

def test_plain_llm_bare_name_custom_provider_no_reasoning():
    assert cfg.resolve_model_reasoning_efforts(
        "llama-3.1-8b-instruct",
        provider_id="custom:newapi",
    ) == [], (
        "generic llama model via custom provider should NOT expose reasoning efforts"
    )


def test_plain_llm_dot_separated_custom_provider_no_reasoning():
    assert cfg.resolve_model_reasoning_efforts(
        "meta.llama-3.1-70b",
        provider_id="custom:newapi",
    ) == []


@pytest.mark.parametrize(
    "model_id",
    [
        "thinkinghub.llama-3.1-70b",
        "reasoninghub.llama-3.1-70b",
    ],
)
def test_vendor_prefix_keyword_does_not_trigger_reasoning(model_id):
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    ) == []


# ── slash-prefixed names must still work (no regression) ─────────────────────

def test_deepseek_slash_prefix_still_works():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek/deepseek-v4-flash",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


def test_openrouter_slash_prefix_unaffected():
    efforts = cfg.resolve_model_reasoning_efforts(
        "anthropic/claude-sonnet-4.5",
        provider_id="openrouter",
    )
    assert set(efforts) >= {"low", "medium", "high"}


@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-3.6-flash",
        "github_copilot/gemini-3.6-flash",
        "github_copilot/gemini-3.1-pro-preview",
        "grok-4.6",
        "github_copilot/grok-4.5",
    ],
)
def test_custom_litellm_gemini_and_grok_routes_expose_reasoning(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:litellm",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} via custom:litellm should expose reasoning efforts"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-1.5-pro",
        "gemini-2.0-flash",
        "grok-4",
        "grok-4-fast",
    ],
)
def test_custom_litellm_pre_reasoning_gemini_grok_stay_hidden(model_id):
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:litellm",
    ) == [], f"{model_id} must not expose a reasoning ladder"


@pytest.mark.parametrize(
    "model_id",
    [
        # xAI ships dated ids for the base Grok 4 line. A naive version parser
        # reads "0709" / "20250709" as a minor >= 5 and wrongly exposes the
        # ladder for a model that rejects reasoning_effort outright.
        "grok-4-0709",
        "grok-4-20250709",
        "github_copilot/grok-4-0709",
        # Same class on the Gemini side.
        "gemini-2-20250219",
    ],
)
def test_dated_model_ids_are_not_read_as_minor_versions(model_id):
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:litellm",
    ) == [], (
        f"{model_id}: a trailing date stamp must not be parsed as a minor "
        "version — the base model rejects reasoning_effort"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "grok-4.5",
        "grok-4.6",
        "gemini-2.5-pro",
    ],
)
def test_date_stamp_guard_does_not_hide_real_minor_versions(model_id):
    """The date-stamp guard must not over-tighten.

    Paired with the test above: clamping the minor group to 1-2 digits has to
    keep genuine `<major>.<minor>` ids working, or the fix trades a false
    positive for a false negative.
    """
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:litellm",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} has a real minor version and must still expose the ladder"
    )


# ── Grok: explicit shapes, NOT a monotonic version comparison ────────────────
#
# xAI's lineup is not ordered by version: grok-3-mini accepts an effort dial
# while grok-3 does not, and within the 4.20 line only the multi-agent build
# accepts one. A ">= 4.5" rule is wrong in BOTH directions, so both directions
# are asserted here.

@pytest.mark.parametrize(
    "model_id",
    [
        "grok-3-mini",
        "grok-3-mini-fast",
        "grok-4.3",
        "grok-4.5",
        "grok-4.6",
        "grok-4.20-multi-agent",
    ],
)
def test_grok_effort_capable_shapes_expose_ladder(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id, provider_id="custom:litellm"
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} accepts reasoning_effort and must expose the ladder"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        # Reason natively but REJECT a reasoning_effort dial.
        "grok-3",
        "grok-4",
        "grok-4-fast",
        "grok-4-fast-reasoning",
        "grok-4-fast-non-reasoning",
        "grok-4-1-fast",
        "grok-4.20",
        "grok-4.20-non-reasoning",
        "grok-code-fast-1",
        # Dated build of the base 4 line.
        "grok-4-0709",
    ],
)
def test_grok_effort_incapable_shapes_stay_hidden(model_id):
    """These would 400 on a `reasoning_effort` parameter.

    `grok-4-fast-reasoning` is the interesting one: the generic
    "reasoning"/"thinking" keyword shortcut in `_candidate_supports_reasoning`
    claims any id containing that token, but "this model reasons" is a
    different question from "this model exposes an effort control".
    """
    assert cfg.resolve_model_reasoning_efforts(
        model_id, provider_id="custom:litellm"
    ) == [], f"{model_id} rejects reasoning_effort and must stay hidden"


# ── Gemini: non-text routes have no ladder at any version ────────────────────

@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-embedding-001",
        "gemini-3-pro-image-preview",
        "gemini-imagine-2",
        "gemini-2.5-flash-image",
    ],
)
def test_gemini_non_text_routes_never_expose_ladder(model_id):
    """Image / imagine / embedding routes must be denied before the version gate.

    Checking the version first would let `gemini-3-pro-image-preview` in on
    its `3`. Asserted for both provider shapes because the deny has to sit
    above the Copilot branch as well as the custom-provider heuristic.
    """
    for provider in ("custom:litellm", "copilot"):
        assert cfg.resolve_model_reasoning_efforts(
            model_id, provider_id=provider
        ) == [], f"{model_id} via {provider} must not expose a reasoning ladder"


@pytest.mark.parametrize(
    ("model_id", "expected_ladder"),
    [
        # The non-text keyword sits in the WRAPPER segment while the model
        # segment is an ordinary text Gemini. Scanning the whole route for
        # `image`/`embedding` and the family name separately denied these.
        ("image-router/gemini-3.6-flash", True),
        ("embedding-gateway/gemini-3.1-pro-preview", True),
        ("imagine-proxy/gemini-2.5-pro", True),
        # The keyword is inside the MODEL segment itself: still denied.
        ("vendor/gemini-3-pro-image-preview", False),
        ("vendor/gemini-embedding-001", False),
        ("router/gemini-imagine-2", False),
    ],
)
def test_gemini_non_text_deny_is_scoped_to_the_model_segment(model_id, expected_ladder):
    """The keyword and the family name must be read from the SAME segment.

    A routed id can nest a model under a wrapper namespace. Asking "does the
    route contain image/imagine/embedding" and "does the route mention gemini"
    as two independent questions lets a wrapper name deny a valid text model:
    `image-router/gemini-3.6-flash` matched `image` in the wrapper and `gemini`
    in the model, and lost its ladder entirely.

    Both directions are asserted here because either one alone is satisfiable
    by a broken predicate: a whole-route scan passes the negatives, and
    dropping the deny passes the positives.
    """
    for provider in ("custom:litellm", "custom:newapi"):
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider)
        if expected_ladder:
            assert efforts, (
                f"{model_id} via {provider}: the model segment is a text Gemini, "
                f"but the wrapper name suppressed its ladder"
            )
            assert set(efforts) <= {"minimal", "low", "medium", "high"}, (
                model_id,
                provider,
                efforts,
            )
        else:
            assert efforts == [], (
                f"{model_id} via {provider}: a non-text model segment must never "
                f"expose a ladder (got {efforts})"
            )


def test_gemini_segment_helpers_agree_on_identity_and_text_ness():
    """The two predicates must read the same segment, not different ones."""
    # Wrapper namespace does not contribute identity...
    assert cfg._gemini_model_segments("image-router/gemini-3.6-flash") == [
        "gemini-3.6-flash"
    ]
    assert cfg._gemini_model_segments("acme-gemini-router/deepseek-r1") == []
    # ...and non-text-ness is judged only on the identified segment.
    assert cfg._gemini_route_is_non_text("image-router/gemini-3.6-flash") is False
    assert cfg._gemini_route_is_non_text("vendor/gemini-3-pro-image-preview") is True


@pytest.mark.parametrize(
    "model_id,provider_id",
    [
        ("gemini-3.6-flash", "custom:litellm"),
        ("gemini-3.6-flash", "gemini"),
        ("gemini-3.6-flash", "copilot"),
        ("gemini-2.5-pro", "gemini"),
        ("google/gemini-2.5-pro", "openrouter"),
        ("vertex/gemini-3-pro-preview", "custom:newapi"),
        ("github_copilot/gemini-3.6-flash", "custom:litellm"),
    ],
)
def test_gemini_ladder_is_exact_never_xhigh_or_max(model_id, provider_id):
    """Gemini never offers `xhigh` or `max`, on any route.

    Gemini has no such levels; the adapter reads an unknown `max` as medium,
    so offering one would report a thinking depth that never happened.

    Whether a ladder is offered AT ALL is a separate question answered by the
    runtime, not by this test: on Copilot the control only appears when the
    installed core resolves that model (see
    `test_copilot_gemini_ladder_matches_runtime_capability` below), and on the
    native provider the ladder is narrowed to the levels the adapter can
    actually distinguish. Asserting a non-empty ladder unconditionally here
    would contradict both of those and re-assert the visible-but-inert
    behaviour this PR removes.
    """
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert "xhigh" not in efforts, (model_id, provider_id, efforts)
    assert "max" not in efforts, (model_id, provider_id, efforts)
    assert set(efforts) <= {"minimal", "low", "medium", "high"}, (
        model_id,
        provider_id,
        efforts,
    )


def test_copilot_gemini_ladder_matches_runtime_capability():
    """On Copilot, the Gemini control appears exactly when the runtime sends it.

    This replaces an unconditional "must expose a ladder" assertion. The real
    contract is agreement with the installed runtime: if the core resolves a
    ladder for the model, the UI must offer one; if it does not, the UI must
    stay empty rather than render a control the runtime ignores.
    """
    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    model_id = "gemini-3.6-flash"
    wire = list(copilot_models.github_model_reasoning_efforts(model_id) or [])
    ui = cfg.resolve_model_reasoning_efforts(model_id, provider_id="copilot")

    if wire:
        assert ui, (
            f"{model_id}: the runtime resolves {wire} but the UI offers nothing"
        )
        assert set(ui) <= set(wire), (
            f"{model_id}: UI offers levels the runtime will not send: "
            f"ui={ui} wire={wire}"
        )
    else:
        assert ui == [], (
            f"{model_id}: the runtime sends no reasoning field, so the control "
            f"must stay hidden (got {ui})"
        )


@pytest.mark.parametrize(
    "model_id,provider_id",
    [
        ("claude-opus-5", "anthropic"),
        ("gpt-5.5", "openai-codex"),
        ("deepseek-v4-flash", "custom:newapi"),
    ],
)
def test_gemini_clamp_does_not_touch_other_families(model_id, provider_id):
    """The Gemini clamp must not narrow anyone else's ladder."""
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert "xhigh" in efforts, (model_id, provider_id, efforts)


def test_generalized_model_families_and_suffixed_ids():
    test_models = [
        # GPT
        ("gpt-5.5", "custom:newapi"),
        ("gpt-6-ultra", "custom:newapi"),
        # Claude
        ("claude-sonnet-4-6-free", "opencode-zen"),
        ("claude-opus-4-7:free", "kilocode"),
        ("claude-sonnet-3-7-free", "opencode-zen"),
        # Qwen
        ("qwen-3-coder-free", "opencode-zen"),
        ("qwen-4-coder:free", "opencode-zen"),
        # Minimax
        ("minimax-m2.5-free", "opencode-zen"),
        ("minimax-m3-pro", "custom:newapi"),
        # Mimo
        ("mimo-v2.5-free", "opencode-zen"),
        ("mimo-v3-pro", "custom:newapi"),
        # GLM
        ("glm-5.1:free", "kilocode"),
        ("glm-6-pro", "custom:newapi"),
        # Step
        ("step-1.5:free", "kilocode"),
        ("step-2-pro", "custom:newapi"),
        # DeepSeek
        ("deepseek-v5-free", "custom:newapi"),
        ("deepseek-r3:free", "kilocode"),
        # Kimi
        ("kimi-k2.6-free", "opencode-zen"),
        ("kimi-k3-pro:free", "kilocode")
    ]

    for model_id, provider_id in test_models:
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
        assert set(efforts) >= {"low", "medium", "high"}, (
            f"Failed: {model_id} via {provider_id} should resolve reasoning support"
        )


def test_unsupported_model_families_and_versions():
    unsupported_models = [
        # GPT: only 5+ supports reasoning_effort — gpt-4o/4.1/3.5 must be excluded
        ("gpt-4o", "opencode-zen"),
        ("gpt-4o-mini", "opencode-zen"),
        ("gpt-4.1", "kilocode"),
        ("gpt-4-turbo", "custom:newapi"),
        ("gpt-3.5-turbo", "opencode-zen"),
        # Claude
        ("claude-sonnet-3.5", "opencode-zen"),
        ("claude-opus-3-5-free", "kilocode"),
        # Qwen
        ("qwen-2.5-coder-free", "opencode-zen"),
        ("qwen-2-7b-instruct", "custom:newapi"),
    ]

    for model_id, provider_id in unsupported_models:
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
        assert efforts == [], (
            f"Failed: {model_id} via {provider_id} should NOT resolve reasoning support"
        )


# ── position-independent DeepSeek version detection (#3650) ───────────────────
#
# The DeepSeek V/R-series check keys off the token immediately AFTER "deepseek"
# rather than requiring "deepseek" to lead the string. This keeps detection
# working when a provider slug is prepended (e.g. a custom aggregator rewriting
# @custom:name:DeepSeek-V4-Flash → "my-provider-deepseek-v4-flash"), while a
# provider slug that happens to start with "v"/"r" (e.g. "vertex") must NOT by
# itself satisfy the version guard.

@pytest.mark.parametrize(
    "model_id",
    [
        "vertex-deepseek-v4-flash",
        "my-provider-deepseek-r1",
        "newapi-deepseek-v5",
    ],
)
def test_deepseek_version_detected_after_provider_slug(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="custom:newapi")
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id}: DeepSeek V/R-series marker after a provider slug should "
        "expose reasoning efforts (position-independent detection, #3650)"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek-chat",
        "deepseek-coder",
        "vertex-deepseek-chat",
    ],
)
def test_deepseek_non_reasoning_variants_excluded(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="custom:newapi")
    assert efforts == [], (
        f"{model_id}: non-reasoning DeepSeek variant must NOT resolve reasoning "
        "support, and a 'v'/'r' provider slug must not falsely trigger it (#3650)"
    )


# ── custom provider nested Gemini routes (vertex/, gemini_cli/) ───────────────


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        # Ladders are model-specific and come from Google's published
        # "Controlling thinking" table. 3 Pro exposes only the two endpoints,
        # so asserting a fixed low/medium/high for every route would demand a
        # level the model does not accept.
        ("vertex/gemini-3.1-pro-preview", {"low", "medium", "high"}),
        ("vertex/gemini-3-pro-preview", {"low", "high"}),
        ("gemini_cli/gemini-3-pro-preview", {"low", "high"}),
    ],
)
def test_custom_nested_gemini_routes_expose_reasoning(model_id, expected):
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    )
    # The point of this test is that a nested/prefixed route still resolves a
    # usable ladder at all — plus that the ladder matches what the model
    # actually publishes.
    assert efforts, f"{model_id} via custom:newapi should expose reasoning efforts"
    assert set(efforts) == expected, (
        f"{model_id} via custom:newapi should expose exactly {sorted(expected)}"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "vertex/gemini-embedding-001",
        "vertex/gemini-3-pro-image-preview",
    ],
)
def test_custom_nested_gemini_routes_exclude_non_reasoning(model_id):
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    ) == [], f"{model_id} must not expose reasoning efforts"


@pytest.mark.parametrize(
    "model_id",
    [
        "vertex/gemini-1.5-pro",
        "vertex/gemini-1.5-flash",
        "gemini_cli/gemini-1.5-pro",
        "vertex/gemini-1.0-pro",
    ],
)
def test_custom_nested_gemini_pre_2_5_routes_exclude_reasoning(model_id):
    """Gemini thinking/reasoning controls are documented for the 2.5 series and
    3-era models only — 1.5 (and earlier) have no thinking support, so the
    nested-gateway detection must not expose a reasoning selector for them
    (a user could otherwise pick an effort the route then rejects)."""
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    ) == [], f"{model_id} (pre-2.5 Gemini) must not expose reasoning efforts"


