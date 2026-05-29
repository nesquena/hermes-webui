from api.streaming import _webui_surface_context_prompt
from api.tool_availability import build_tool_availability_context, tool_availability_prompt_lines


def test_browser_toolset_enabled_but_filtered_reports_runtime_config_issue():
    payload = build_tool_availability_context(
        enabled_toolsets=["browser", "terminal"],
        agent_tools=[{"function": {"name": "terminal"}}],
        cfg={"browser": {"cloud_provider": "browser-use"}},
    )

    browser = payload["browser"]
    assert browser["toolset_enabled"] is True
    assert browser["available"] is False
    assert browser["reason"] == "browser_requirements_unmet"
    assert browser["provider"] == "browser-use"
    assert "BROWSER_USE_API_KEY" in browser["hint"]
    assert "permanent WebUI/API limitation" in "\n".join(tool_availability_prompt_lines(payload))


def test_browser_tools_available_reports_available_tools():
    payload = build_tool_availability_context(
        enabled_toolsets=["browser"],
        agent_tools=[
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "browser_snapshot"}},
        ],
        cfg={"browser": {"cloud_provider": "local"}},
    )

    browser = payload["browser"]
    assert browser["available"] is True
    assert browser["reason"] == "available"
    assert browser["tools"] == ["browser_navigate", "browser_snapshot"]


def test_webui_surface_context_includes_tool_availability_note():
    prompt = _webui_surface_context_prompt(
        {
            "source": "webui",
            "session_id": "session-123",
            "workspace": "/workspace",
            "tool_availability": build_tool_availability_context(
                enabled_toolsets=["browser"],
                agent_tools=[],
                cfg={"browser": {"cloud_provider": "browser-use"}},
            ),
        }
    )

    assert "Source: webui" in prompt
    assert "Session ID: session-123" in prompt
    assert "Tool availability diagnostics:" in prompt
    assert "Browser toolset enabled: yes" in prompt
    assert "Browser tools available: no" in prompt
    assert "enabled-but-unavailable runtime configuration" in prompt
