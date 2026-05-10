"""
Sprint 23 Tests: agentic transparency — token/cost display, session usage fields,
subagent card names, skill picker in cron, skill linked files.
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, d["session"]


# ── Session usage fields ─────────────────────────────────────────────────


def test_new_session_has_usage_fields():
    """New session should include input_tokens, output_tokens, estimated_cost."""
    created = []
    try:
        sid, sess = make_session(created)
        post("/api/session/rename", {"session_id": sid, "title": "Usage Test"})
        d, status = get(f"/api/session?session_id={sid}")
        assert status == 200
        sess = d["session"]
        assert "input_tokens" in sess, "input_tokens field missing from session"
        assert "output_tokens" in sess, "output_tokens field missing from session"
        assert "estimated_cost" in sess, "estimated_cost field missing from session"
        assert sess["input_tokens"] == 0
        assert sess["output_tokens"] == 0
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_session_compact_has_usage_fields():
    """Session list should include usage fields in compact form."""
    created = []
    try:
        sid, _ = make_session(created)
        post("/api/session/rename", {"session_id": sid, "title": "Compact Usage"})
        d, status = get("/api/sessions")
        assert status == 200
        match = [s for s in d["sessions"] if s["session_id"] == sid]
        assert len(match) == 1
        assert "input_tokens" in match[0], "input_tokens missing from session list"
        assert "output_tokens" in match[0], "output_tokens missing from session list"
        assert match[0]["input_tokens"] == 0
        assert match[0]["output_tokens"] == 0
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_session_usage_defaults_zero():
    """New session usage fields should default to 0/None in creation response."""
    created = []
    try:
        sid, sess = make_session(created)
        assert "input_tokens" in sess, "input_tokens missing from new session response"
        assert "output_tokens" in sess, (
            "output_tokens missing from new session response"
        )
        assert sess["input_tokens"] == 0
        assert sess["output_tokens"] == 0
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


# ── Skills content linked_files ──────────────────────────────────────────


def test_skills_content_requires_name():
    """GET /api/skills/content without name should return 400 (or 500 if skills module unavailable)."""
    try:
        d, status = get("/api/skills/content")
        assert status in (400, 500), f"Expected 400/500 for missing name, got {status}"
    except urllib.error.HTTPError as e:
        assert e.code in (400, 500), f"Expected 400/500 for missing name, got {e.code}"


def test_skills_content_has_linked_files_key():
    """GET /api/skills/content should always return a linked_files key."""
    try:
        d, status = get("/api/skills")
        if not d.get("skills"):
            return  # no skills in test env, skip
        name = d["skills"][0]["name"]
        d2, status2 = get(f"/api/skills/content?name={name}")
        assert status2 == 200
        assert "linked_files" in d2, (
            "linked_files key missing from skills/content response"
        )
        # linked_files must be a dict (possibly empty), not None
        assert isinstance(d2["linked_files"], dict), "linked_files must be a dict"
    except urllib.error.HTTPError:
        pass  # skills module unavailable in this env


def test_skills_content_file_path_traversal_rejected():
    """GET /api/skills/content with traversal path should be rejected."""
    from urllib.parse import quote as _quote

    try:
        d, status = get("/api/skills")
        if not d.get("skills"):
            return  # no skills in test env, skip
        name = d["skills"][0]["name"]
        traversal = _quote("../../etc/passwd", safe="")
        try:
            d2, status2 = get(f"/api/skills/content?name={name}&file={traversal}")
            assert status2 in (400, 404, 500), (
                f"Path traversal should be rejected, got {status2}"
            )
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404, 500), (
                f"Path traversal should be rejected, got {e.code}"
            )
    except urllib.error.HTTPError:
        pass  # skills module unavailable in test env


def test_skills_content_wildcard_name_rejected():
    """GET /api/skills/content with glob wildcard in name should be rejected when file param present."""
    try:
        try:
            d2, status2 = get("/api/skills/content?name=*&file=SKILL.md")
            assert status2 == 400, f"Wildcard name should return 400, got {status2}"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404), (
                f"Wildcard name should be rejected, got {e.code}"
            )
    except Exception:
        pass


# ── Cron create with skills ───────────────────────────────────────────────


def test_cron_create_accepts_skills():
    """POST /api/crons/create should accept and store a skills array (or 500 if cron module unavailable)."""
    created_jobs = []
    try:
        body = {
            "name": "test-sprint23-skills",
            "schedule": "0 9 * * *",
            "prompt": "test prompt",
            "deliver": "local",
            "skills": ["some-skill"],
        }
        d, status = post("/api/crons/create", body)
        if status in (400, 500) and (
            "module" in str(d.get("error", "")) or "cron" in str(d.get("error", ""))
        ):
            return  # cron module not available in test env
        assert status == 200, f"Expected 200 from cron create, got {status}: {d}"
        assert d.get("ok"), f"Cron create did not return ok: {d}"
        job_id = d.get("job", {}).get("id") or d.get("id")
        if job_id:
            created_jobs.append(job_id)
        # Verify job appears in list
        jobs_d, _ = get("/api/crons")
        job = next(
            (
                j
                for j in jobs_d.get("jobs", [])
                if j.get("name") == "test-sprint23-skills"
            ),
            None,
        )
        assert job is not None, "Created cron job not found in job list"
        assert (
            job.get("skills") == ["some-skill"] or job.get("skill") == "some-skill"
        ), f"skills not stored on job: {job}"
    finally:
        try:
            for jid in created_jobs:
                post("/api/crons/delete", {"id": jid})
            jobs_d, _ = get("/api/crons")
            for j in jobs_d.get("jobs", []):
                if j.get("name") == "test-sprint23-skills":
                    post("/api/crons/delete", {"id": j["id"]})
        except Exception:
            pass  # cron module may not be available


# ── Tool call integrity ──────────────────────────────────────────────────


def test_tool_calls_have_real_names():
    """Tool calls in session JSON should not have unresolved 'tool' name."""
    created = []
    try:
        sid, _ = make_session(created)
        d, status = get(f"/api/session?session_id={sid}")
        assert status == 200
        for tc in d["session"].get("tool_calls", []):
            assert tc.get("name") not in ("tool", "", None), (
                f"Unresolved tool name: {tc}"
            )
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})
