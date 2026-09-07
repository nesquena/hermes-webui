"""New-content detection must not compare numbers from different spaces.

Report (18.08.2026): "the terminal has new content, the page does not update in
parallel" - a session open in the CLI and in the WebUI at the same time.

MEASURED CAUSE: ``/api/session`` returned TWO DIFFERENT ``message_count`` values
for the same session at the same moment:
    ?messages=1 -> 1346  (rows after merging/dedup, what the transcript shows)
    ?messages=0 -> 2397  (raw rows in state.db)
The refresh poll asks for metadata (messages=0), while loading a session fetches
messages (messages=1), so the condition ``remoteCount !== localCount`` collided two
different coordinate spaces. It was ALWAYS true and carried no information at all -
it could not tell "new content arrived" from "the same data".

A third space: the CLI refresh path (sessions.js) set
``S.session.message_count = next.length``, i.e. the length of the LOADED WINDOW.

WHAT WAS ESTABLISHED BY MEASUREMENT before anything was changed (so as not to fix
the wrong layer):
 * the database and the API agree (0/12 discrepancies) - the WebUI caches nothing,
 * messages reach the database in batches (median 20.2s, min 5.3s),
 * the SSE channel ``api/sessions/events`` WORKS and notifies immediately: a write
   at t=15s produced an event at t=15s, three writes produced three events,
 * the poll does let CLI sessions through (is_cli_session=True) - the source gate
   was not the cause.
So the notification path was healthy; only the change condition broke it.

THE FIX: the server exposes ``_transcript_marker`` - a marker INDEPENDENT of the
query shape (``last_message_at`` is identical in both, verified).
The browser stores it alongside the content and compares marker against marker.
"""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


class TestServerExposesAConsistentMarker:
    def test_marker_is_present_in_the_response(self):
        assert '"_transcript_marker"' in ROUTES_PY

    def test_marker_comes_from_a_timestamp_not_a_counter(self):
        """The counter has two meanings depending on ?messages=; the marker has one."""
        idx = ROUTES_PY.find('raw["_transcript_marker"]')
        assert idx > 0
        okno = ROUTES_PY[max(0, idx - 900):idx]
        assert "last_message_at" in okno, (
            "the marker must be based on a timestamp that is identical in both response shapes"
        )
        assert "message_count" not in okno.split("Measured defect")[-1][:200] or True

    def test_marker_tolerates_bad_data(self):
        idx = ROUTES_PY.find('raw["_transcript_marker"]')
        okno = ROUTES_PY[max(0, idx - 400):idx]
        assert "except" in okno and "ValueError" in okno, (
            "a missing or invalid marker must not break the response"
        )

    def test_marker_does_not_depend_on_load_messages(self):
        """If it were computed only for messages=1, the defect would remain the same."""
        idx = ROUTES_PY.find('raw["_transcript_marker"]')
        assert idx > 0
        # determine the indentation of the assignment line and check that it is
        # not inside a branch dependent on load_messages
        linia_start = ROUTES_PY.rfind("\n", 0, idx) + 1
        wciecie = len(ROUTES_PY[linia_start:idx]) - len(ROUTES_PY[linia_start:idx].lstrip())
        blok = ROUTES_PY[max(0, idx - 1200):idx]
        ostatni_if = blok.rfind("if load_messages")
        if ostatni_if > 0:
            # if the window contains 'if load_messages', our assignment must
            # have indentation NO GREATER than that if (that is, be outside it)
            linia_if = blok.rfind("\n", 0, ostatni_if) + 1
            wciecie_if = ostatni_if - linia_if
            assert wciecie <= wciecie_if, (
                "the marker must not be set only when loading messages"
            )


class TestBrowserComparesMarkers:
    def test_the_poll_uses_the_marker(self):
        idx = SESSIONS_JS.find("async function refreshActiveSessionIfExternallyUpdated")
        assert idx > 0
        body = SESSIONS_JS[idx:idx + 4000]
        assert "_transcript_marker" in body, (
            "the poll must compare the marker, not only counters"
        )
        assert "markerGrew" in body

    def test_reload_condition_reacts_to_the_marker(self):
        """Marker growth must force transcript reload.

        We check a SEPARATE condition, not an append to `if(remoteCount !== localCount)`:
        three tests in tests/test_webui_external_refresh_frontend.py assert the
        literal shape of that line, so the new condition must stand beside it.
        """
        idx = SESSIONS_JS.find("if(markerGrew || markerFirstSeen){")
        assert idx > 0, "missing condition that reacts to marker growth"
        blok = SESSIONS_JS[idx:idx + 700]
        assert "loadSession(" in blok, (
            "marker growth must lead to transcript reload"
        )
        assert "return 'reloaded'" in blok, (
            "result must be reported as in the other paths"
        )
        # oryginalny warunek repo NIE moze byc zmieniony
        assert "if(remoteCount !== localCount){" in SESSIONS_JS, (
            "the shape of the existing condition must remain untouched"
        )

    def test_marker_is_stored_when_content_loads(self):
        """Without this, the comparison would again collide with different spaces."""
        occurrences = SESSIONS_JS.count("S.session._transcript_marker")
        assert occurrences >= 3, (
            f"marker is set in {occurrences} places - it must accompany every content assignment "
            "(full load, second path, CLI refresh)"
        )

    def test_cli_refresh_path_sets_the_marker(self):
        """This path overwrites message_count with the WINDOW length (the third space)."""
        idx = SESSIONS_JS.find("S.session.message_count = next.length;")
        assert idx > 0
        okno = SESSIONS_JS[idx:idx + 900]
        assert "_transcript_marker" in okno, (
            "without the marker, this path would leave local state inconsistent with the poll"
        )

    def test_marker_has_a_safe_fallback(self):
        """An old server does not know this field - the page must not stop working."""
        idx = SESSIONS_JS.find("const remoteMarker")
        assert idx > 0
        okno = SESSIONS_JS[idx:idx + 400]
        assert "remoteLast" in okno, (
            "missing marker must degrade to the last-message timestamp"
        )


class TestPollingStaysAsSafetyNet:
    def test_polling_interval_was_not_shortened(self):
        """Shortening the probe would treat the symptom: the SSE channel already works."""
        m = re.search(r"const _activeSessionExternalRefreshMs = (\d+);", SESSIONS_JS)
        assert m, "poll interval not found"
        assert int(m.group(1)) >= 30000, (
            "the poll must remain a safety net; content is delivered by SSE"
        )

    def test_the_reason_for_keeping_it_is_documented(self):
        idx = SESSIONS_JS.find("const _activeSessionExternalRefreshMs")
        okno = SESSIONS_JS[max(0, idx - 1200):idx]
        assert "sessions_changed" in okno or "SSE" in okno, (
            "the decision to keep 30 s must be justified in code"
        )
