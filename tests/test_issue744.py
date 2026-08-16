import pathlib


def test_every_writable_user_message_gets_edit_button():
    src = pathlib.Path("static/ui.js").read_text(encoding="utf-8")
    assert "let lastUserRawIdx=-1;" in src
    assert "const isEditableUser=isUser&&!(readOnlySession&&!branchableReadOnlySession);" in src
    assert src.count("const readOnlySession=typeof _isReadOnlySession==='function'") == 1
    assert src.count("const branchableReadOnlySession=typeof _isBranchableReadOnlySession==='function'") == 2
    assert "const editBtn  = isEditableUser ?" in src
    assert "const forkBtn  = (readOnlySession&&!branchableReadOnlySession)" in src

