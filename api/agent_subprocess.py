"""
Hermes WebUI -- Agent subprocess worker (focused).

Only runs AIAgent.run_conversation() in a subprocess.
All SSE events push to ``event_queue``; final result is the return value.
"""

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ensure_agent_paths():
    _h = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).resolve()
    _agent = _h / "hermes-agent"
    if str(_agent) not in sys.path:
        sys.path.insert(0, str(_agent))


def run_agent_conversation(
    event_queue,
    cancel_event,
    *,
    session_id: str,
    msg_text: str,
    model: str,
    workspace: str,
    provider_context_json: str,
    attachments_json: str,
    profile_home: str,
    hermes_home: str,
):
    """
    Run AIAgent.run_conversation() in subprocess.
    Push events to ``event_queue``; return final result dict.
    """
    _ensure_agent_paths()
    os.environ["HERMES_HOME"] = hermes_home

    from run_agent import AIAgent
    import inspect

    from api.config import (
        get_config,
        resolve_model_provider,
        model_with_provider_context,
        resolve_custom_provider_connection,
        _resolve_cli_toolsets,
        parse_reasoning_effort,
    )
    from api.streaming import (
        _resolve_custom_provider_runtime_overrides,
        _build_native_multimodal_message,
        _workspace_context_prefix,
        _sanitize_messages_for_api,
        _webui_ephemeral_system_prompt,
    )
    from api.models import Session, get_state_db_session_messages, reconciled_state_db_messages_for_session

    # ── Load session ─────────────────────────────────────────────────────────
    s = Session.load(session_id)
    if s is None:
        event_queue.put(("error", {"message": f"Session {session_id} not found"}))
        return {"error": "Session not found"}

    # ── Resolve credentials ──────────────────────────────────────────────────
    provider_context = json.loads(provider_context_json) if provider_context_json else {}
    resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
        model_with_provider_context(model, provider_context)
    )

    resolved_api_key = None
    try:
        from api.oauth import resolve_runtime_provider_with_anthropic_env_lock
        from hermes_cli.runtime_provider import resolve_runtime_provider
        _rt = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider, requested=resolved_provider
        )
        resolved_api_key = _rt.get("api_key")
        if not resolved_provider:
            resolved_provider = _rt.get("provider")
        if not resolved_base_url:
            resolved_base_url = _rt.get("base_url")
    except Exception:
        pass

    resolved_provider, resolved_api_key, resolved_base_url = _resolve_custom_provider_runtime_overrides(
        resolved_provider, resolved_api_key, resolved_base_url
    )

    # ── Build callbacks ──────────────────────────────────────────────────────
    def on_token(text):
        if text is not None and not cancel_event.is_set():
            event_queue.put(("token", {"text": str(text)}))

    def on_reasoning(text):
        if text is not None and not cancel_event.is_set():
            event_queue.put(("reasoning", {"text": str(text)}))

    def on_tool(*cb_args, **cb_kwargs):
        if cancel_event.is_set():
            return
        event_type = None
        name = None
        preview = None
        args = None
        if len(cb_args) >= 4:
            event_type, name, preview, args = cb_args[:4]
        elif len(cb_args) == 3:
            name, preview, args = cb_args
            event_type = "tool.started"
        elif len(cb_args) == 2:
            event_type, name = cb_args
        elif len(cb_args) == 1:
            name = cb_args[0]
            event_type = "tool.started"

        if event_type in ("reasoning.available", "_thinking"):
            reason_text = preview if event_type == "reasoning.available" else name
            if reason_text:
                event_queue.put(("reasoning", {"text": str(reason_text)}))
            return

        args_snap = {}
        if isinstance(args, dict):
            for k, v in list(args.items())[:4]:
                s2 = str(v)
                args_snap[k] = s2[:120] + ("..." if len(s2) > 120 else "")

        if event_type in (None, "tool.started"):
            event_queue.put(("tool", {
                "event_type": event_type or "tool.started",
                "name": name,
                "preview": preview,
                "args": args_snap,
            }))
            return

        if event_type == "tool.completed":
            event_queue.put(("tool_complete", {
                "event_type": event_type,
                "name": name,
                "preview": preview,
                "args": args_snap,
                "duration": cb_kwargs.get("duration"),
                "is_error": bool(cb_kwargs.get("is_error", False)),
            }))

    def on_tool_start(tool_call_id, name, args):
        if cancel_event.is_set():
            return
        args_snap = {}
        if isinstance(args, dict):
            for k, v in list(args.items())[:4]:
                s2 = str(v)
                args_snap[k] = s2[:120] + ("..." if len(s2) > 120 else "")
        event_queue.put(("tool", {
            "event_type": "tool.started",
            "name": name,
            "preview": None,
            "args": args_snap,
            "tid": tool_call_id,
        }))

    def on_tool_complete(tool_call_id, name, args, function_result):
        if cancel_event.is_set():
            return
        _snippet = ""
        if isinstance(function_result, dict):
            _snippet = str(function_result.get("output", "") or function_result.get("result", ""))[:200]
        elif isinstance(function_result, str):
            _snippet = function_result[:200]
        else:
            _snippet = str(function_result)[:200]
        event_queue.put(("tool_complete", {
            "event_type": "tool.completed",
            "name": name,
            "preview": _snippet,
            "args": {},
            "tid": tool_call_id,
            "is_error": False,
        }))

    def on_status(kind, message):
        event_queue.put(("status", {"kind": kind, "message": message}))

    # ── Build agent kwargs ────────────────────────────────────────────────────
    _agent_params = set(inspect.signature(AIAgent.__init__).parameters)
    _cfg = get_config()

    _agent_kwargs = dict(
        model=resolved_model,
        provider=resolved_provider,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        platform="webui",
        quiet_mode=True,
        session_id=session_id,
        stream_delta_callback=on_token,
        reasoning_callback=on_reasoning,
        tool_progress_callback=on_tool,
        status_callback=on_status,
    )

    if "interim_assistant_callback" in _agent_params:
        def on_interim(text, **kw):
            if text is not None:
                visible = str(text).strip()
                if visible:
                    event_queue.put(("interim_assistant", {
                        "text": visible,
                        "already_streamed": bool(kw.get("already_streamed", False)),
                    }))
        _agent_kwargs["interim_assistant_callback"] = on_interim
    if "tool_start_callback" in _agent_params:
        _agent_kwargs["tool_start_callback"] = on_tool_start
    if "tool_complete_callback" in _agent_params:
        _agent_kwargs["tool_complete_callback"] = on_tool_complete

    # max_iterations
    try:
        _raw = (_cfg.get("agent", {}) if isinstance(_cfg, dict) else {}).get("max_turns")
        if _raw is None:
            _raw = _cfg.get("max_turns")
        if _raw is not None:
            _parsed = int(_raw)
            if _parsed > 0:
                _agent_kwargs["max_iterations"] = _parsed
    except Exception:
        pass

    # max_tokens
    try:
        _raw = _cfg.get("max_tokens")
        if _raw is None:
            _raw = (_cfg.get("agent", {}) if isinstance(_cfg, dict) else {}).get("max_tokens")
        if _raw is not None:
            _parsed = int(_raw)
            if _parsed > 0:
                _agent_kwargs["max_tokens"] = _parsed
    except Exception:
        pass

    # reasoning_config
    try:
        _effort_raw = (_cfg.get("agent", {}) if isinstance(_cfg, dict) else {}).get("reasoning_effort")
        _rc = parse_reasoning_effort(_effort_raw)
        if "reasoning_config" in _agent_params and _rc is not None:
            _agent_kwargs["reasoning_config"] = _rc
    except Exception:
        pass

    # fallback_model
    _fallback = _cfg.get("fallback_model") or _cfg.get("fallback_providers") or None
    if _fallback:
        _fb_entry = None
        if isinstance(_fallback, list):
            for _e in _fallback:
                if isinstance(_e, dict) and _e.get("model"):
                    _fb_entry = _e
                    break
        elif isinstance(_fallback, dict) and _fallback.get("model"):
            _fb_entry = _fallback
        if _fb_entry:
            _agent_kwargs["fallback_model"] = {
                "model": _fb_entry.get("model", ""),
                "provider": _fb_entry.get("provider", ""),
                "base_url": _fb_entry.get("base_url"),
                "api_key": _fb_entry.get("api_key"),
                "key_env": _fb_entry.get("key_env"),
            }

    # toolsets
    _toolsets = _resolve_cli_toolsets(_cfg)
    try:
        _meta = Session.load_metadata_only(session_id)
        _override = getattr(_meta, "enabled_toolsets", None) if _meta else None
        if _override:
            _toolsets = _override
    except Exception:
        pass
    _agent_kwargs["enabled_toolsets"] = _toolsets

    # SessionDB
    try:
        from hermes_state import SessionDB
        _agent_kwargs["session_db"] = SessionDB()
    except Exception:
        pass

    # gateway_session_key
    if "gateway_session_key" in _agent_params:
        _agent_kwargs["gateway_session_key"] = session_id

    # api_mode / acp_command / acp_args / credential_pool
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        _rt2 = resolve_runtime_provider()
        if "api_mode" in _agent_params:
            _agent_kwargs["api_mode"] = _rt2.get("api_mode")
        if "acp_command" in _agent_params:
            _agent_kwargs["acp_command"] = _rt2.get("command")
        if "acp_args" in _agent_params:
            _agent_kwargs["acp_args"] = _rt2.get("args")
        if "credential_pool" in _agent_params:
            _agent_kwargs["credential_pool"] = _rt2.get("credential_pool")
    except Exception:
        pass

    # Create agent (fresh each turn — no cross-process cache)
    agent = AIAgent(**_agent_kwargs)

    # ── Workspace context ─────────────────────────────────────────────────────
    workspace_ctx = _workspace_context_prefix(workspace)
    workspace_system_msg = (
        f"Active workspace at session start: {workspace}\n"
        "Every user message is prefixed with [Workspace::v1: /absolute/path] indicating the "
        "workspace the user has selected in the web UI at the time they sent that message. "
        "This tag is the single authoritative source of the active workspace and updates "
        "with every message. It overrides any prior workspace mentioned in this system "
        "prompt, memory, or conversation history. Always use the value from the most recent "
        "[Workspace::v1: ...] tag as your default working directory for ALL file operations: "
        "write_file, read_file, search_files, terminal workdir, and patch. "
        "Never fall back to a hardcoded path when this tag is present."
    )

    # Personality prompt
    _personality_prompt = None
    _pname = getattr(s, "personality", None)
    if _pname:
        _agent_cfg = _cfg.get("agent", {})
        _personalities = _agent_cfg.get("personalities", {})
        if isinstance(_personalities, dict) and _pname in _personalities:
            _pval = _personalities[_pname]
            if isinstance(_pval, dict):
                _parts = [_pval.get("system_prompt", "") or _pval.get("prompt", "")]
                if _pval.get("tone"):
                    _parts.append(f'Tone: {_pval["tone"]}')
                if _pval.get("style"):
                    _parts.append(f'Style: {_pval["style"]}')
                _personality_prompt = "\n".join(p for p in _parts if p)
            else:
                _personality_prompt = str(_pval)

    agent.ephemeral_system_prompt = _webui_ephemeral_system_prompt(_personality_prompt)

    # ── Conversation history ────────────────────────────────────────────────
    _external_state_messages = get_state_db_session_messages(session_id)
    _previous_context_messages = _sanitize_messages_for_api(
        reconciled_state_db_messages_for_session(
            s, state_messages=_external_state_messages, prefer_context=True,
        ),
        cfg=_cfg,
    )

    # ── Pre-stream save ───────────────────────────────────────────────────────
    s.save(touch_updated_at=True, skip_index=False)

    # ── Build user message ────────────────────────────────────────────────────
    attachments = json.loads(attachments_json) if attachments_json else []
    user_message = _build_native_multimodal_message(workspace_ctx, msg_text, attachments, workspace, cfg=_cfg)

    # ── Run conversation ─────────────────────────────────────────────────────
    result = agent.run_conversation(
        user_message=user_message,
        system_message=workspace_system_msg,
        conversation_history=_previous_context_messages,
        task_id=session_id,
        persist_user_message=msg_text,
    )

    # ── Cancel check ─────────────────────────────────────────────────────────
    if cancel_event.is_set():
        event_queue.put(("cancel", {"message": "Cancelled by user"}))
        return {"cancelled": True}

    # ── Build response ──────────────────────────────────────────────────────
    _result_messages = result.get("messages") or []
    input_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
    output_tokens = getattr(agent, "session_completion_tokens", 0) or 0
    estimated_cost = getattr(agent, "session_estimated_cost_usd", None)
    cache_read_tokens = getattr(agent, "session_cache_read_tokens", 0) or 0
    cache_write_tokens = getattr(agent, "session_cache_write_tokens", 0) or 0

    _compressed = False
    _compressor = getattr(agent, "context_compressor", None)
    if _compressor and getattr(_compressor, "compression_count", 0) > 0:
        _compressed = True

    response = {
        "messages": _result_messages,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": estimated_cost,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "compressed": _compressed,
        "session_id": getattr(s, "session_id", session_id),
    }

    if _compressor:
        response["context_length"] = getattr(_compressor, "context_length", 0) or 0
        response["threshold_tokens"] = getattr(_compressor, "threshold_tokens", 0) or 0
        response["last_prompt_tokens"] = getattr(_compressor, "last_prompt_tokens", 0) or 0

    event_queue.put(("done", response))
    return response
