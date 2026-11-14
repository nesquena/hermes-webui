"""Regression coverage for the workspace file-existence probe.

The browser answered "does this file exist?" by scanning the parent
directory's listing. That listing is bounded — the server caps a single
``/api/list`` response — so in a workspace with more entries than the cap the
tail of the sorted directory is dropped. A file past the cap is then absent
from the listing even though it is present on disk, and the click handler
concluded "file open failed" for a file that was sitting right there.

This module covers the fix: when the parent listing does not contain the name,
the check asks the server for the file itself instead of treating the listing's
silence as proof of absence.

Provenance matters as much as existence. ``/api/file/raw`` intentionally falls
back to the requesting session's chat-attachment inbox when the workspace path
is absent, so a plain 200 does not prove that a *workspace* file exists — an
uploaded ``note.txt`` that was never written into the workspace answers 200
too. The probe therefore asks for ``workspace_only=1``, and the server honours
it by skipping the inbox fallback. The attachment route keeps its fallback for
every other caller, and escape-granted paths keep ``/api/escape/file/raw``
(which never consults the inbox).

The server-side pagination for #6645 (#6685, #6687) is a separate, larger
change; this probe deliberately does not depend on any pagination metadata, so
it stays correct on its own and remains a valid fallback once pagination lands.

An inconclusive probe must not be read as permission to open. ``api()`` times
out after 30 s, and a timeout is not evidence either way — but the follow-up
routes in ``openFile()`` were unscoped, so "proceed" meant the server answered
from the session's upload inbox and the browser downloaded an upload as if it
were a workspace file. The artifact-open chain therefore fails closed on a
timeout *and* carries the workspace scope through ``openFile()`` into every
raw preview URL and ``downloadFile()``, so the follow-up stays workspace-scoped
no matter how the check concluded. Other callers of ``openFile()`` pass no
scope and keep the attachment fallback.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import TEST_WORKSPACE  # noqa: E402
from tests._pytest_port import BASE  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def _node_env() -> dict:
    # node aborts with SIGABRT when the inherited environment carries
    # NODE_CHANNEL_FD, so the checks below run with that variable stripped.
    return {k: v for k, v in os.environ.items() if k != "NODE_CHANNEL_FD"}


def test_probe_is_used_when_the_listing_misses():
    """A name missing from the parent listing must trigger the direct probe.

    The listing may be capped, so its silence is not proof of absence.
    """
    source = WORKSPACE_JS
    exists_fn = source[ source.index("async function _workspacePathExists(") : ]
    exists_fn = exists_fn[: exists_fn.index("\n}\n")]

    assert "_workspaceProbeFile" in exists_fn
    # The listing hit short-circuits; the miss falls through to the probe.
    assert re.search(r"if\s*\(\s*hit\s*\)\s*return true", exists_fn)
    assert "_workspaceProbeFile" in exists_fn.split("if(hit)")[1]
    # A listing error must also fall back rather than report "missing".
    assert "catch" in exists_fn
    assert "_workspaceProbeFile" in exists_fn.split("catch", 1)[1]


def test_probe_uses_the_raw_route_not_the_size_capped_read_route():
    """`read` answers 404 for an over-sized file (MAX_FILE_BYTES), which would
    make a perfectly real (large) file look absent. The probe must use `raw`."""
    source = WORKSPACE_JS
    probe = source[ source.index("async function _workspaceProbeFile(") : ]
    probe = probe[: probe.index("\n}\n")]
    # The raw route with workspace-only provenance: a bare `raw` 200 could come
    # from the session's attachment inbox (see the module docstring).
    assert "_workspaceRouteForPath(path, 'raw', {workspaceOnly:true})" in probe
    assert "'read'" not in probe
    # Only a 404 counts as absent; other failures are inconclusive and must
    # not be turned into a hard "missing" verdict.
    assert re.search(r"if\s*\(\s*status\s*===\s*404\s*\)\s*return false", probe)
    # A resolved response is not proof of existence: only an explicit 2xx is.
    assert re.search(r"status\s*>=\s*200", probe), "probe must require a 2xx status"
    assert re.search(r"status\s*<\s*300", probe), "probe must reject non-2xx statuses"
    # A timeout is inconclusive, so it must never be reported as proof of
    # existence: #8421 (gate certificate 2) — answering "exists" let the
    # artifact open fall through into unscoped routes, and /api/file/raw then
    # served the session's attachment inbox for a path the workspace does not
    # have. The branch therefore fails closed.
    assert re.search(r"timeout\s*===\s*true", probe), "probe must treat a timeout as inconclusive"
    assert re.search(r"name\s*===\s*'TimeoutError'", probe)
    timeout_branch = probe[probe.index("err.timeout===true"):]
    timeout_branch = timeout_branch[: timeout_branch.index("throw err")]
    assert "return true" not in timeout_branch, timeout_branch
    # The body is dropped so a large file is not buffered for an existence check.
    assert "statusOnly:true" in probe


def test_route_builder_scopes_only_the_workspace_raw_route():
    """`workspace_only=1` belongs to the workspace raw route alone.

    The escape raw route resolves only against the authorized external root and
    has no inbox fallback, so it must stay exactly as it was.
    """
    source = WORKSPACE_JS
    body = source[ source.index("function _workspaceRouteForPathRel(") : ]
    body = body[: body.index("\n}\n")]

    assert re.search(r"if\s*\(\s*opts\.workspaceOnly\s*\)\s*extra\.push\('workspace_only=1'\)", body)
    # The escape raw route is returned from the grant branch above the
    # workspace-only flag; it must never carry the flag.
    escape_raw_line = next(
        line for line in body.splitlines() if "/api/escape/file/raw?" in line
    )
    assert "workspace_only" not in escape_raw_line
    assert body.index("/api/escape/file/raw?") < body.index("opts.workspaceOnly")


def test_server_keeps_attachment_fallback_by_default_and_scopes_it_on_request():
    """The inbox fallback is unchanged for normal callers.

    It is only skipped when the caller asks for workspace provenance, which is
    what makes the probe's 200 mean "this workspace file exists".
    """
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    target = routes[ routes.index("def _file_raw_target(") : ]
    target = target[: target.index("\ndef ")]
    handler = routes[ routes.index("def _handle_file_raw(") : ]
    handler = handler[: handler.index("\ndef ")]

    assert "allow_attachment_fallback: bool = True" in target
    assert "if not allow_attachment_fallback:" in target
    # The fallback itself is preserved verbatim.
    assert "return workspace_root, target" in target
    assert "return attachment_root, attachment_target" in target
    # And the handler wires the flag from the query string.
    assert 'qs.get("workspace_only", [""])[0] == "1"' in handler
    assert "allow_attachment_fallback=not workspace_only" in handler
    # Escape raw never gains an inbox fallback (it has its own authority).
    escape = routes[ routes.index("def _handle_escape_file_raw(") : ]
    escape = escape[: escape.index("\ndef ")]
    assert "raw_authorized_escape_target" in escape
    assert "workspace_only" not in escape

    # The capped listing is still the probe's fast path: this fix adds a
    # fallback, it does not replace the listing.
    list_handler = routes[ routes.index("def _handle_list_dir(") : ]
    list_handler = list_handler[: list_handler.index("\ndef ")]
    assert '"truncated"' not in list_handler
    assert "entries = list_dir(Path(workspace), rel_path)" in list_handler


def test_workspace_js_stays_syntactically_valid():
    """A syntax error in workspace.js breaks the whole workspace panel."""
    if shutil.which("node") is None:
        pytest.skip("node is unavailable on this host")
    result = subprocess.run(
        ["node", "--check", str(ROOT / "static" / "workspace.js")],
        capture_output=True,
        text=True,
        env=_node_env(),
    )
    assert result.returncode == 0, result.stderr


# ── node driver: exercise the REAL browser-side decision logic ──────────────
# The static assertions above only prove the source TEXT mentions the probe.
# The driver below (tests/_workspace_path_exists_probe_driver.js) extracts the
# shipped functions out of static/workspace.js and runs them against a mocked
# api(), so the decision branches are verified as behaviour, not as a grep hit.


def test_browser_existence_logic_behaviour():
    """Drive the real _workspacePathExists / _workspaceProbeFile.

    Branches verified:
      1. the parent listing contains the name  -> true, no probe
      2. the listing is capped, the name is missing -> true via the direct probe
         (this is the bug: the file was present on disk but unreachable), and
         the probe asks for workspace-only provenance
      3. the file genuinely does not exist      -> false (404 only)
      4. the listing request itself fails       -> still probes the file
      5. a non-404 probe failure               -> reported as not-openable,
         never as a fabricated "missing" verdict
      6. the probe resolves but carries NO status -> NOT reported as existing.
         A resolved response is not proof of existence; only an explicit 2xx
         is. This mirrors the upstream contract in
         tests/test_issue6867_artifacts_settled_recovery.py, where a missing
         artifact path must stay blocked.
      7. the probe TIMES OUT -> fails closed. Per #6710 a timeout is not
         evidence of absence, but #8421 (gate certificate 2) showed it is not
         evidence of presence either: answering "exists" let the artifact open
         fall through to unscoped routes, and /api/file/raw served the
         session's attachment inbox for a path the workspace does not have.
      8. an escape-granted path keeps /api/escape/file/raw (and its token),
         without the workspace-only flag
      9. a name that exists only in the attachment inbox -> false, because the
         probe asked for workspace provenance
    """
    if shutil.which("node") is None:
        pytest.skip("node is unavailable on this host")
    driver = ROOT / "tests" / "_workspace_path_exists_probe_driver.js"
    result = subprocess.run(
        ["node", str(driver), str(ROOT / "static" / "workspace.js")],
        capture_output=True,
        text=True,
        env=_node_env(),
    )
    # NOTE: a "not found" in stderr means the driver could not extract a
    # function from workspace.js — that is a REAL failure, not a missing node.
    # Skipping on it would hide the regression this test exists to catch.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL BRANCHES OK" in result.stdout


# ── real HTTP: the workspace-versus-attachment authority ────────────────────
# The gate reproduced a P2 where an inbox-only upload made the predicate true
# and openArtifactPath downloaded the uploaded copy. These tests use the real
# session, upload and raw endpoints, and drive the shipped browser functions
# against the live server, so the provenance question is answered end to end.


def _post_json(path: str, body: dict) -> tuple[dict, int]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}"), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}"), e.code


def _get(path: str) -> tuple[bytes, int]:
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        return e.read(), e.code


def _upload(session_id: str, filename: str, data: bytes) -> tuple[dict, int]:
    boundary = "----probe-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="session_id"\r\n\r\n'
        f"{session_id}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode() + data + b"\r\n" + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}"), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}"), e.code


def _new_session(workspace: Path) -> str:
    payload, status = _post_json("/api/session/new", {"workspace": str(workspace)})
    assert status == 200, payload
    sid = (payload.get("session") or {}).get("session_id") or payload.get("session_id")
    assert sid, payload
    return sid


def _probe_live(sid: str, workspace: Path, paths: list[str]) -> dict[str, dict]:
    """Run the shipped _workspacePathExists against the live server."""
    if shutil.which("node") is None:
        pytest.skip("node is unavailable on this host")
    driver = ROOT / "tests" / "_workspace_probe_live_driver.js"
    result = subprocess.run(
        [
            "node",
            str(driver),
            str(ROOT / "static" / "workspace.js"),
            BASE,
            sid,
            str(workspace),
            *paths,
        ],
        capture_output=True,
        text=True,
        env=_node_env(),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        rows[row["path"]] = row
    assert set(rows) == set(paths), result.stdout
    return rows


def _workspace_case_dir(name: str) -> Path:
    path = TEST_WORKSPACE / f"probe-{name}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def test_inbox_only_upload_is_not_a_workspace_file():
    """An upload that only lives in the inbox must not answer "exists".

    This is the gate's P2 fixture: upload note.txt through the real chat-upload
    endpoint, leave the workspace without it, then ask the probe. The raw route
    keeps its attachment fallback for normal callers, but the workspace-only
    probe must say "absent" so openArtifactPath refuses the artifact instead of
    downloading the uploaded copy.
    """
    workspace = _workspace_case_dir("inbox-only")
    sid = _new_session(workspace)
    payload, status = _upload(sid, "note.txt", b"INBOX-ONLY")
    assert status == 200, payload
    inbox_copy = Path(payload["path"])
    assert inbox_copy.read_bytes() == b"INBOX-ONLY"
    assert not (workspace / "note.txt").exists()

    # Normal attachment raw access is untouched: the unscoped route still
    # serves the inbox copy.
    body, status = _get(f"/api/file/raw?session_id={sid}&path=note.txt")
    assert status == 200, body
    assert body == b"INBOX-ONLY"

    # The workspace-only authority says it is absent.
    body, status = _get(
        f"/api/file/raw?session_id={sid}&path=note.txt&workspace_only=1"
    )
    assert status == 404, body

    row = _probe_live(sid, workspace, ["note.txt"])["note.txt"]
    assert row["exists"] is False, row
    assert any(
        "workspace_only=1" in url for url in row["requests"] if "/api/file/raw" in url
    ), row["requests"]


def test_same_name_workspace_file_wins_over_the_upload():
    """Control: the same name in both places must resolve to the workspace.

    Without this, a fix that simply disabled the inbox fallback for every
    caller (instead of scoping the probe) could still pass the inbox test.
    """
    workspace = _workspace_case_dir("same-name")
    sid = _new_session(workspace)
    payload, status = _upload(sid, "note.txt", b"INBOX-COPY")
    assert status == 200, payload
    (workspace / "note.txt").write_bytes(b"WORKSPACE-COPY")

    body, status = _get(
        f"/api/file/raw?session_id={sid}&path=note.txt&workspace_only=1"
    )
    assert status == 200, body
    assert body == b"WORKSPACE-COPY"

    # The unscoped route prefers the workspace too (pre-existing behaviour).
    body, status = _get(f"/api/file/raw?session_id={sid}&path=note.txt")
    assert status == 200, body
    assert body == b"WORKSPACE-COPY"

    row = _probe_live(sid, workspace, ["note.txt"])["note.txt"]
    assert row["exists"] is True, row


def test_cjk_file_past_the_list_cap_is_still_probeable():
    """Keep the original positive case, end to end.

    Directories sort before files and CJK names sort after ASCII, so in a
    directory past the 200-entry cap the Chinese-named file is exactly the one
    the listing drops. The probe must still find it.
    """
    workspace = _workspace_case_dir("cjk-past-cap")
    crowded = workspace / "crowded"
    crowded.mkdir()
    for i in range(205):
        (crowded / f"a{i:03d}.txt").write_text("x", encoding="utf-8")
    cjk_name = "中文文档.txt"
    (crowded / cjk_name).write_text("cjk", encoding="utf-8")
    sid = _new_session(workspace)

    listing, status = _get(f"/api/list?session_id={sid}&path=crowded")
    assert status == 200, listing
    names = [entry.get("name") for entry in json.loads(listing).get("entries", [])]
    assert cjk_name not in names, "fixture must exceed the listing cap"

    body, status = _get(
        f"/api/file/raw?session_id={sid}"
        f"&path={urllib.parse.quote('crowded/' + cjk_name)}&workspace_only=1"
    )
    assert status == 200, body
    assert body == b"cjk"

    row = _probe_live(sid, workspace, [f"crowded/{cjk_name}"])[f"crowded/{cjk_name}"]
    assert row["exists"] is True, row


# ── the timeout fall-through (gate RED certificate #2) ──────────────────────
# `_workspaceProbeFile()` used to answer "exists" when `api()` timed out, so
# `openArtifactPath()` fell through into `openFile()` — and every follow-up
# route there was UNSCOPED. `/api/file/raw` falls back to the requesting
# session's upload inbox when the workspace path is absent, so the read's
# download fallback (and the image/audio/video/PDF/HTML preview URLs) answered
# from the inbox: one artifact click downloaded a session upload as if it were
# a workspace file. The gate measured it with a real server, a real upload and
# a held `workspace_only=1` request, so the browser's unmodified 30 s `api()`
# deadline fired while every other request was answered normally.
#
# `tests/_workspace_probe_timeout_open_driver.js` replays that reproduction and
# drives the SHIPPED openArtifactPath()/openFile()/downloadFile():
#   * `--mock` injects the timeout at the api() seam (fast, deterministic);
#   * `--live` holds the probe request in fetch so the shipped api() hits its
#     own 30 s deadline against the real server.


def _run_timeout_open_driver(mode: str, *extra: str) -> dict[str, dict]:
    """Run the artifact-open driver and index its rows by scenario name."""
    if shutil.which("node") is None:
        pytest.skip("node is unavailable on this host")
    driver = ROOT / "tests" / "_workspace_probe_timeout_open_driver.js"
    result = subprocess.run(
        ["node", str(driver), str(ROOT / "static" / "workspace.js"), mode, *extra],
        capture_output=True,
        text=True,
        env=_node_env(),
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        rows[row["scenario"]] = row
    assert rows, result.stdout
    return rows


def test_probe_timeout_never_reaches_the_attachment_inbox():
    """A forced probe timeout must not read, preview or download the inbox.

    The probe is held past the 30 s `api()` deadline for a path that exists
    only in the session's upload inbox. Whatever the existence check answers,
    the click must not produce an unscoped `/api/file/raw` request — that route
    is the one the server answers from the inbox.
    """
    rows = _run_timeout_open_driver("--mock")

    for name in (
        "timeout-inbox-only-txt",   # read route -> download fallback
        "timeout-inbox-only-png",   # image preview URL
        "timeout-inbox-only-zip",   # download-only extension
        "timeout-inbox-only-mp3",   # audio preview URL
        "timeout-inbox-only-pdf",   # PDF reachability probe + frame URL
    ):
        row = rows[name]
        assert row["error"] is None, row
        # The reproduction is only meaningful if the probe really ran and was
        # workspace-scoped — otherwise the click never had a timeout to survive.
        assert row["workspaceOnlyUrls"], row
        # The whole point of the fix: an inconclusive check must not lead to an
        # unscoped read, preview or download.
        assert row["unscopedRaw"] == [], row
        assert row["downloads"] == [], row
        # The click fails closed instead of guessing that the file is there.
        assert row["opened"] is False, row
        assert row["statuses"] == ["file_open_failed"], row

    # No over-blocking: a conclusive probe still opens the workspace file and
    # previews the workspace bytes.
    ok = rows["probe-200-workspace-txt"]
    assert ok["opened"] is True, ok
    assert ok["rendered"] and ok["rendered"][0]["content"] == "WORKSPACE-BYTES", ok
    assert ok["unscopedRaw"] == [] and ok["downloads"] == [], ok

    # Every raw URL the artifact open builds is workspace-scoped, previews
    # included, and the download-only branch downloads through the scoped URL.
    png = rows["probe-200-workspace-png"]
    assert png["opened"] is True and png["previewUrls"], png
    assert all("workspace_only=1" in url for url in png["previewUrls"]), png
    assert png["unscopedRaw"] == [], png

    zipped = rows["probe-200-workspace-zip"]
    assert zipped["opened"] is False and len(zipped["downloads"]) == 1, zipped
    assert "workspace_only=1" in zipped["downloads"][0]["href"], zipped
    assert zipped["unscopedRaw"] == [], zipped

    # Escape-granted paths keep `/api/escape/file/raw` + token and gain no flag
    # (that route has no inbox fallback and stays authoritative).
    for name in ("escape-grant-txt", "escape-grant-png"):
        row = rows[name]
        assert row["opened"] is True, row
        assert row["escapeUrls"], row
        assert row["workspaceOnlyUrls"] == [], row
        assert row["unscopedRaw"] == [], row

    # Any OTHER caller of openFile() keeps the default attachment fallback:
    # its raw URLs and downloads stay unscoped, exactly as before.
    for name in ("other-caller-openFile-txt", "other-caller-openFile-png"):
        row = rows[name]
        assert row["unscopedRaw"], row
        assert row["workspaceOnlyUrls"] == [], row


def test_probe_timeout_never_reaches_the_attachment_inbox_live():
    """Same reproduction end to end: the SHIPPED api() hits its own deadline.

    A real session, a real multipart upload of ``note.txt``, no ``note.txt`` in
    the workspace, and a fetch that holds only the ``workspace_only=1``
    request. Everything else reaches the real server, so the 30 s deadline is
    the shipped one and the 404s are the server's.
    """
    workspace = _workspace_case_dir("timeout-inbox-only-live")
    sid = _new_session(workspace)
    payload, status = _upload(sid, "note.txt", b"INBOX-ONLY")
    assert status == 200, payload
    assert not (workspace / "note.txt").exists()
    (workspace / "real.txt").write_bytes(b"WORKSPACE-REAL")

    rows = _run_timeout_open_driver("--live", BASE, sid, str(workspace))

    row = rows["timeout-inbox-only-txt"]
    # The held probe really did time out through the shipped api(): its own
    # deadline fired, which is what surfaces the timeout toast.
    assert any("timed out" in str(t).lower() for t in row["toasts"]), row["toasts"]
    assert row["error"] is None, row
    assert row["unscopedRaw"] == [], row
    assert row["downloads"] == [], row
    assert row["opened"] is False, row

    # Control on the same server with nothing held: a real workspace file still
    # opens and serves the workspace bytes, not the inbox copy.
    ok = rows["probe-200-workspace-txt"]
    assert ok["opened"] is True, ok
    assert ok["rendered"] and ok["rendered"][0]["content"] == "WORKSPACE-REAL", ok
    assert ok["unscopedRaw"] == [] and ok["downloads"] == [], ok


def test_open_file_threads_the_workspace_scope_into_every_raw_route():
    """The artifact-open chain must stay workspace-scoped end to end.

    Scoping only the existence probe leaves every follow-up route unscoped:
    the read's download fallback and the image/audio/video/PDF/HTML previews
    all build `/api/file/raw`, the one route with an inbox fallback. The scope
    the artifact click asks for must reach each of them, and `downloadFile()`
    must forward it into its own URL.
    """
    source = WORKSPACE_JS
    open_fn = source[source.index("async function openFile(") :]
    open_fn = open_fn[: open_fn.index("\n}\n")]
    dl_fn = source[source.index("function downloadFile(") :]
    dl_fn = dl_fn[: dl_fn.index("\n}\n")]
    art_fn = source[source.index("async function openArtifactPath(") :]
    art_fn = art_fn[: art_fn.index("\n}\n")]

    raw_calls = re.findall(r"_workspaceRouteForPath\([^;]*?'raw'[^;]*?\)", open_fn)
    assert raw_calls, "openFile() must build its raw routes through _workspaceRouteForPath"
    for call in raw_calls:
        assert "workspaceOnly" in call, call

    dl_calls = re.findall(r"downloadFile\([^)]*\)", open_fn)
    assert dl_calls, "openFile() must route its download fallbacks through downloadFile()"
    for call in dl_calls:
        assert "workspaceOnly" in call, call

    dl_route = re.search(r"_workspaceRouteForPath\([^;]*?'raw'[^;]*?\)", dl_fn)
    assert dl_route, dl_fn
    assert "opts.workspaceOnly" in dl_route.group(0), dl_route.group(0)

    # The artifact click is the one caller that asks for the scope. Passing
    # nothing (the default) is what keeps the attachment fallback for every
    # other caller, so this call site must be explicit.
    assert re.search(r"openFile\(rel,\s*\{\s*workspaceOnly:\s*true\s*\}\)", art_fn)
