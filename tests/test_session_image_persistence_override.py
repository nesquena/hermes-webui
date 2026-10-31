import json

from api.models import Session
from api.session_image_persistence_override import (
    PERSISTENCE_OVERRIDE_VERSION,
    compact_persisted_image_parts,
)


def test_override_compacts_user_and_tool_image_parts_for_persistence():
    huge = "data:image/jpeg;base64," + ("A" * 1_000_000)
    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": huge}}]},
        {"role": "tool", "content": [{"type": "image_url", "image_url": {"url": huge}}]},
    ]

    changed = compact_persisted_image_parts(messages)

    assert changed == 2
    assert "data:image" not in json.dumps(messages)
    assert all(m["content"] == [{"type": "text", "text": "[image attachment]"}] for m in messages)


def test_override_preserves_non_image_parts_and_is_idempotent():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "keep"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "document", "id": "doc-1"},
    ]}]

    assert compact_persisted_image_parts(messages) == 1
    assert messages[0]["content"] == [
        {"type": "text", "text": "keep"},
        {"type": "text", "text": "[image attachment]"},
        {"type": "document", "id": "doc-1"},
    ]
    assert compact_persisted_image_parts(messages) == 0


def test_session_save_writes_compact_copy_without_mutating_model_payload(tmp_path, monkeypatch):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    huge = "data:image/jpeg;base64," + ("A" * 1_000_000)
    source = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": huge}}]}]
    session = Session(session_id="save-boundary", messages=source)

    session.save(touch_updated_at=False)

    persisted = json.loads((tmp_path / "save-boundary.json").read_text())
    assert PERSISTENCE_OVERRIDE_VERSION == "1.0.0"
    assert "data:image" not in json.dumps(persisted["messages"])
    assert source[0]["content"][0]["image_url"]["url"] == huge
    assert session.messages == source
