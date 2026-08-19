import copy

from api.models import Session
from api.session_ops import resolve_regeneration_turn, regeneration_revision


def _session():
    return Session(
        session_id="issue6611",
        messages=[
            {
                "role": "user",
                "content": "same prompt",
                "id": "u1",
                "_source": "webui",
                "timestamp": 123,
                "attachments": [
                    {
                        "name": "a.txt",
                        "path": "C:/uploads/a.txt",
                        "mime": "text/plain",
                        "size": 7,
                        "is_image": False,
                        "upload_id": "upload-1",
                    }
                ],
            },
            {"role": "assistant", "content": "failed"},
        ],
        context_messages=[{"role": "user", "content": "same prompt", "id": "u1"}, {"role": "assistant", "content": "failed"}],
    )


def test_regenerate_errored_turn_yields_one_user_row():
    session = _session()
    turn = resolve_regeneration_turn(session)
    assert [row["role"] for row in session.messages[: turn.user_index + 1]] == ["user"]
    assert turn.message["id"] == "u1"


def test_retained_row_preserves_attachments_and_identity():
    session = _session()
    turn = resolve_regeneration_turn(session)
    retained = copy.deepcopy(turn.message)
    assert retained["id"] == "u1"
    assert retained["timestamp"] == 123
    assert retained["attachments"] == [
        {
            "name": "a.txt",
            "path": "C:/uploads/a.txt",
            "mime": "text/plain",
            "size": 7,
            "is_image": False,
            "upload_id": "upload-1",
        }
    ]
    assert regeneration_revision(session)


def test_generic_operations_have_no_regeneration_coordinate():
    session = _session()
    assert "regeneration_revision" not in session.__dict__
    assert session.messages[-1]["content"] == "failed"
