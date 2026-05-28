from api.streaming import _webui_ephemeral_system_prompt


def test_webui_ephemeral_prompt_includes_browser_surface_context():
    prompt = _webui_ephemeral_system_prompt(
        "Use a concise tone.",
        surface_context={
            "source": "webui",
            "session_id": "session-123",
            "profile": "default",
            "workspace": "/tmp/example-workspace",
        },
    )

    assert "Use a concise tone." in prompt
    assert "WebUI session context" in prompt
    assert "Source: webui" in prompt
    assert "Session ID: session-123" in prompt
    assert "Profile: default" in prompt
    assert "Workspace: /tmp/example-workspace" in prompt
    assert "not the same live transcript as Telegram" in prompt
    assert "Do not copy or dump this browser transcript" in prompt
    assert "explicit captures" in prompt
    assert "durable user preferences" in prompt


def test_webui_ephemeral_prompt_skips_empty_surface_fields():
    prompt = _webui_ephemeral_system_prompt(
        None,
        surface_context={
            "source": "webui",
            "session_id": "",
            "profile": None,
            "workspace": "   ",
        },
    )

    assert "WebUI session context" in prompt
    assert "Source: webui" in prompt
    assert "Session ID:" not in prompt
    assert "Profile:" not in prompt
    assert "Workspace:" not in prompt
