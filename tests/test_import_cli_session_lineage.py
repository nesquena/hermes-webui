import json


def test_import_cli_session_preserves_parent_session_id():
    from api.models import import_cli_session, SESSION_DIR, Session

    parent_id = "parent_lineage_001"
    child_id = "child_lineage_001"

    # Ensure clean fixture state for direct model-level import.
    for sid in (parent_id, child_id):
        try:
            (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
        except Exception:
            pass

    session = import_cli_session(
        child_id,
        "Child Session",
        [{"role": "user", "content": "hello", "timestamp": 1.0}],
        model="test-model",
        parent_session_id=parent_id,
        created_at=1.0,
        updated_at=2.0,
    )

    assert session.parent_session_id == parent_id

    payload = json.loads((SESSION_DIR / f"{child_id}.json").read_text(encoding="utf-8"))
    assert payload["parent_session_id"] == parent_id

    loaded = Session.load(child_id)
    assert loaded.parent_session_id == parent_id
    assert loaded.compact()["parent_session_id"] == parent_id
