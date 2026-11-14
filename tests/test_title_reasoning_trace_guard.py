"""Regression coverage for reasoning-trace leakage in WebUI session titles."""

from contextlib import nullcontext
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

import api.streaming as streaming
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config


BAD_TITLE_OUTPUTS = (
    "Title should be 3-8 words, matching the user's language (English), topic label s…",
    'Something like "Fix Session Title Generation" or "Audit Session Title Generation…',
    'Good title: "Compare Storage Recommendations" or "Compare Other Storage…',
    'Options: - "Durable Storage Options" - "Cost-Effective Storage…',
    "The title should be 3-8 words, matching the user's language (English), as a topi",
    'Something like "Project Docs Review and Filing" or "File Project Documents in Sub...',
    'Something like "Service Capacity Projection" or "Service Capaci...',
    "The title should be concise",
)

EXPECTED_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "session_title",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}


def _response(content):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]
    )


@pytest.mark.parametrize("candidate", BAD_TITLE_OUTPUTS)
def test_sanitizer_rejects_observed_reasoning_trace_titles(candidate):
    assert streaming._sanitize_generated_title(candidate) == ""


@pytest.mark.parametrize(
    "candidate",
    (
        "Fix session title generation",
        "Reparar botón de inicio móvil",
        "移动端登录按钮修复",
    ),
)
def test_sanitizer_preserves_valid_titles_in_multiple_languages(candidate):
    assert streaming._sanitize_generated_title(candidate) == candidate


@pytest.mark.parametrize(
    "content",
    (
        '{"title": "Fix login button on mobile"}',
        '```json\n{"title": "Fix login button on mobile"}\n```',
        'Here is the requested JSON:\n{"title": "Fix login button on mobile"}\nDone.',
    ),
)
def test_extract_title_response_parses_json_wrappers(content):
    assert streaming._extract_title_response(_response(content)) == (
        "Fix login button on mobile",
        "",
    )


def test_extract_title_response_parses_top_level_json_string():
    assert streaming._extract_title_response(_response('"Fix login button on mobile"')) == (
        "Fix login button on mobile",
        "",
    )


def test_sanitizer_rejects_quoted_alternatives_and_bulleted_options():
    assert streaming._sanitize_generated_title('"Fix title generation" or "Audit title generation"') == ""
    assert streaming._sanitize_generated_title('- "Fix title generation"\n- "Audit title generation"') == ""


def test_sanitizer_rejects_more_than_twelve_words_instead_of_truncating():
    candidate = "Investigate why the mobile login button stops responding after users rotate their devices twice"
    assert len(candidate.split()) > 12
    assert streaming._sanitize_generated_title(candidate) == ""


def test_overlong_title_response_retries_until_recovery_cap(monkeypatch):
    candidate = "Investigate why the mobile login button stops responding after users rotate their devices twice"
    session = types.SimpleNamespace(
        session_id="title-reasoning-fallback",
        title="Untitled",
        llm_title_generated=False,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Fix the mobile login button."},
            {"role": "assistant", "content": "I will inspect the mobile event handling."},
        ],
        save=MagicMock(),
    )
    events = []

    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(
        streaming,
        "generate_title_raw_via_aux",
        lambda *_args, **_kwargs: (candidate, "llm_aux"),
    )
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Fix the mobile login button.",
        assistant_text="I will inspect the mobile event handling.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    assert session.title == "Untitled"
    session.save.assert_not_called()
    assert session.llm_title_generated is False
    # Preserve master's bounded recovery: invalid output is retried on later
    # exchanges, then a local summary is accepted at the existing cap.
    session.messages *= streaming._TITLE_RECOVERY_MAX_EXCHANGES
    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Fix the mobile login button.",
        assistant_text="I will inspect the mobile event handling.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )
    assert session.title == "Fix mobile login button"
    assert session.save.call_args.kwargs == {"touch_updated_at": False}
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "fallback"
    assert status[-1]["reason"] == "local_summary:llm_invalid_aux"


def test_aux_title_request_uses_strict_json_schema():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "Fix login button on mobile"}')

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "openai", "model": "gpt-4o-mini"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Why is login broken on mobile?",
                    "The click handler is not attached.",
                )

    assert result == "Fix login button on mobile"
    assert status == "llm_aux"
    assert captured["extra_body"]["response_format"] == EXPECTED_RESPONSE_FORMAT


def test_aux_schema_request_sends_reasoning_disabler():
    """Re-gate finding 3: the schema request carries the same route-gated
    reasoning disable as the compatibility request, so reasoning models do
    not burn the 512-token title budget on the first call (#2083 class)."""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response('{"title": "Schema Works"}')

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "schema-route"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a strict schema route.",
                    "The route supports response_format.",
                )

    assert result == "Schema Works"
    assert status == "llm_aux"
    assert len(calls) == 1
    assert calls[0]["extra_body"]["response_format"] == EXPECTED_RESPONSE_FORMAT
    assert calls[0]["extra_body"]["reasoning"] == {"enabled": False}


def test_aux_schema_request_omits_reasoning_for_routes_that_reject_it():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        extra = kwargs.get("extra_body") or {}
        if "reasoning" in extra:
            raise ValueError("strict gateway rejects reasoning")
        return _response("No Reasoning Route Title")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "openai", "model": "gpt-4o-mini"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use an OpenAI-compatible title route.",
                    "OpenAI routes reject the reasoning extra (#4161).",
                )

    assert result == "No Reasoning Route Title"
    assert status == "llm_aux"
    assert len(calls) == 1
    assert "reasoning" not in calls[0]["extra_body"]


def test_aux_schema_rejection_falls_back_to_existing_request_shape():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise ValueError("response_format unsupported")
        return _response("Compatibility Fallback Title")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "legacy-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a legacy title route.",
                    "The route rejects response_format.",
                )

    assert result == "Compatibility Fallback Title"
    assert status == "llm_aux_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning": {"enabled": False}}


def test_aux_schema_reasoning_only_retries_with_reasoning_disabled_shape():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            return {
                "choices": [
                    {
                        "message": {"content": "", "reasoning": "hidden reasoning"},
                        "finish_reason": "length",
                    }
                ]
            }
        return _response("Compatibility Reasoning Fallback")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "reasoning-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a reasoning title route.",
                    "The schema request returns reasoning only.",
                )

    assert result == "Compatibility Reasoning Fallback"
    assert status == "llm_aux_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning": {"enabled": False}}


def test_extract_title_response_rejects_json_without_string_title():
    assert streaming._extract_title_response(_response('{"options": ["one", "two"]}')) == (
        "",
        "llm_empty",
    )


def test_agent_openai_title_request_uses_strict_json_schema():
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "Fix login button on mobile"}')

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "openai"
    agent.model = "gpt-4o-mini"
    agent.base_url = "https://api.openai.com/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is login broken on mobile?",
        "The click handler is not attached.",
    )

    assert result == "Fix login button on mobile"
    assert status == "llm"
    assert captured["extra_body"]["response_format"] == EXPECTED_RESPONSE_FORMAT
    assert agent.reasoning_config is None


def test_agent_schema_rejection_falls_back_to_existing_request_shape():
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise ValueError("response_format unsupported")
        return _response("Compatibility Agent Title")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "minimax"
    agent.model = "minimax-m2"
    agent.base_url = "https://api.minimaxi.com/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is the title route failing?",
        "The endpoint rejects response_format.",
    )

    assert result == "Compatibility Agent Title"
    assert status == "llm_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning_split": True}
    assert agent.reasoning_config is None


@pytest.mark.parametrize(
    "schema_response",
    (
        _response(""),
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "hidden reasoning"},
                    "finish_reason": "length",
                }
            ]
        },
    ),
)
def test_agent_empty_schema_response_tries_compatibility_shape(schema_response):
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return schema_response
        return _response("Compatibility After Empty Schema")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "custom"
    agent.model = "reasoning-gateway"
    agent.base_url = "https://reasoning.example/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.side_effect = lambda messages: {
        "messages": messages,
        "extra_body": {"reasoning_effort": "none"},
    }
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is the schema response empty?",
        "Compatibility mode can still return a title.",
    )

    assert result == "Compatibility After Empty Schema"
    assert status == "llm_retry"
    assert len(calls) == 2
    assert calls[1]["messages"] == calls[0]["messages"]
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"]
    assert calls[1]["extra_body"] == {"reasoning_effort": "none"}
    assert agent.reasoning_config is None


LEGITIMATE_PERSISTED_TITLES = (
    "Topic Label Accessibility",
    "Maybe Monad Error Handling",
    "Understanding 3-8 Words in Regex",
    "The Good Title Debate",
    "A Good Title for Your Novel",
    "Something Like Summer Discussion",
    '"Merge" or "Rebase" in Git',
    "Parsing <analysis> Tags",
    "Title Shoulders of Giants",
    "The Title Shoulders Story",
    "Good Title: A Style Guide",
    "Options: Portfolio Optimization",
    'Something Like "Summer" Discussion',
    "Analysis: Q3 Sales Trends",
    "Thinking: Fast and Slow Summary",
    "Reasoning: Deductive vs Inductive",
    "Thought: Experiments in Ethics",
    "Options: Stocks, Bonds, ETFs",
    "Title Should Reflect Content",
    # Re-gate 2026-10-21 (db59f307) finding 1: heading prefixes followed by a
    # bare pronoun/"user" must survive as ordinary persisted subject matter.
    "Analysis: User Retention",
    "Analysis: User Authentication",
    "Thinking: You and Your Research",
    # Title-cased style-guide subjects that share prompt vocabulary.
    "Title Should Include Full Sentences",
    "Title Should Match Topic Labels",
    "Title should include full sentences",
    "Title should match topic labels",
    # One quoted name with appositives is a single title, not a menu.
    'Options: "Summer", a Memoir, Revised Edition',
    'Good Title: "Summer", a Memoir, Revised Edition',
    'Something Like "Summer", a Memoir, Revised Edition',
    # Round-15 audit: prompt words, quoted words, hyphens, semicolons and
    # bare tags in one saved title are subject matter.
    "Title should be a topic label",
    "Title should be full sentences",
    "Title should: Include Full Sentences",
    'Options: "Yes", "No", and the Art of Choosing',
    'Good Title: "Who", "What", and Why',
    'Options: "REST" or "GraphQL" Comparison',
    "Options: Client - Server - Architecture",
    "Good Title: Summer; a Memoir",
    "<analysis> Element Reference",
    '<analysis mode="fast"> Tag Guide',
    # Round-16 audit: the length rule needs a delimited range and a whole
    # word, and a wrapper needs matching tag names.
    "Title should be 38 WordPress Themes",
    "Title should be three to eight Wordsmith Exercises",
    "Title should be 3-8 wordsworth poems",
    "Title should be 38 words",
    "<analysis-panel> vs </analysis> XML Validation",
    "<thought-experiment> vs </thought> XML Validation",
    "Title should be italicized, not 3-8 words",
    'Title should be "Beyond 3-8 Words"',
    # Round-17 audit: quoted attributes, self-closing tags, direct menu
    # preambles with subject prose, and singular "word" are subjects.
    '<analysis data="> </analysis>"> Attribute Parsing',
    '<reasoning title="> </reasoning>"> Attribute Syntax',
    "<analysis /> Widget </analysis> Reference",
    'Title Suggestions: "Merge" or "Rebase" in Git',
    'Title Options: "Call Options" or "Put Options" Comparison',
    "Title should be 3-8 word-for-word Quotations",
    "Title should be three to eight word Headlines",
    # Round-18 audit: saved titles with other tag names are subjects.
    "<analysis><!-- </analysis> --> HTML Comment Parsing",
    "<reasoning><![CDATA[</reasoning>]]> XML CDATA Guide",
    '<analysis><span title="</analysis>"> Nested Attribute Parsing',
    "< analysis>Tag Spacing</analysis> Guide",
)

SCREENSHOT_PERSISTED_TRACES = (
    "The title should be 3-8 words, matching the user's language (English), as a topi",
    'Something like "Project Docs Review and Filing" or "File Project Documents in Sub...',
)

ADVERSARIAL_TRACE_TITLES = (
    "<think >secret</think> Safe Title",
    "<analysis>We need to inspect this</analysis> Safe Title",
    "<|channel|>analysis We need to inspect this",
    "Analysis: We need to fix login button",
)


@pytest.mark.parametrize("candidate", LEGITIMATE_PERSISTED_TITLES)
def test_persisted_title_check_accepts_ordinary_subject_matter(candidate):
    assert streaming._looks_invalid_generated_title(candidate) is False
    session = types.SimpleNamespace(
        title=candidate,
        llm_title_generated=True,
        messages=[
            {"role": "user", "content": "Explain the topic."},
            {"role": "assistant", "content": "Here is the explanation."},
        ],
    )
    assert streaming._background_title_generation_inputs(session) is None


@pytest.mark.parametrize(
    "candidate",
    (
        "Something Like Summer Discussion",
        '"Merge" or "Rebase" in Git',
        "Parsing <analysis> Tags",
        "Title Shoulders of Giants",
        "The Title Shoulders Story",
        "Good Title: A Style Guide",
        "Options: Portfolio Optimization",
        'Something Like "Summer" Discussion',
        "Analysis: Q3 Sales Trends",
        "Thinking: Fast and Slow Summary",
        "Reasoning: Deductive vs Inductive",
        "Thought: Experiments in Ethics",
        "Options: Stocks, Bonds, ETFs",
        "Title Should Reflect Content",
        "Analysis: User Retention",
        "Analysis: User Authentication",
        "Thinking: You and Your Research",
        "Title Should Include Full Sentences",
        "Title Should Match Topic Labels",
        "Title should include full sentences",
        "Title should match topic labels",
        'Options: "Summer", a Memoir, Revised Edition',
        'Good Title: "Summer", a Memoir, Revised Edition',
        'Something Like "Summer", a Memoir, Revised Edition',
        "Title should be a topic label",
        "Title should be full sentences",
        "Title should: Include Full Sentences",
        'Options: "Yes", "No", and the Art of Choosing',
        'Good Title: "Who", "What", and Why',
        'Options: "REST" or "GraphQL" Comparison',
        "Options: Client - Server - Architecture",
        "Good Title: Summer; a Memoir",
        "<analysis> Element Reference",
        '<analysis mode="fast"> Tag Guide',
        "Title should be 38 WordPress Themes",
        "Title should be three to eight Wordsmith Exercises",
        "Title should be 3-8 wordsworth poems",
        "Title should be 38 words",
        "<analysis-panel> vs </analysis> XML Validation",
        "<thought-experiment> vs </thought> XML Validation",
        "Title should be italicized, not 3-8 words",
        'Title should be "Beyond 3-8 Words"',
        '<analysis data="> </analysis>"> Attribute Parsing',
        '<reasoning title="> </reasoning>"> Attribute Syntax',
        "<analysis /> Widget </analysis> Reference",
        'Title Suggestions: "Merge" or "Rebase" in Git',
        'Title Options: "Call Options" or "Put Options" Comparison',
        "Title should be 3-8 word-for-word Quotations",
        "Title should be three to eight word Headlines",
        "<analysis><!-- </analysis> --> HTML Comment Parsing",
        "<reasoning><![CDATA[</reasoning>]]> XML CDATA Guide",
        '<analysis><span title="</analysis>"> Nested Attribute Parsing',
        "< analysis>Tag Spacing</analysis> Guide",
    ),
)
def test_persisted_subject_matter_survives_normal_send(candidate, monkeypatch):
    session = types.SimpleNamespace(
        session_id="title-false-positive-persist",
        title=candidate,
        llm_title_generated=True,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Explain the topic."},
            {"role": "assistant", "content": "Here is the explanation."},
        ],
        save=MagicMock(),
    )
    events = []
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(
        streaming,
        "_generate_llm_session_title_via_aux",
        MagicMock(return_value=("Silent Rename Victim", "llm_aux", "Silent Rename Victim")),
    )
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Explain the topic.",
        assistant_text="Here is the explanation.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    streaming._generate_llm_session_title_via_aux.assert_not_called()
    assert session.title == candidate
    session.save.assert_not_called()
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "skipped"
    assert status[-1]["reason"] == "already_generated"


def test_new_candidates_still_reject_embedded_traces_and_quoted_alternatives():
    assert streaming._sanitize_generated_title("Parsing <analysis> Tags") == ""
    assert streaming._sanitize_generated_title('"Merge" or "Rebase" in Git') == ""
    assert streaming._looks_invalid_generated_title("Parsing <analysis> Tags") is False
    assert streaming._looks_invalid_generated_title('"Merge" or "Rebase" in Git') is False
    # Persisted keeps only unambiguous trace anchors, but sentence echoes of
    # the prompt (previously persisted-only) remain candidate-only rejects.
    assert streaming._looks_invalid_generated_title("The title should be concise") is False
    assert streaming._is_bad_new_title("The title should be concise") is True


@pytest.mark.parametrize("candidate", SCREENSHOT_PERSISTED_TRACES)
def test_persisted_title_check_self_heals_screenshot_traces(candidate):
    assert streaming._looks_invalid_generated_title(candidate) is True
    session = types.SimpleNamespace(
        title=candidate,
        llm_title_generated=True,
        messages=[
            {"role": "user", "content": "File the project documents."},
            {"role": "assistant", "content": "I will inspect the project packet."},
        ],
    )
    assert streaming._background_title_generation_inputs(session) is not None


@pytest.mark.parametrize("candidate", ADVERSARIAL_TRACE_TITLES)
def test_sanitizer_rejects_whitespace_and_provider_trace_wrappers(candidate):
    assert streaming._is_bad_new_title(candidate) is True
    assert streaming._sanitize_generated_title(candidate) == ""


def test_schema_extract_accepts_only_json_object_with_string_title():
    assert streaming._extract_title_text(
        '{"title": "Fix login button on mobile"}',
        mode="schema",
    ) == "Fix login button on mobile"
    assert streaming._extract_title_text("Fix login button on mobile", mode="schema") == ""
    assert streaming._extract_title_text('"Fix login button on mobile"', mode="schema") == ""


@pytest.mark.parametrize(
    "content",
    (
        '{"foo": "bar',
        '{"title": "None"}',
        '{"title": "undefined"}',
        '{"title": "null"}',
    ),
)
def test_schema_extract_fails_closed_on_malformed_json_and_sentinels(content):
    assert streaming._extract_title_text(content, mode="schema") == ""
    assert streaming._extract_title_response(_response(content), mode="schema") == (
        "",
        "llm_empty",
    )


def test_compatibility_extract_still_accepts_prose_and_json_string():
    assert streaming._extract_title_response(
        _response("Fix login button on mobile"),
        mode="compatibility",
    ) == ("Fix login button on mobile", "")
    assert streaming._extract_title_response(
        _response('"Fix login button on mobile"'),
        mode="compatibility",
    ) == ("Fix login button on mobile", "")


def test_legitimate_existing_title_is_not_self_healed_on_background_update(monkeypatch):
    session = types.SimpleNamespace(
        session_id="keep-maybe-monad-title",
        title="Maybe Monad Error Handling",
        llm_title_generated=True,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Explain maybe monads."},
            {"role": "assistant", "content": "A maybe monad models optional values."},
        ],
        save=MagicMock(),
    )
    events = []

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("title generation must not rerun for a legitimate persisted title")

    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", fail_if_called)
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Explain maybe monads.",
        assistant_text="A maybe monad models optional values.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    assert session.title == "Maybe Monad Error Handling"
    assert session.save.call_count == 0
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "skipped"
    assert status[-1]["reason"] == "already_generated"


@pytest.mark.parametrize("title", ("Analysis: User Needs Assessment", "Analysis: I Think Therefore I Am", "Thinking: User Needs Assessment", "Analysis: We Can Do It Poster", "Analysis: I Will Survive Lyrics", "Analysis: You Should Know Podcast", "Thinking: I Think Therefore I Am", "Analysis: They Said Yes Wedding", "Thought: Let Me In Review"))
def test_persisted_title_worker_leaves_subjects_unchanged(title, tmp_path, monkeypatch):
    import api.models as models
    from api.models import Session
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    session = Session(session_id="persisted-title", title=title, messages=[{"role": "user", "content": "A topic."}, {"role": "assistant", "content": "Details."}], llm_title_generated=True, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    session.save()
    path = directory / "persisted-title.json"
    original = path.read_bytes()
    calls, events = [], []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: calls.append(kw), create=True):
            streaming._run_background_title_update(session_id=session.session_id, user_text="A topic.", assistant_text="Details.", placeholder_title="Untitled", put_event=lambda n, d: events.append((n, d)), agent=None)
    assert path.read_bytes() == original
    assert calls == []
    assert not [1 for name, _ in events if name == "title"]
    assert [d["reason"] for n, d in events if n == "title_status"][-1] == "already_generated"


def test_legitimate_existing_title_survives_normal_turn(tmp_path, monkeypatch):
    import queue
    import sys

    import api.config as config
    import api.models as models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "keep-maybe-monad-send"
    stream_id = "stream-keep-maybe-monad"
    original_title = "Maybe Monad Error Handling"
    session = Session(
        session_id=sid,
        title=original_title,
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Explain maybe monads."},
            {"role": "assistant", "content": "A maybe monad models optional values."},
        ],
        llm_title_generated=True,
        manual_title=False,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Give another example."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[sid] = session
    streaming.SESSIONS[sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 4
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("Option types are a maybe monad.")
            return {
                "completed": True,
                "final_response": "Option types are a maybe monad.",
                "messages": [
                    {"role": "user", "content": "Explain maybe monads."},
                    {"role": "assistant", "content": "A maybe monad models optional values."},
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "Option types are a maybe monad."},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    title_update_calls = []

    def track_title_update(*args, **kwargs):
        title_update_calls.append((args, kwargs))

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_run_background_title_update", track_title_update)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="Give another example.",
        model="gpt-4o",
        workspace=str(tmp_path),
        stream_id=stream_id,
    )

    assert session.title == original_title
    assert session.llm_title_generated is True
    assert streaming._background_title_generation_inputs(session) is None
    assert title_update_calls == []


def test_minimax_aux_schema_request_keeps_reasoning_split():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "MiniMax Schema Title"}')

    with auxiliary_client_modules():
        with patch_tg_config(
            {
                "provider": "minimax",
                "model": "minimax-m2",
                "base_url": "https://api.minimaxi.com/v1",
            }
        ):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Why is login broken on mobile?",
                    "The click handler is not attached.",
                )

    assert result == "MiniMax Schema Title"
    assert status == "llm_aux"
    extra = captured["extra_body"]
    assert extra["response_format"] == EXPECTED_RESPONSE_FORMAT
    assert extra["reasoning_split"] is True
    # Re-gate finding 3: MiniMax is not a reasoning-extra-rejecting route, so
    # the schema request carries the reasoning disable too (not just the split).
    assert extra["reasoning"] == {"enabled": False}


@pytest.mark.parametrize("candidate", (
    'Something Like "Summer" Discussion',
    "Analysis: Q3 Sales Trends",
    "Thinking: Fast and Slow Summary",
    "Reasoning: Deductive vs Inductive",
    "Thought: Experiments in Ethics",
    "Title Should Reflect Content",
    "The title should be concise",
    "Title should be a concise topic label",
    "Title should: Something Else Entirely",
    '<analysis mode="fast">We need a title</analysis> Login Repair',
    "<analysis>We need to inspect this</analysis> Safe Title",
    "Analysis: User Retention",
    "Analysis: User Authentication",
    "Thinking: You and Your Research",
))
def test_ambiguous_headings_are_candidate_only(candidate):
    assert not streaming._looks_invalid_generated_title(candidate)
    assert streaming._is_bad_new_title(candidate)


@pytest.mark.parametrize("candidate", (
    "Maybe Monad Error Handling",
    "Options: Portfolio Optimization",
    "Topic Label Accessibility",
    "Options: Stocks, Bonds, ETFs",
))
def test_subject_like_keywords_are_not_candidate_rejection_evidence(candidate):
    assert not streaming._looks_invalid_generated_title(candidate)
    assert not streaming._is_bad_new_title(candidate)


@pytest.mark.parametrize("trace", (
    "Analysis: We need to fix login button",
    "Thinking: Let me reconsider the scope",
    "Reasoning: They asked for a summary",
    "Thought: I should double-check the parser",
    "Analysis: the user wants a breakdown of costs",
    "Reasoning: okay, so the parser needs a fix",
    "Analysis: I think we should file by quarter",
    "Thinking: You asked for a weekly summary",
))
def test_heading_trace_shapes_are_candidate_only(trace):
    """Heading clauses are rejected for new candidates, never used to rename persisted titles."""
    assert streaming._looks_invalid_generated_title(trace) is False
    assert streaming._is_bad_new_title(trace)


def test_aux_schema_prose_response_is_reused_without_second_provider_call():
    """Re-gate finding 2 (aux route): transports that silently strip
    ``response_format`` answer the schema request with a valid plain title.
    Parse that SAME response with the compatibility extractor instead of
    re-sending the identical request (2 provider calls -> 1)."""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("Reuse Prose Title")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "strips-response-format"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a schema-stripping route.",
                    "The route ignores response_format.",
                )

    assert result == "Reuse Prose Title"
    assert status == "llm_aux"
    assert len(calls) == 1
    assert "response_format" in (calls[0]["extra_body"] or {})


def test_agent_schema_prose_response_is_reused_without_second_provider_call():
    """Re-gate finding 2 (agent route): same prose-reuse contract as the aux
    route when the transport drops the schema request shape."""
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _response("Reuse Agent Prose Title")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "custom"
    agent.model = "strips-response-format"
    agent.base_url = "https://strips.example/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is the schema response prose?",
        "The transport strips response_format.",
    )

    assert result == "Reuse Agent Prose Title"
    assert status == "llm"
    assert len(calls) == 1
    assert "response_format" in (calls[0].get("extra_body") or {})


def test_aux_schema_unshaped_error_fails_without_schema_fallback_call():
    """Re-gate finding (low): a 401/429/timeout on the schema attempt is a
    transport failure, not proof the route lacks JSON-schema support — do not
    re-label it 'schema unavailable' and re-send the identical request."""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        raise ValueError("Too Many Requests (429)")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "rate-limited"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Probe the rate limited route.",
                    "The provider throttles every call.",
                )

    assert result is None
    assert status == "llm_error_aux"
    # One attempt per prompt only: no silent compatibility-mode re-send.
    assert len(calls) == 2


def test_aux_schema_response_format_rejection_still_falls_back():
    """A schema-shaped 4xx (naming response_format/json_schema) is a real
    schema rejection and keeps the existing compatibility fallback."""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise ValueError("400 Bad Request: response_format is not supported")
        return _response("Schema Rejection Fallback Title")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "legacy-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a legacy title route.",
                    "The route rejects response_format.",
                )

    assert result == "Schema Rejection Fallback Title"
    assert status == "llm_aux_retry"
    assert len(calls) == 2
    assert "response_format" in (calls[0]["extra_body"] or {})
    assert calls[1]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("payload", ('{"foo": "bar', '{"title": "Broken', '["Broken', '```json\n{"foo": "bar\n```'))
def test_malformed_schema_recovers_on_both_routes(route, payload, monkeypatch):
    calls = []
    def respond(**kwargs):
        calls.append(kwargs)
        return _response(payload if len(calls) == 1 else "Valid Recovery Title")
    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, _ = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
    else:
        agent = MagicMock()
        agent.api_mode = "openai"
        agent.provider = "custom"
        agent.model = "fixture"
        agent.base_url = "https://example.invalid/v1"
        agent.reasoning_config = None
        agent._build_api_kwargs.return_value = {}
        agent._ensure_primary_openai_client.return_value = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=respond)))
        result, _ = streaming.generate_title_raw_via_agent(agent, "Fix login.", "Inspect login.")
    assert result == "Valid Recovery Title"
    assert len(calls) == 2
    assert "response_format" not in (calls[1].get("extra_body") or {})


@pytest.mark.parametrize("status", (401, 403, 408, 429, 500, 503))
def test_non_schema_http_errors_never_retry_as_schema_rejections(status):
    error = RuntimeError(f"HTTP {status}: request included response_format json_schema")
    error.status_code = status
    assert not streaming._schema_rejection_error(error)


@pytest.mark.parametrize("message", (
    "429 Too Many Requests: response_format", "401 unauthorized response_format",
    "timeout waiting for response_format", "connection error json_schema",
))
def test_non_schema_text_errors_never_retry_as_schema_rejections(message):
    assert not streaming._schema_rejection_error(RuntimeError(message))


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_malformed_schema_worker_saves_and_publishes_recovered_title(route, tmp_path, monkeypatch):
    import json
    import api.models as models
    from api.models import Session
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    session = Session(session_id="malformed-schema-worker", title="Untitled", messages=[
        {"role": "user", "content": "Fix login."},
        {"role": "assistant", "content": "Inspect login."},
    ], llm_title_generated=False, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    monkeypatch.setattr("api.state_sync.sync_session_title", lambda *a, **k: None)
    calls = []
    def respond(**kwargs):
        calls.append(kwargs)
        return _response('{"foo": "bar' if len(calls) == 1 else "Valid Recovery Title")
    agent = None
    if route == "agent":
        agent = MagicMock()
        agent.api_mode = "openai"
        agent.provider = "custom"
        agent.model = "fixture"
        agent.base_url = "https://example.invalid/v1"
        agent.reasoning_config = None
        agent._build_api_kwargs.return_value = {}
        agent._ensure_primary_openai_client.return_value = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=respond)))
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session_id=session.session_id, user_text="Fix login.", assistant_text="Inspect login.",
                placeholder_title="Untitled", put_event=lambda name, data: events.append((name, data)), agent=agent)
    assert len(calls) == 2
    saved = json.loads((session_dir / (session.session_id + ".json")).read_text(encoding="utf-8"))
    assert saved["title"] == "Valid Recovery Title"
    assert saved["llm_title_generated"] is True
    assert [data["title"] for name, data in events if name == "title"] == ["Valid Recovery Title"]


# ── Re-gate 2026-10-21 at db59f307 (must-fix 2/3, should-fix 1-3) ────────────

TRACE_SENTENCE = 'I should not use {"title": "Rejected Candidate"} for this conversation.'
EXTRA_KEY_PAYLOAD = '{"title": "Unexpected Title", "extra": "forbidden"}'


def _fixture_agent(respond):
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "custom"
    agent.model = "fixture"
    agent.base_url = "https://example.invalid/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=respond)))
    return agent


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_reasoning_embedded_title_falls_through_to_compatibility(route):
    """Re-gate finding 2: the schema attempt on the rejected-candidate
    sentence fails closed; the same-response compatibility reuse must not pull
    the embedded candidate out of the reasoning sentence; the caller falls
    through to the real compatibility request, whose canned answer wins."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(TRACE_SENTENCE if len(calls) == 1 else "Valid Recovery Title")

    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, status = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
        assert status == "llm_aux_retry"
    else:
        result, status = streaming.generate_title_raw_via_agent(
            _fixture_agent(respond), "Fix login.", "Inspect login.")
        assert status == "llm_retry"
    assert result == "Valid Recovery Title"
    assert "Rejected Candidate" not in result
    # schema attempt + real compatibility request — no second schema pass
    assert len(calls) == 2
    assert "response_format" not in (calls[1].get("extra_body") or {})


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_persistent_reasoning_trace_ends_as_invalid_model_output(route):
    """Re-gate finding 2 (end state): a transport that keeps answering with
    the reasoning sentence yields an invalid-model-output status (the
    llm_invalid_trace family), which suppresses the local fallback."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(TRACE_SENTENCE)

    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, status = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_invalid_trace_aux")
    else:
        result, status = streaming.generate_title_raw_via_agent(
            _fixture_agent(respond), "Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_invalid_trace")
    # prompt 1: schema + compatibility resend; prompt 2: compatibility only
    assert len(calls) == 3


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_extra_key_title_object_is_not_accepted(route):
    """Re-gate finding 2b: a parsed object whose keys are not exactly
    {"title"} fails closed in both modes even with a string title field."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(EXTRA_KEY_PAYLOAD)

    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, status = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_empty_aux")
    else:
        result, status = streaming.generate_title_raw_via_agent(
            _fixture_agent(respond), "Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_empty")
    assert len(calls) == 3


@pytest.mark.parametrize("trace", (
    "The title should be 3-8 words, as a topic label",
    "Title options: Stocks; Bonds; ETFs",
    'Title options: "Fix Login Flow" or "Audit Login Flow"',
))
def test_unambiguous_prompt_echoes_still_self_heal(trace):
    assert streaming._looks_invalid_generated_title(trace) is True
    assert streaming._is_bad_new_title(trace) is True


@pytest.mark.parametrize("content", (
    TRACE_SENTENCE,
    'Here is my thinking: {"title": "Wrong One"}',
    "Sure! {\"foo\": \"bar",
    'Here is the JSON: {"title": "Login Flow',
    EXTRA_KEY_PAYLOAD,
    '{"extra": "first", "title": "Unexpected Title"}',
))
def test_compatibility_extract_fails_closed_on_reasoning_and_broken_json(content):
    """Full-text reasoning guards run before embedded extraction; malformed
    JSON behind a short preamble never returns as raw prose; objects that
    violate additionalProperties fail closed."""
    assert streaming._extract_title_text(content, mode="compatibility") == ""


@pytest.mark.parametrize("content", (
    "<think>I should choose a title.</think>\nMobile Login Repair",
    "Let me think through this.\nMobile Login Repair",
    "Here's my thinking process: short.\nMobile Login Repair",
    "The user is asking about login.\nMobile Login Repair",
))
def test_compatibility_extract_reuses_visible_title_after_thinking(content):
    assert streaming._extract_title_text(content, mode="compatibility") == "Mobile Login Repair"


def test_completed_think_wrapper_embedded_candidate_does_not_block_visible_title():
    content = '<think>I should not use {"title": "Rejected Candidate"}.</think>\nMobile Login Repair'
    assert streaming._extract_title_text(content, mode="compatibility") == "Mobile Login Repair"


def test_unwrapped_reasoning_with_embedded_json_candidate_fails_closed():
    content = 'I should not use {"title": "Rejected Candidate"}.\nMobile Login Repair'
    assert streaming._extract_title_text(content, mode="compatibility") == ""


def test_compatibility_extract_keeps_bracket_prose_and_preamble_valid_json():
    """Should-fix 3 (bracket prose) and the retained recovery semantics."""
    assert streaming._extract_title_text("[Bug] Login Fails On Submit", mode="compatibility") == (
        "[Bug] Login Fails On Submit"
    )
    assert streaming._extract_title_text('Sure. {"title": "Login Flow"}', mode="compatibility") == (
        "Login Flow"
    )
    assert streaming._extract_title_text(EXTRA_KEY_PAYLOAD, mode="schema") == ""


@pytest.mark.parametrize("message", (
    "This response_format type is unavailable now",
    "response_format must be one of text, json_object",
    "response_format 参数非法。",
    "'response_format' of type 'json_schema' is not supported for this model (max_tokens 512)",
    "Structured outputs are not supported for this model",
))
def test_schema_rejection_text_evidence_permits_fallback(message):
    """Must-fix 3 + should-fix 1: real schema-unavailable rejections permit the
    compatibility resend — "unavailable now", ``must be one of``, non-English
    bodies and ``max_tokens 512`` are no longer misclassified."""
    assert streaming._schema_rejection_error(RuntimeError(message)) is True


def test_schema_rejection_http_status_is_authoritative():
    """Should-fix 1: when the failure carries an HTTP status, that status is
    the verdict; message text cannot re-label a throttling 429."""
    available = RuntimeError("This response_format type is unavailable now")
    available.status_code = 400
    assert streaming._schema_rejection_error(available) is True
    throttled = RuntimeError("This response_format type is unavailable now")
    throttled.status_code = 429
    assert streaming._schema_rejection_error(throttled) is False


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_worker_reasoning_trace_recovery_saves_canned_recovery(route, tmp_path, monkeypatch):
    """Worker-level (must-fix 2): the embedded candidate of the reasoning
    sentence is never saved or published; the compatibility recovery decides
    the persisted title."""
    import json
    import api.models as models
    from api.models import Session
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    session = Session(session_id="trace-worker-recovery", title="Untitled", messages=[
        {"role": "user", "content": "Fix login."},
        {"role": "assistant", "content": "Inspect login."},
    ], llm_title_generated=False, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    monkeypatch.setattr("api.state_sync.sync_session_title", lambda *a, **k: None)
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(TRACE_SENTENCE if len(calls) == 1 else "Valid Recovery Title")

    agent = _fixture_agent(respond) if route == "agent" else None
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session_id=session.session_id, user_text="Fix login.", assistant_text="Inspect login.",
                placeholder_title="Untitled", put_event=lambda name, data: events.append((name, data)),
                agent=agent)
    saved = json.loads((session_dir / (session.session_id + ".json")).read_text(encoding="utf-8"))
    assert saved["title"] == "Valid Recovery Title"
    assert saved["llm_title_generated"] is True
    assert [data["title"] for name, data in events if name == "title"] == ["Valid Recovery Title"]
    assert "Rejected Candidate" not in repr(events)
    assert len(calls) == 2


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_worker_persistent_reasoning_trace_keeps_provisional_title(route, tmp_path, monkeypatch):
    """Worker-level (must-fix 2, end state): when every call answers with the
    reasoning sentence, the session keeps its provisional title — invalid
    model output must not mint a fallback and must not publish any title."""
    import api.models as models
    from api.models import Session
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    session = Session(session_id="trace-worker-keeps", title="Untitled", messages=[
        {"role": "user", "content": "Fix login."},
        {"role": "assistant", "content": "Inspect login."},
    ], llm_title_generated=False, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    monkeypatch.setattr("api.state_sync.sync_session_title", lambda *a, **k: None)
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(TRACE_SENTENCE)

    agent = _fixture_agent(respond) if route == "agent" else None
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session_id=session.session_id, user_text="Fix login.", assistant_text="Inspect login.",
                placeholder_title="Untitled", put_event=lambda name, data: events.append((name, data)),
                agent=agent)
    assert session.title == "Untitled"
    assert session.llm_title_generated is False
    assert [name for name, _data in events if name == "title"] == []
    assert "Rejected Candidate" not in repr(events)
    reasons = [data.get("reason") for name, data in events if name == "title_status"]
    assert any(r in ("llm_invalid_trace_aux", "llm_invalid_trace") for r in reasons if r)
    # route loops: prompt 1 (schema + compatibility resend) and prompt 2
    # (compatibility only); the agent-routed worker additionally retries the
    # aux route end-to-end before declaring invalid output.
    assert len(calls) == (6 if route == "agent" else 3)


@pytest.mark.parametrize("candidate", (
    "Maybe I should call it Login Repair",
    "Maybe the user wants a login title",
    "Perhaps something like Login Repair",
    "Maybe we could go with Login Repair",
    "Maybe, let me think about the login flow",
    "Maybe We should call it Login Repair",
    "Maybe I'll call it Login Repair",
    "Maybe we can call it Login Repair",
    "Maybe the title could be Login Repair",
    "MAYBE I SHOULD CALL IT LOGIN REPAIR",
    "*Maybe*, *I should call it Login Repair*",
    "**Maybe** _I should_ call it Login Repair",
    "_Maybe_ __we__ could go with Login Repair",
))
def test_hedged_reasoning_sentences_are_candidate_rejections(candidate):
    assert streaming._is_bad_new_title(candidate)


@pytest.mark.parametrize("candidate", (
    "Maybe Monad Error Handling", "Maybe Later Feature Flags",
    "Perhaps Later Roadmap Notes", "Maybe Monad vs Either",
    "Maybe We Should Talk Podcast", "Perhaps I Will Review",
    "Maybe I Should Learn Rust vs Go", "Maybe I'll Be There Lyrics",
    "Maybe Use Cases For Monads",
))
def test_bare_hedge_subjects_survive(candidate):
    assert not streaming._is_bad_new_title(candidate)
