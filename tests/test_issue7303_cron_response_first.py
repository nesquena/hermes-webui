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
    """A ``## Response`` that appears far into the artifact (well past
    the front-matter + system context region) is almost certainly
    quoted text inside an agent transcript. The parser caps the probe
    range so such false positives do not steal the projection.
    """
    # Build a file where the only ``## Response`` is past the probe
    # cap. We use the constant to keep the test honest if the cap
    # changes (the test still passes for any sufficiently large cap).
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
