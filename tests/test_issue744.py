import pathlib


def test_only_latest_user_message_gets_edit_button():
    src = pathlib.Path("static/ui.js").read_text(encoding="utf-8")
    assert "let lastUserRawIdx=-1;" in src
    assert "const isEditableUser=isUser&&rawIdx===lastUserRawIdx;" in src
    assert "const editBtn  = isEditableUser ?" in src

