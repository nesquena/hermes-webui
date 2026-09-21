"""Regression tests for #7303: cron run detail is response-first.

The shared parser (:mod:`api.cron_output_parser`) is the single source
of truth for the response / context / raw projection. The collapsed
preview, the expanded view, and the future raw-override affordance
all consume the same projection instead of re-parsing in click
handlers.
"""
from __future__ import annotations

import textwrap

from api.cron_output_parser import parse_cron_output, response_snippet


# ── Happy path: canonical ## Response heading ─────────────────────────


def test_standard_response_heading_is_recognized():
    text = textwrap.dedent(
        """\
        ---
        model: claude-opus-4
        run_id: abc
        ---

        ## Prompt

        You are an SRE bot. Check the cluster health.

        ## Response

        All 12 nodes are healthy. Latency p99 = 142 ms.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response_line == 10  # 1-indexed
    assert "All 12 nodes are healthy" in projection.response
    assert "SRE bot" in projection.context
    assert "## Prompt" in projection.context
    assert projection.raw == text


def test_alternate_h1_response_heading_is_also_recognized():
    """Some agent runs use ``# Response`` instead of ``## Response``.
    The parser accepts both so the response-first view does not fall
    back to raw for inconsistent runs.
    """
    text = textwrap.dedent(
        """\
        # Response

        Final reply.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert "Final reply." in projection.response


def test_response_text_is_stripped():
    text = textwrap.dedent(
        """\
        ## Response



        The response body has leading blank lines that should be trimmed.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response.startswith("The response body")


# ── Fail-closed boundaries ────────────────────────────────────────────


def test_response_heading_inside_fenced_code_block_is_ignored():
    """The parser must not treat a heading-shaped line that appears
    inside a fenced code block as a section boundary. A real cron
    run can quote shell output that contains ``## Response`` literally.

    The contract is narrow: we don't want the parser to *flip* the
    boundary because of code-block content. The response body itself
    can still contain the rest of the file (including a closing fence
    and the lines that follow) — the UI decides how to render that.
    """
    text = textwrap.dedent(
        """\
        ## Response

        Real response at the top.

        ```bash
        # The following is a script output that mentions Response:
        echo "## Response"
        echo "This is not a boundary"
        ```
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    # The response is the FIRST boundary, not the one inside the fence.
    assert "Real response at the top" in projection.response
    # The quoted text that mentioned ``## Response`` is in the response
    # body (after the first boundary), not in the context. The key
    # invariant is that the boundary is the *first* ``## Response`` we
    # found, not the deeper one inside the fence.
    assert "echo" in projection.response
    assert projection.context == ""


def test_long_fence_is_not_closed_by_short_fence():
    """A four-backtick fence must not be closed by three backticks.

    Skill dumps nest fences: a ```` ``` ```` line inside a ```` ````
    ```` block is content, not a terminator. Tracking only the fence
    character closed the block early, which let a ``## Response``
    inside the still-open block be accepted as the boundary — the
    projection then split the artifact in the wrong place.
    """
    text = (
        "Intro.\n\n"
        "````markdown\n"
        "```\n"
        "## Response\n"
        "quoted inside the still-open four-backtick block\n"
        "```\n"
        "````\n"
        "\n"
        "## Response\n"
        "\n"
        "The real reply.\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    # The boundary is the heading AFTER the closed fence, not the one
    # inside the four-backtick block.
    assert projection.response == "The real reply."
    assert "quoted inside" in projection.context


def test_fence_with_info_string_does_not_close_block():
    """A closing fence carries no info string. A tagged run of fence
    characters inside an open block is a nested opening fence.
    """
    text = (
        "Intro.\n\n"
        "````markdown\n"
        "```python\n"
        "print(1)\n"
        "```\n"
        "````\n"
        "\n"
        "## Response\n"
        "\n"
        "Done.\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "Done."


def test_response_heading_inside_html_pre_block_is_ignored():
    text = textwrap.dedent(
        """\
        <pre>
        # Response (inside pre)
        </pre>

        ## Response

        Real response below the pre block.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert "Real response below" in projection.response


def test_response_heading_only_inside_code_falls_back_to_raw():
    """When the only ``## Response``-shaped line is inside a fence, the
    parser must NOT treat it as a boundary. The caller falls back to
    the raw-primary view.
    """
    text = textwrap.dedent(
        """\
        Front-matter text.

        ```markdown
        ## Response
        This is quoted, not a real section.
        ```
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is False
    assert projection.response == ""
    assert projection.context.startswith("Front-matter")


def test_response_heading_deep_in_file_is_ignored():
    """The probe cap must stop an unbounded scan of pathological input,
    but it must NOT be so tight that a real artifact loses its boundary.

    #7303's motivating artifact carries ~320 context lines before
    ``## Response``; the previous 200-line cap made
    ``has_response_boundary`` False for exactly that case, so the
    response-first view silently fell back to raw-primary and the
    feature did nothing. This test pins the real-artifact contract.
    """
    # A boundary after 321 context lines must be found.
    padding = "\n".join("context line" for _ in range(321))
    text = f"Front-matter\n\n{padding}\n\n## Response\n\nThe answer is 42.\n"
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "The answer is 42."
    assert "Front-matter" in projection.context

    # The cap still exists: a boundary beyond it is not taken.
    from api.cron_output_parser import _MAX_PROBE_LINES

    padding = "\n".join("padding line " * 3 for _ in range(_MAX_PROBE_LINES + 50))
    text = f"front-matter\n\n{padding}\n\n## Response\n\nThis is too deep.\n"
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is False


def test_similar_heading_is_not_a_boundary():
    """A heading like ``## Response time`` must not be matched -- the
    boundary is exactly ``## Response`` / ``# Response`` so we never
    accidentally split a section that happens to start with the word
    ``Response``.
    """
    text = textwrap.dedent(
        """\
        ## Response time

        2.4 seconds.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is False
    # Legacy contract: when no boundary, response is empty and the
    # whole text becomes context.
    assert projection.response == ""
    assert "## Response time" in projection.context


# ── Empty / malformed inputs ──────────────────────────────────────────


def test_empty_input():
    projection = parse_cron_output("")
    assert projection.has_response_boundary is False
    assert projection.response == ""
    assert projection.context == ""


def test_input_with_no_heading_falls_back_to_raw_context():
    text = "Just a stream of agent output, no markdown headings."
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is False
    assert projection.context == text
    assert projection.response == ""


# ── Legacy snippet contract ───────────────────────────────────────────


def test_response_snippet_returns_response_when_boundary_present():
    text = textwrap.dedent(
        """\
        ## Response

        This is the agent reply.
        """
    )
    snippet = response_snippet(text, limit=80)
    assert "This is the agent reply" in snippet


def test_response_snippet_legacy_falls_back_to_full_text():
    """No boundary → legacy behaviour: return the full text, capped at
    *limit*. This is what ``_cron_output_snippet`` always did, and
    ``test_cron_run`` snapshot tests depend on it.
    """
    text = "front-matter only\n" * 50
    snippet = response_snippet(text, limit=20)
    assert len(snippet) <= 20


# ── to_dict shape used by the route layer ─────────────────────────────


def test_to_dict_matches_route_contract():
    text = textwrap.dedent(
        """\
        ## Response

        OK
        """
    )
    d = parse_cron_output(text).to_dict()
    assert set(d.keys()) == {
        "response", "context", "raw", "has_response_boundary", "response_line"
    }
    assert d["has_response_boundary"] is True
    assert d["response_line"] >= 1


# ── Route layer integration ───────────────────────────────────────────


def test_handle_cron_run_detail_surfaces_parsed_projection():
    """``/api/crons/run`` must include a ``parsed`` field so the UI
    can render the response-first view without re-parsing.
    """
    import re
    routes_src = open("api/routes.py").read()
    # The endpoint must include the parsed field in the success body.
    m = re.search(
        r'def _handle_cron_run_detail.*?return j\(handler,\s*\{[^}]*"parsed"',
        routes_src,
        re.DOTALL,
    )
    assert m, (
        "_handle_cron_run_detail must include the parsed projection in "
        "the success body so the UI can render response-first without "
        "re-parsing the artifact in the browser."
    )


def test_handle_cron_run_detail_omits_raw_from_projection():
    """``content`` already carries the verbatim artifact, so the
    projection must not ship ``raw`` as well — that doubles the payload
    of a large run for no benefit.
    """
    routes_src = open("api/routes.py").read()
    detail = routes_src[
        routes_src.index("def _handle_cron_run_detail"): routes_src.index("def _cron_output_usage_metadata")
    ]
    assert 'parsed_projection.pop("raw", None)' in detail


def test_handle_cron_output_listing_stays_bounded():
    """The list endpoint must NOT emit a ``parsed`` projection.

    ``_cron_output_content_window`` bounds each item to 8 KB; the
    projection carries full ``raw`` + ``context`` + ``response``, so
    adding it (up to 500 items) would return one large artifact
    several times over and make the listing unbounded. The projection
    belongs to the single-run detail route only.
    """
    routes_src = open("api/routes.py").read()
    start = routes_src.index("def _handle_cron_output")
    listing = routes_src[start : routes_src.index("def _handle_cron_status", start)]
    # The bounded content window is still the payload.
    assert '"content": _cron_output_content_window(txt),' in listing
    assert '"parsed"' not in listing
