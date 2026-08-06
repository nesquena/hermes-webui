"""Test vision base64 persistence: callback→journal, compact, replay."""

import copy
import json

from api.streaming import (
    _compact_image_parts_for_persistence,
    _strip_base64_data_urls,
    _is_inline_base64_image_leaf,
    _project_image_parts,
    _project_live_tool_args,
    _project_tool_call_rows,
    _build_partial_message,
    _tool_result_snippet,
)


def test_callback_journal_no_base64():
    """_tool_result_snippet com _multimodal → sem base64 no resultado."""
    raw = {"type": "function_result", "content": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="}
    result = _tool_result_snippet(raw)
    # Base64 data is replaced with [base64 image] placeholder — no raw payload
    assert 'base64,' not in result
    assert '[base64 image]' in result


def test_non_base64_images_preserved():
    """http/file references sobrevivem à compactação."""
    msg = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.com/img.png'}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 0
    assert copied[0]['content'][0]['image_url']['url'] == 'https://example.com/img.png'


def test_base64_inline_replaced():
    """data:image base64 compactado."""
    msg = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    assert copied[0]['content'][0]['type'] == 'text'


def test_anthropic_source_base64():
    """Anthropic source: {type: 'base64'} compactado."""
    msg = [{'role': 'tool', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1


def test_in_place_not_mutated():
    """Objeto original não é alterado."""
    original = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}}]}]
    frozen = copy.deepcopy(original)
    _compact_image_parts_for_persistence(original)
    assert original == frozen


def test_mixed_images():
    """Trecho com imagens base64 E http preserva http, compacta base64."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
        {'type': 'image_url', 'image_url': {'url': 'https://example.com/ok.png'}},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 1
    assert copied[0]['content'][0]['type'] == 'text'
    assert copied[0]['content'][1]['image_url']['url'] == 'https://example.com/ok.png'


def test_nested_mixed_wrapper_preserves_siblings():
    """_multimodal wrapper com base64 E texto preserva ambos, compacta apenas base64."""
    msg = [{'role': 'tool', 'content': [
        {
            'type': 'multimodal',
            'content': [
                {'type': 'text', 'text': 'Descrição:'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/normal.png'}},
            ],
        },
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    wrapper = copied[0]['content'][0]
    assert wrapper['type'] == 'multimodal'
    assert wrapper['content'][0] == {'type': 'text', 'text': 'Descrição:'}
    assert wrapper['content'][1] == {'type': 'text', 'text': '[screenshot]'}
    assert wrapper['content'][2]['image_url']['url'] == 'https://example.com/normal.png'


def test_direct_string_image_url_compactado():
    """Direct-string image_url (não dict) com data URL é compactado."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': 'data:image/png;base64,iVBORw0KGgo='},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    assert copied[0]['content'][0]['type'] == 'text'


def test_anthropic_source_base64_non_image_preserved():
    """Anthropic base64 source com media_type não-imagem é preservado."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'text/plain', 'data': 'plaintextdata'}},
    ]}]
    # media_type não contém 'image' → não é leaf de imagem → preservado byte a byte
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 0
    assert copied == msg


def test_project_live_tool_args_strips_base64():
    """_project_live_tool_args remove data URLs de args aninhados."""
    args = {
        'path': '/tmp/file.png',
        'image_data': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ=',
        'metadata': {'url': 'data:image/jpeg;base64,/9j/4AAQ'},
        'file_list': ['data:image/gif;base64,R0lGODlh', '/real/path.png'],
    }
    projected = _project_live_tool_args(args)
    # Original não é mutado
    assert args['image_data'].startswith('data:image')
    # Projetado: base64 substituído
    assert '[base64 image]' in projected['image_data']
    assert '[base64 image]' in projected['metadata']['url']
    assert '[base64 image]' in projected['file_list'][0]
    # Não-base64 preservado
    assert projected['path'] == '/tmp/file.png'
    assert projected['file_list'][1] == '/real/path.png'


def test_project_live_tool_args_preserves_non_base64():
    """_project_live_tool_args preserva strings sem data URL."""
    args = {'url': 'https://example.com/image.png', 'text': 'hello'}
    projected = _project_live_tool_args(args)
    assert projected == args


def test_idempotent_projection():
    """Segunda projeção não muda nada."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
        {'type': 'image_url', 'image_url': {'url': 'https://example.com/ok.png'}},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 1
    second_copy, changed2 = _compact_image_parts_for_persistence(copied)
    assert changed2 == 0


def test_projection_recurses_generic_wrappers():
    """Wrappers genéricos (dict-valued content, chave arbitrária) são projetados."""
    msg = [{'role': 'tool', 'content': [
        {
            'type': 'provider_envelope',
            'content': {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
            'meta': {'keep': 'me'},
        },
        {
            'type': 'provider_envelope',
            'nested': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBOR'}},
                {'type': 'text', 'text': 'keep text'},
            ],
        },
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 2
    env1 = copied[0]['content'][0]
    assert env1['type'] == 'provider_envelope'
    assert env1['meta'] == {'keep': 'me'}
    assert env1['content'] == {'type': 'text', 'text': '[screenshot]'}
    env2 = copied[0]['content'][1]
    assert env2['type'] == 'provider_envelope'
    assert env2['nested'][0] == {'type': 'text', 'text': '[screenshot]'}
    assert env2['nested'][1] == {'type': 'text', 'text': 'keep text'}


def test_projection_preserves_order_and_unknown_values():
    """Ordem das chaves, valores desconhecidos e referências HTTP sobrevivem."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
        {'type': 'image_url', 'image_url': {'url': 'file:///tmp/a.png'}},
        {'type': 'image_url', 'image_url': {'url': 'artifact://handle/x'}},
        # Sem type de imagem → NÃO é leaf de imagem; valor desconhecido preservado.
        {'type': 'weird', 'blob': {'nested': {'x': 'data:image/gif;base64,R0lGOD'}}},
        'scalar-part',
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 1  # apenas o leaf image_url data:image é projetado
    parts = copied[0]['content']
    assert parts[0] == {'type': 'text', 'text': '[screenshot]'}
    assert parts[1]['image_url']['url'] == 'file:///tmp/a.png'
    assert parts[2]['image_url']['url'] == 'artifact://handle/x'
    assert parts[3]['type'] == 'weird'
    # Sem prova de imagem: o valor base64 aninhado é preservado byte a byte.
    assert parts[3]['blob']['nested'] == {'x': 'data:image/gif;base64,R0lGOD'}
    assert parts[4] == 'scalar-part'


# ── Callback lifecycle: callback → live mirrors → journal-before-queue ────────

def _run_streaming_with_agent(agent_class, stream_id):
    """Drive _run_agent_streaming with a fake agent; return (events, journal).

    Mirrors the proven harness from test_issue_progress_echo_dedupe.py: fake
    session, fake hermes_cli/hermes_state modules, mocked resolvers, and a
    recording RunJournalWriter so journal-before-queue payloads are observable.
    """
    import queue
    import sys
    import types
    from typing import cast
    from unittest import mock

    import api.streaming as streaming

    class FakeSession:
        def __init__(self):
            self.session_id = stream_id.replace("stream_", "sess_")
            self.title = "Vision lifecycle"
            self.workspace = "/tmp"
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.cache_read_tokens = 0
            self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = stream_id
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {
                "session_id": self.session_id,
                "title": self.title,
                "workspace": self.workspace,
                "model": self.model,
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "personality": self.personality,
            }

    class RecordingRunJournal:
        def __init__(self, *args, **kwargs):
            self.events = []

        def append_sse_event(self, event_name, payload=None):
            seq = len(self.events) + 1
            self.events.append({
                "seq": seq,
                "event": event_name,
                "payload": payload or {},
                "event_id": f"e{seq}",
            })
            return {"event_id": f"e{seq}"}

    fake_session = FakeSession()
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_payload = {
        "provider": "openai",
        "base_url": None,
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    runtime_payload["api_" + "key"] = "***"
    fake_runtime_module.__dict__["resolve_runtime_provider"] = mock.Mock(return_value=runtime_payload)
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.__dict__["SessionDB"] = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    saved = {k: sys.modules.get(k) for k in injected}
    sys.modules.update(injected)
    journal = RecordingRunJournal()
    try:
        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=agent_class), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch.object(streaming, "RunJournalWriter", return_value=journal):
            streaming.STREAMS[stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="scan",
                model="gpt-test",
                workspace="/tmp",
                stream_id=stream_id,
            )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        for k, prev in saved.items():
            if prev is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
    events = list(fake_queue.queue)
    return events, journal, fake_session


class _LegacyVisionAgent:
    """Legacy callback API: only tool_progress_callback (no structured params)."""

    def __init__(
        self,
        model=None,
        provider=None,
        base_url=None,
        platform=None,
        quiet_mode=False,
        enabled_toolsets=None,
        fallback_model=None,
        session_id=None,
        session_db=None,
        prefill_messages=None,
        stream_delta_callback=None,
        reasoning_callback=None,
        tool_progress_callback=None,
        clarify_callback=None,
        interim_assistant_callback=None,
        **_kwargs,
    ):
        self.stream_delta_callback = stream_delta_callback
        self.reasoning_callback = reasoning_callback
        self.tool_progress_callback = tool_progress_callback
        self.interim_assistant_callback = interim_assistant_callback
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="
        args = {"image": b64, "path": "/tmp/real.png", "meta": {"url": b64}}
        self.tool_progress_callback("tool.started", "read_file", b64, args)
        self.tool_progress_callback("tool.completed", "read_file", b64, args)
        history = kwargs.get("conversation_history", [])
        return {"messages": history + [
            {"role": "user", "content": kwargs.get("persist_user_message", "scan")},
            {"role": "assistant", "content": "done"},
        ]}

    def interrupt(self, _message):
        pass


class _StructuredVisionAgent(_LegacyVisionAgent):
    """Structured callback API: declares tool_start/tool_complete params."""

    def __init__(self, *args, tool_start_callback=None, tool_complete_callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_start_callback = tool_start_callback
        self.tool_complete_callback = tool_complete_callback

    def run_conversation(self, **kwargs):
        b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="
        args = {"image": b64, "path": "/tmp/real.png"}
        result = {"type": "function_result", "content": b64}
        self.tool_start_callback("call_1", "read_file", args)
        self.tool_complete_callback("call_1", "read_file", args, result)
        history = kwargs.get("conversation_history", [])
        return {"messages": history + [
            {"role": "user", "content": kwargs.get("persist_user_message", "scan")},
            {"role": "assistant", "content": "done"},
        ]}


def _assert_no_base64_in_tool_events(events, journal):
    """Fail-first: journal-before-queue and queue tool payloads must be clean."""
    for event, payload in events:
        if event not in ("tool", "tool_complete"):
            continue
        assert "base64," not in json.dumps(payload, default=str), event
        assert "data:image" not in json.dumps(payload, default=str), event
    for entry in journal.events:
        if entry["event"] not in ("tool", "tool_complete"):
            continue
        assert "base64," not in json.dumps(entry["payload"], default=str), entry["event"]
        assert "data:image" not in json.dumps(entry["payload"], default=str), entry["event"]


def test_legacy_callback_lifecycle_no_base64():
    """Legacy callback → live mirrors → journal-before-queue sem base64."""
    events, journal, fake_session = _run_streaming_with_agent(
        _LegacyVisionAgent, "stream_vision_legacy_lifecycle"
    )
    tool_events = [payload for event, payload in events if event == "tool"]
    complete_events = [payload for event, payload in events if event == "tool_complete"]
    assert tool_events, "legacy tool.started must emit a tool event"
    assert complete_events, "legacy tool.completed must emit a tool_complete event"
    _assert_no_base64_in_tool_events(events, journal)


def test_structured_callback_lifecycle_no_base64():
    """Structured callback → live mirrors → journal-before-queue sem base64."""
    events, journal, fake_session = _run_streaming_with_agent(
        _StructuredVisionAgent, "stream_vision_structured_lifecycle"
    )
    tool_events = [payload for event, payload in events if event == "tool"]
    complete_events = [payload for event, payload in events if event == "tool_complete"]
    assert tool_events, "structured tool_start_callback must emit a tool event"
    assert complete_events, "structured tool_complete_callback must emit a tool_complete event"
    _assert_no_base64_in_tool_events(events, journal)


def test_cancel_partial_save_reload_no_base64():
    """_build_partial_message + save/reload JSON não reintroduz base64."""
    b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="
    rows = [
        {"name": "read_file", "args": {"image": b64, "path": "/tmp/real.png"}, "done": False},
        {"name": "fetch", "args": {"url": "https://example.com/x.png"}, "done": True, "snippet": b64},
    ]
    msg = _build_partial_message("", "", rows)
    assert msg is not None
    assert msg["_partial_tool_calls"]
    for row in msg["_partial_tool_calls"]:
        assert "base64," not in json.dumps(row, default=str)
        assert "data:image" not in json.dumps(row, default=str)
    # Durable save/reload round-trip (sidecar JSON).
    reloaded = json.loads(json.dumps(msg))
    for row in reloaded["_partial_tool_calls"]:
        assert "base64," not in json.dumps(row, default=str)
        assert "data:image" not in json.dumps(row, default=str)
    # Non-base64 references survive projection.
    assert reloaded["_partial_tool_calls"][0]["args"]["path"] == "/tmp/real.png"
    assert reloaded["_partial_tool_calls"][1]["args"]["url"] == "https://example.com/x.png"


def test_shared_alias_fixture_not_mutated():
    """Mesmo dict de parte compartilhado entre messages e context_messages."""
    part = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}}
    shared_row = {'role': 'tool', 'content': [part]}
    messages = [shared_row]
    context_messages = [shared_row]  # MESMO objeto aliased nas duas histórias
    frozen = copy.deepcopy(messages)

    copied, changed = _compact_image_parts_for_persistence(messages)
    assert changed == 1
    # O objeto compartilhado original não foi mutado (copy-on-write).
    assert messages == frozen
    assert context_messages[0] is shared_row
    assert shared_row['content'][0] is part
    assert part['image_url']['url'].startswith('data:image')
    # A cópia compactada é independente e já projetada.
    assert copied[0] is not shared_row
    assert copied[0]['content'][0] == {'type': 'text', 'text': '[screenshot]'}


def test_replay_run_journal_projects_pre_fix_payloads():
    """Replay projeta payloads tool/tool_complete de journals pré-fix."""
    from api.routes import _project_replay_tool_payload

    b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="
    payload = {
        "event_type": "tool.started",
        "name": "read_file",
        "preview": b64,
        "args": {"image": b64, "path": "/tmp/real.png"},
    }
    projected = _project_replay_tool_payload("tool", payload)
    assert projected["preview"] == "[base64 image]"
    assert projected["args"]["image"] == "[base64 image]"
    # Referências não-base64 preservadas.
    assert projected["args"]["path"] == "/tmp/real.png"
    # Eventos não-tool passam intactos.
    token = {"text": "hello"}
    assert _project_replay_tool_payload("token", token) is token
    # Payload não-dict passa intacto.
    assert _project_replay_tool_payload("tool", None) is None


def test_strip_base64_data_urls_valid_subtypes():
    """Reconhecedor cobre subtypes com +, -, . e case-variants; referências seguras intactas."""
    # Subtypes com pontuação que o regex antigo [a-zA-Z]+ não casava.
    assert _strip_base64_data_urls("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=") == "[base64 image]"
    assert _strip_base64_data_urls("data:image/x-icon;base64,AAABAAEAEBAAAAEAIABoBAAAFgAAACg=") == "[base64 image]"
    assert _strip_base64_data_urls("data:image/vnd.microsoft.icon;base64,AAABAAEAEBAAAAEAIABoBAAAFgAAACg=") == "[base64 image]"
    assert _strip_base64_data_urls("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ=") == "[base64 image]"
    # Case-variants (scheme/media type/base64 marker).
    assert _strip_base64_data_urls("DATA:IMAGE/SVG+XML;BASE64,PHN2Zz48L3N2Zz4=") == "[base64 image]"
    assert _strip_base64_data_urls("Data:Image/PNG;Base64,iVBORw0KGgo=") == "[base64 image]"
    # Referências seguras e texto comum permanecem byte-for-byte.
    text = "open https://example.com/img.png and file:///tmp/a.png artifact://x hello world"
    assert _strip_base64_data_urls(text) == text
    assert _strip_base64_data_urls("data:text/plain;base64,aGVsbG8=") == "data:text/plain;base64,aGVsbG8="
    assert _strip_base64_data_urls("data:image/svg+xml,<svg/>") == "data:image/svg+xml,<svg/>"
    assert _strip_base64_data_urls("data:image/png;charset=utf-8,iVBOR") == "data:image/png;charset=utf-8,iVBOR"


def test_replay_projector_shape_complete_and_non_mutating():
    """Projector lida com args dict/list/tuple/str e preview+snippet; entry armazenado não é mutado."""
    from api.routes import _project_replay_tool_payload

    b64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="
    b64_svg = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
    b64_icon = "data:image/x-icon;base64,AAABAAEAEBAAAAEAIABoBAAAFgAAACg="
    payload = {
        "event_type": "tool.started",
        "name": "read_file",
        "preview": b64_svg,
        "snippet": b64_png,
        "args": {
            "path": "/tmp/real.png",
            "nested": {"image": b64_png},
            "list": [b64_svg, "https://example.com/ok.png"],
            "pair": (b64_icon, "plain"),
        },
    }
    frozen = copy.deepcopy(payload)
    projected = _project_replay_tool_payload("tool", payload)
    assert projected is not payload
    assert projected["preview"] == "[base64 image]"
    assert projected["snippet"] == "[base64 image]"
    assert projected["args"]["nested"]["image"] == "[base64 image]"
    assert projected["args"]["list"][0] == "[base64 image]"
    assert projected["args"]["list"][1] == "https://example.com/ok.png"
    assert projected["args"]["pair"][0] == "[base64 image]"
    assert projected["args"]["pair"][1] == "plain"
    assert projected["args"]["path"] == "/tmp/real.png"
    # A entrada armazenada não foi mutada (copy-on-write).
    assert payload == frozen
    # Args como lista ou string JSON também são projetados shape-complete.
    list_projected = _project_replay_tool_payload("tool_complete", {"args": [b64_png, {"deep": b64_svg}]})
    assert list_projected["args"][0] == "[base64 image]"
    assert list_projected["args"][1]["deep"] == "[base64 image]"
    str_projected = _project_replay_tool_payload("tool", {"args": '{"image": "%s"}' % b64_png})
    assert str_projected["args"] == '{"image": "[base64 image]"}'
