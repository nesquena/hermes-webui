import pathlib


def test_every_user_message_gets_edit_button():
    src = pathlib.Path("static/ui.js").read_text(encoding="utf-8")
    # #6737: the edit affordance now renders on EVERY user message (not just the
    # latest one), so the last-only lookup loop is gone.
    assert "let lastUserRawIdx=-1;" not in src
    assert "const isEditableUser=isUser;" in src
    assert "const editBtn  = isEditableUser ?" in src
