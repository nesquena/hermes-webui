from pathlib import Path

ROUTES = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")


def test_regenerate_title_uses_shared_aux_helper_not_hardcoded_kimi():
    idx = ROUTES.find('if parsed.path == "/api/session/regenerate_title":')
    assert idx >= 0
    block = ROUTES[idx:idx + 2200]
    assert '_generate_llm_session_title_via_aux(' in block
    assert 'provider="litellm-chat"' not in block
    assert 'model="fireworks/kimi-k2.6-turbo"' not in block
    assert 'base_url="https://llm.dreamit.au/v1"' not in block
