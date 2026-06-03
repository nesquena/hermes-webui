from collections import OrderedDict


def test_session_todos_persist_and_compact(tmp_path, monkeypatch):
    import api.models as models
    from api.models import Session

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    session = Session(
        session_id="todotest1234",
        title="Todo session",
        todos=[
            {"id": "a", "content": "first item", "status": "in_progress"},
            {"id": "", "content": "second item", "status": "weird-status"},
            {"id": "skip", "content": "", "status": "pending"},
        ],
    )
    session.save()

    loaded = Session.load("todotest1234")
    assert loaded is not None
    assert loaded.todos == [
        {"id": "a", "content": "first item", "status": "in_progress"},
        {"id": "todo-2", "content": "second item", "status": "pending"},
    ]

    compact = loaded.compact()
    assert compact["todos"] == loaded.todos
