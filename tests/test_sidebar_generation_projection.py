"""The sessions route must carry shrink metadata through to unread decisions."""
import json
import shutil

import pytest

from tests.test_issue4766_sidebar_source_pushdown import (
    _handle_sessions,
    _install_common_monkeypatches,
)
from tests.test_issue856_background_completion_unread import _run_unread_behavior


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("retained", [0, 2])
def test_sessions_route_preserves_post_shrink_unread(monkeypatch, retained):
    import api.routes as routes
    from api.models import Session
    from api.session_ops import truncate_session_at_keep

    session = Session(session_id="X", messages=[
        {"role": "user", "content": str(i)} for i in range(10)
    ])
    truncate_session_at_keep(session, retained)
    session.messages.append({"role": "assistant", "content": "new response"})
    row = session.compact()
    _install_common_monkeypatches(monkeypatch, [row])
    routes._session_list_cache_clear()
    try:
        # Exercise both the initial projection and the cached response.
        for _ in range(2):
            response = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")
            assert response.status == 200
            [projected] = response.json_body()["sessions"]
            result = _run_unread_behavior("""
store[SESSION_VIEWED_COUNTS_KEY] = JSON.stringify({
  X: {message_count: 10, transcript_generation: 0},
});
const unread = _hasUnreadForSession(""" + json.dumps(projected) + """);
console.log(JSON.stringify({unread, viewed: JSON.parse(store[SESSION_VIEWED_COUNTS_KEY]).X}));
""")
            assert result == {
                "unread": True,
                "viewed": {"message_count": retained, "transcript_generation": 1},
            }
            assert "messages" not in projected
    finally:
        routes._session_list_cache_clear()
