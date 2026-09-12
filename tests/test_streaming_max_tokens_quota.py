"""Regression coverage for WebUI streaming provider failure handling.

The incident this guards against: WebUI-created AIAgent instances did not pass
config.yaml's max_tokens, so a fallback Claude model via OpenRouter requested its
native 64k output ceiling and failed with HTTP 402 "more credits / fewer
max_tokens". The stream then looked like a stuck Thinking card instead of a
clear quota error.
"""
from pathlib import Path


STREAMING = Path(__file__).resolve().parents[1] / "api" / "streaming.py"


def _src() -> str:
    return STREAMING.read_text(encoding="utf-8")


def _compute_agent_cache_signature_source() -> str:
    """Return the source of the `_compute_agent_cache_signature()` helper.

    The signature blob used to be inlined in the streaming send path; it now
    lives in this helper so the initial send and both self-heal retry paths
    derive the signature from the same final runtime bundle.
    """
    src = _src()
    start = src.index("def _compute_agent_cache_signature(")
    end = src.index("\ndef ", start)
    return src[start:end]


def _signature_blob() -> str:
    """Return the `_json.dumps([...])` field list the signature hashes."""
    helper = _compute_agent_cache_signature_source()
    blob_start = helper.index("_sig_blob = _json.dumps")
    blob_end = helper.index("], sort_keys=True)", blob_start)
    return helper[blob_start:blob_end]


def _production_signature_calls() -> list[tuple[int, str]]:
    """Return `(offset, source)` for every production signature call site.

    Paren-balanced so the whole multi-line keyword-argument list is captured,
    and every live call site is returned so a retry path cannot silently drop a
    field that the initial send still passes. Commented-out occurrences are
    skipped -- a disabled retry call must read as missing, not as present.
    """
    src = _src()
    marker = "_agent_sig = _compute_agent_cache_signature("
    calls: list[tuple[int, str]] = []
    pos = src.find(marker)
    while pos != -1:
        line_start = src.rfind("\n", 0, pos) + 1
        if src[line_start:pos].strip():
            # Commented-out (or otherwise non-statement) occurrence: it no
            # longer runs, so it must not count as a live call site.
            pos = src.find(marker, pos + 1)
            continue
        depth = 0
        for idx in range(pos + len(marker) - 1, len(src)):
            char = src[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append((pos, src[pos:idx + 1]))
                    break
        else:
            raise AssertionError("unterminated _compute_agent_cache_signature( call")
        pos = src.find(marker, pos + 1)
    return calls


# Lifecycle anchors for the streaming send path. Pinning one signature call to
# each region keeps the oracle honest: a retry call that silently disappears
# fails the region check instead of hiding behind the calls that remain.
INITIAL_SEND_MARKER = "# ── Agent cache: reuse across messages in the same session ──"
RETURNED_ERROR_SELF_HEAL_MARKER = (
    "logger.info('[webui] self-heal: retrying stream after credential refresh')"
)
RAISED_EXCEPTION_SELF_HEAL_MARKER = (
    "logger.info('[webui] self-heal (except path): "
    "retrying stream after credential refresh')"
)

# Each region ends at the cache write that stores the signature it just
# computed. Bounding the region at the write -- rather than at the next
# lifecycle anchor or at EOF -- is what proves the signature is recomputed
# *before* it is cached: a call moved below its own write lands outside the
# region and fails, instead of passing while the write consumes a stale
# `_agent_sig`.
INITIAL_SEND_CACHE_WRITE_MARKER = (
    "SESSION_AGENT_CACHE[session_id] = (agent, _agent_sig)"
)
RETURNED_ERROR_SELF_HEAL_CACHE_WRITE_MARKER = (
    "_SAC[session_id] = (agent, _agent_sig)"
)
RAISED_EXCEPTION_SELF_HEAL_CACHE_WRITE_MARKER = (
    "_SAC2[session_id] = (_heal_agent, _agent_sig)"
)


def _marker_offset(marker: str) -> int:
    """Return the single offset of a lifecycle marker in streaming.py."""
    src = _src()
    offset = src.index(marker)
    assert src.find(marker, offset + 1) == -1, (
        "lifecycle marker is no longer unique in streaming.py:\n" + marker
    )
    return offset


def _signature_calls_by_region(
    calls: list[tuple[int, str]],
) -> dict[str, tuple[int, str]]:
    """Map each lifecycle region to the single signature call inside it.

    The regions are the initial send, the returned-error self-heal retry and
    the raised-exception self-heal retry; each runs from its lifecycle anchor
    to the cache write that consumes `_agent_sig`. Requiring exactly one call
    inside those bounds means neither a dropped retry call (masked by its
    surviving siblings) nor a call recomputed after its own cache write (which
    would leave the write storing a stale signature) can read as correct.
    """
    initial = _marker_offset(INITIAL_SEND_MARKER)
    initial_cache_write = _marker_offset(INITIAL_SEND_CACHE_WRITE_MARKER)
    returned_error = _marker_offset(RETURNED_ERROR_SELF_HEAL_MARKER)
    returned_error_cache_write = _marker_offset(
        RETURNED_ERROR_SELF_HEAL_CACHE_WRITE_MARKER
    )
    raised_exception = _marker_offset(RAISED_EXCEPTION_SELF_HEAL_MARKER)
    raised_exception_cache_write = _marker_offset(
        RAISED_EXCEPTION_SELF_HEAL_CACHE_WRITE_MARKER
    )
    assert (
        initial
        < initial_cache_write
        < returned_error
        < returned_error_cache_write
        < raised_exception
        < raised_exception_cache_write
    ), (
        "streaming.py lifecycle regions are out of order; these anchors no "
        "longer describe the send path"
    )

    bounds = {
        "initial send": (initial, initial_cache_write),
        "returned-error self-heal": (returned_error, returned_error_cache_write),
        "raised-exception self-heal": (
            raised_exception,
            raised_exception_cache_write,
        ),
    }
    by_region: dict[str, tuple[int, str]] = {}
    for region, (start, end) in bounds.items():
        found = [(offset, call) for offset, call in calls if start < offset < end]
        assert len(found) == 1, (
            "expected exactly one _compute_agent_cache_signature() call in the "
            f"{region} region of streaming.py -- between its lifecycle anchor "
            f"and the cache write that stores `_agent_sig` -- found "
            f"{len(found)}"
        )
        by_region[region] = found[0]
    return by_region


def test_streaming_passes_configured_max_tokens_to_agent():
    src = _src()
    assert "_raw_max_tokens = _cfg.get('max_tokens')" in src
    assert "_agent_cfg_for_tokens.get('max_tokens')" in src
    assert "_agent_kwargs['max_tokens'] = _max_tokens_cfg" in src


def test_streaming_agent_cache_signature_includes_max_tokens_and_fallback():
    blob = _signature_blob()
    assert "max_tokens_cfg or ''" in blob, (
        "_compute_agent_cache_signature() must hash max_tokens_cfg, or a "
        "max_tokens change reuses the agent built on the old output ceiling."
    )
    assert "fallback_resolved or {}" in blob, (
        "_compute_agent_cache_signature() must hash the resolved fallback "
        "chain so a fallback edit mints a new agent."
    )

    calls = _production_signature_calls()
    assert len(calls) == 3, (
        "expected exactly three _compute_agent_cache_signature() call sites "
        "(initial send plus both self-heal retries), found "
        f"{len(calls)}"
    )
    for region, (_offset, call) in _signature_calls_by_region(calls).items():
        assert "max_tokens_cfg=_max_tokens_cfg" in call, (
            f"the {region} signature call must pass the resolved "
            "max_tokens:\n" + call
        )
        assert "fallback_resolved=_fallback_resolved" in call, (
            f"the {region} signature call must pass the resolved fallback "
            "chain:\n" + call
        )


def test_openrouter_more_credits_error_is_classified_as_quota():
    src = _src()
    assert "'more credits' in _err_lower" in src
    assert "'can only afford' in _err_lower" in src
    assert "'fewer max_tokens' in _err_lower" in src
    assert "'more credits' in _exc_lower" in src
    assert "'can only afford' in _exc_lower" in src
    assert "'fewer max_tokens' in _exc_lower" in src
