"""Sprint 2 tests: image preview, file types, markdown. Uses cleanup_test_sessions fixture."""
import io, json, uuid, urllib.request, urllib.error, pathlib

from tests._pytest_port import BASE

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read(), r.headers.get('Content-Type', ''), r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def make_session_tracked(created_list, ws=None):
    """Create a session and register it with the cleanup fixture."""
    import pathlib as _pathlib
    body = {}
    if ws: body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, _pathlib.Path(d["session"]["workspace"])



def test_raw_endpoint_serves_png(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    png = (b"\x89PNG\r\n\x1a\n" b"\x00\x00\x00\rIHDR\x00\x00\x00\x01"
           b"\x00\x00\x00\x01\x08\x02\x00\x00\x00"
           b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
           b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
           b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    (ws / "test.png").write_bytes(png)
    raw, ct, status = get_raw(f"/api/file/raw?session_id={sid}&path=test.png")
    assert status == 200
    assert "image/png" in ct
    assert raw == png

def test_raw_endpoint_serves_jpeg(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    (ws / "photo.jpg").write_bytes(jpeg)
    raw, ct, status = get_raw(f"/api/file/raw?session_id={sid}&path=photo.jpg")
    assert status == 200
    assert "image/jpeg" in ct

def test_raw_endpoint_serves_svg(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    svg = b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100\" height=\"100\"><circle/></svg>"
    (ws / "icon.svg").write_bytes(svg)
    raw, ct, status = get_raw(f"/api/file/raw?session_id={sid}&path=icon.svg")
    assert status == 200
    assert "image/svg" in ct

def test_raw_endpoint_path_traversal_blocked(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    try:
        get_raw(f"/api/file/raw?session_id={sid}&path=../../etc/passwd")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code in (400, 500)

def test_raw_endpoint_missing_file_returns_404(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    try:
        get_raw(f"/api/file/raw?session_id={sid}&path=no_such_file.png")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code in (404, 500)

def test_md_file_returns_text_via_api_file(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    md = "# Hello\n\nThis is **bold**.\n"
    (ws / "README.md").write_text(md)
    data, status = get(f"/api/file?session_id={sid}&path=README.md")
    assert status == 200
    assert data["content"] == md

def test_md_file_with_table(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    md = "| Name | Value |\n|------|-------|\n| foo  | bar   |\n"
    (ws / "table.md").write_text(md)
    data, status = get(f"/api/file?session_id={sid}&path=table.md")
    assert status == 200
    assert "| Name | Value |" in data["content"]

def test_file_listing_includes_images(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    (ws / "photo.png").write_bytes(b"fake png")
    (ws / "notes.md").write_text("# Notes")
    (ws / "script.py").write_text("print('hello')")
    data, status = get(f"/api/list?session_id={sid}&path=.")
    assert status == 200
    names = {e["name"]: e for e in data["entries"]}
    assert "photo.png" in names
    assert "notes.md" in names
    assert "script.py" in names
