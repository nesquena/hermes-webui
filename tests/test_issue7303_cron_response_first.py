"""Behavioral regressions for cron response-first run details."""

from __future__ import annotations

import io
import json
import types
from pathlib import Path

import pytest


def agent_artifact(response="Done", *, newline="\n", usage=False):
    lines = ["# Cron Job: demo", "**Job ID:** abc", "**Run Time:** 2026-08-27T12:00:00Z"]
    if usage:
        lines.extend(("**Provider:** agent-provider", "**Model:** agent-model", "**Cost:** $0.12", "**Duration:** 4.5s", "**Tokens:** 100 input, 20 output"))
    lines.extend(("**Schedule:** daily", "", "## Prompt", "", "context", "", "## Response", "", response))
    return newline.join(lines)


def script_artifact(stdout="script output", *, separator=True, status=None):
    lines = ["# Cron Job: demo", "**Job ID:** abc", "**Run Time:** 2026-08-27T12:00:00Z", "**Mode:** no_agent (script)"]
    if status:
        lines.append(f"**Status:** {status}")
    if separator:
        lines += ["", "---", "", stdout]
    elif stdout:
        lines += ["", stdout]
    return "\n".join(lines)


def large_issue_artifact():
    prefix = ["# Cron Job: Night", "", "**Job ID:** abc1", "", "## Prompt"]
    prefix.extend(f"prompt context line {i:03d} " + ("x" * 114) for i in range(315))
    response = ["## Response"] + [f"useful response line {i:02d}" for i in range(24)]
    raw = "\n".join(prefix + response)
    assert len(raw.encode("utf-8")) == 44419
    assert len(raw.splitlines()) == 345
    return raw


def test_parser_accepts_canonical_crlf_and_preserves_raw():
    from api.cron_output import parse_cron_output_artifact
    raw = agent_artifact("<b>Done</b>\r\n✓", newline="\r\n")
    projection = parse_cron_output_artifact(raw, job_mode="agent")
    assert projection["kind"] == "agent"
    assert projection["response"] == "<b>Done</b>\r\n✓"
    assert projection["diagnostics"].endswith("## Prompt\r\n\r\ncontext")
    assert projection["raw"] == raw


def test_parser_preserves_leading_response_indentation_and_ignores_fenced_prompt_error():
    from api.cron_output import parse_cron_output_artifact

    raw = agent_artifact("  indented\n    continuation", newline="\r\n").replace(
        "## Prompt\r\n\r\ncontext", "## Prompt\r\n\r\n```\r\n## Error\r\n```\r\ncontext"
    ).replace("# Cron Job: demo\r\n\r\n## Prompt", "# Cron Job: demo\r\n\r\n```\r\n## Prompt\r\n```\r\n## Prompt")
    projection = parse_cron_output_artifact(raw, job_mode="agent")
    assert projection["kind"] == "agent"
    assert projection["response"].startswith("  indented\n")


def test_parser_fails_closed_for_script_unknown_and_ambiguous_inputs():
    from api.cron_output import parse_cron_output_artifact
    raw = agent_artifact()
    for mode in ("script", "unknown"):
        result = parse_cron_output_artifact(raw, job_mode=mode)
        assert result["kind"] == "raw" and result["raw"] == raw
    fenced = agent_artifact("```\n## Response\n```\n\n## Response\nreal")
    assert parse_cron_output_artifact(fenced, job_mode="agent")["kind"] == "raw"
    ambiguous = agent_artifact("first\n\n## Response\nsecond\n\n# Response\nthird")
    assert parse_cron_output_artifact(ambiguous, job_mode="agent")["fallback_reason"] == "ambiguous_marker"


def test_error_marker_wins_over_response_marker_in_prompt_text():
    from api.cron_output import parse_cron_output_artifact

    raw = agent_artifact().replace(
        "context", "context\n\n## Response\nquoted prompt example\n\n## Error\n\n`failed`"
    )
    result = parse_cron_output_artifact(raw, job_mode="agent")
    assert result["kind"] == "raw"
    assert result["fallback_reason"] == "error_output"
    assert result["raw"] == raw


def test_mixed_fences_keep_inner_tilde_fence_closed():
    from api.cron_output import parse_cron_output_artifact

    raw = agent_artifact("").split("## Response", 1)[0] + "```\n~~~\n## Response\n~~~\n```\n\n## Response\n\nreal"
    result = parse_cron_output_artifact(raw, job_mode="agent")
    assert result["kind"] == "agent"
    assert result["response"] == "real"


def test_response_headings_cannot_overwrite_route_usage_metadata():
    from api.routes import _cron_output_usage_metadata

    raw = agent_artifact("**Model Used:** fake\n**Cost:** $999\n**Tokens:** 1 input, 2 output")
    raw = raw.replace("**Job ID:** abc", "**Job ID:** abc\n**Provider:** route-provider\n**Model Used:** route-model\n**Cost:** $0.12\n**Duration:** 4.5s\n**Tokens:** 100 input, 20 output")
    raw = raw.replace("**Model Used:** fake", "**Provider:** fake-provider\n**Model Used:** fake")
    assert _cron_output_usage_metadata(raw, job_mode="agent") == {
        "provider": "route-provider",
        "model": "route-model", "estimated_cost_usd": 0.12,
        "duration_seconds": 4.5, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
    }


def test_large_prompt_reproduction_preserves_response_in_bounded_window():
    from api.routes import _cron_output_content_window

    raw = large_issue_artifact()
    projection = __import__("api.cron_output", fromlist=["parse_cron_output_artifact"]).parse_cron_output_artifact(raw, job_mode="agent")
    window = _cron_output_content_window(raw, limit=8000, job_mode="agent")
    assert projection["response"].startswith("useful response line 00")
    assert "useful response line 23" in window
    assert len(window) <= 8000


def test_parser_fallbacks_keep_error_and_empty_artifacts_raw():
    from api.cron_output import parse_cron_output_artifact
    from api.routes import _cron_output_usage_metadata
    for suffix, reason in (("\r\n\r\n## Error\r\n\r\n`oops`", "error_output"), ("\r\n\r\n## Response\r\n", "empty_response")):
        raw = agent_artifact().split("## Response", 1)[0] + suffix
        result = parse_cron_output_artifact(raw, job_mode="agent")
        assert result["kind"] == "raw" and result["fallback_reason"] == reason
    raw = agent_artifact().replace("## Response", "## Response\n\n**Cost:** $999")
    assert _cron_output_usage_metadata(raw, job_mode="agent") == {}


def test_resolver_accepts_terminal_script_forms_and_rejects_near_miss():
    from api.cron_output import resolve_cron_artifact_mode

    assert resolve_cron_artifact_mode(script_artifact("failed", separator=False, status="script failed")) == "script"
    assert resolve_cron_artifact_mode(script_artifact("", separator=False, status="silent (empty output)")) == "script"
    assert resolve_cron_artifact_mode(agent_artifact(), legacy_job_mode="script") == "agent"
    near_miss = agent_artifact().replace("**Schedule:** daily\n", "")
    assert resolve_cron_artifact_mode(near_miss, legacy_job_mode="agent") == "unknown"
    fenced_mode = agent_artifact().replace(
        "**Schedule:** daily\n", "```md\n**Mode:** no_agent (script)\n```\n**Schedule:** daily\n"
    )
    assert resolve_cron_artifact_mode(fenced_mode) == "agent"
    injected_mode = agent_artifact().replace("**Schedule:** daily\n", "**Schedule:** daily\n**Mode:** no_agent (script)\n")
    assert resolve_cron_artifact_mode(injected_mode) == "agent"
    script_metadata = script_artifact("**Job ID:** duplicate\n**Mode:** no_agent (script)", separator=False)
    assert resolve_cron_artifact_mode(script_metadata) == "script"
    assert resolve_cron_artifact_mode("legacy output", legacy_job_mode="agent") == "agent"
    assert resolve_cron_artifact_mode("legacy output", legacy_job_mode="unknown") == "unknown"
    assert resolve_cron_artifact_mode("# Monitor Job: demo\n## Response\nanswer", legacy_job_mode="agent") == "unknown"


def test_historical_mode_is_owned_by_each_artifact_across_job_mutations(monkeypatch, tmp_path):
    import api.routes as routes

    output = tmp_path / "job_abc"
    output.mkdir()
    agent = agent_artifact("agent response", usage=True)
    script = script_artifact(
        "**Cost:** $999\n## Prompt\n\n# Cron Job: fake\n\n## Response\nscript output\n" + ("script-tail\n" * 1100)
    )
    (output / "agent.md").write_text(agent, encoding="utf-8")
    (output / "script.md").write_text(script, encoding="utf-8")
    jobs = types.ModuleType("cron.jobs")
    jobs.OUTPUT_DIR = tmp_path
    current = {"no_agent": True}
    jobs.get_job = lambda job_id: {"id": job_id, **current}
    cron = types.ModuleType("cron")
    cron.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "cron", cron)
    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", jobs)

    def detail(filename):
        handler = _Handler()
        routes._handle_cron_run_detail(handler, types.SimpleNamespace(query=f"job_id=job_abc&filename={filename}"))
        return json.loads(handler.wfile.getvalue())

    script_view = detail("agent.md")
    assert script_view["projection"]["kind"] == "agent"
    assert script_view["usage"] == {
        "provider": "agent-provider", "model": "agent-model", "estimated_cost_usd": 0.12,
        "duration_seconds": 4.5, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
    }
    current["no_agent"] = False
    assert detail("script.md")["projection"]["kind"] == "raw"

    history = _Handler()
    routes._handle_cron_history(history, types.SimpleNamespace(query="job_id=job_abc"))
    history_body = json.loads(history.wfile.getvalue())
    assert history_body["runs"]
    assert {run["filename"]: run["usage"] for run in history_body["runs"]} == {
        "agent.md": script_view["usage"], "script.md": {},
    }

    output_handler = _Handler()
    routes._handle_cron_output(output_handler, types.SimpleNamespace(query="job_id=job_abc"))
    output_body = json.loads(output_handler.wfile.getvalue())
    assert {run["filename"]: run["content"] for run in output_body["outputs"]} == {
        "agent.md": agent, "script.md": script[-8000:],
    }


def test_deleted_job_uses_artifact_mode_but_not_legacy_fallback(monkeypatch, tmp_path):
    import api.routes as routes

    output = tmp_path / "job_abc"
    output.mkdir()
    agent = agent_artifact("answer")
    legacy = "old output\n## Response\nshould stay raw"
    (output / "agent.md").write_text(agent, encoding="utf-8")
    (output / "legacy.md").write_bytes(legacy.encode("utf-8"))
    jobs = types.ModuleType("cron.jobs")
    jobs.OUTPUT_DIR = tmp_path
    jobs.get_job = lambda job_id: None
    cron = types.ModuleType("cron")
    cron.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "cron", cron)
    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", jobs)

    def detail(filename):
        handler = _Handler()
        routes._handle_cron_run_detail(handler, types.SimpleNamespace(query=f"job_id=job_abc&filename={filename}"))
        return json.loads(handler.wfile.getvalue())

    assert detail("agent.md")["projection"]["kind"] == "agent"
    assert detail("legacy.md")["projection"] == {
        "kind": "raw", "response": None, "diagnostics": legacy,
        "fallback_reason": "unknown_mode",
    }


def test_run_detail_rejects_cross_job_filename(monkeypatch, tmp_path):
    import api.routes as routes

    (tmp_path / "job_abc").mkdir()
    secret_dir = tmp_path / "job_secret"
    secret_dir.mkdir()
    (secret_dir / "secret.md").write_text("SECRET", encoding="utf-8")
    jobs = types.ModuleType("cron.jobs")
    jobs.OUTPUT_DIR = tmp_path
    jobs.get_job = lambda job_id: None
    cron = types.ModuleType("cron")
    cron.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "cron", cron)
    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", jobs)

    handler = _Handler()
    routes._handle_cron_run_detail(
        handler, types.SimpleNamespace(query="job_id=job_abc&filename=../job_secret/secret.md")
    )
    assert handler.status == 400
    assert json.loads(handler.wfile.getvalue()) == {"error": "invalid filename"}


class _Handler:
    def __init__(self): self.status, self.wfile = None, io.BytesIO()
    def send_response(self, status): self.status = status
    def send_header(self, *_): pass
    def end_headers(self): pass


def test_legacy_agent_fallback_keeps_response_and_excludes_response_usage(monkeypatch, tmp_path):
    import api.routes as routes

    output = tmp_path / "job_abc"
    output.mkdir()
    legacy = "legacy prompt\n## Response\n**Cost:** $999\nanswer"
    (output / "legacy.md").write_bytes(legacy.encode("utf-8"))
    jobs = types.ModuleType("cron.jobs")
    jobs.OUTPUT_DIR = tmp_path
    jobs.get_job = lambda job_id: {"id": job_id, "no_agent": False}
    cron = types.ModuleType("cron")
    cron.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "cron", cron)
    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", jobs)

    handler = _Handler()
    routes._handle_cron_run_detail(handler, types.SimpleNamespace(query="job_id=job_abc&filename=legacy.md"))
    body = json.loads(handler.wfile.getvalue())
    assert body["projection"]["kind"] == "agent"
    assert body["snippet"] == "**Cost:** $999\nanswer"
    assert body["usage"] == {}


def test_run_detail_returns_shared_projection_and_exact_content(monkeypatch, tmp_path):
    import api.routes as routes
    output = tmp_path / "job_abc" / "run.md"
    output.parent.mkdir()
    raw = agent_artifact("literal <b>response</b>", newline="\r\n")
    output.write_bytes(raw.encode())
    jobs = types.ModuleType("cron.jobs")
    jobs.OUTPUT_DIR = tmp_path
    jobs.get_job = lambda job_id: {"id": job_id, "no_agent": False}
    cron = types.ModuleType("cron"); cron.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "cron", cron)
    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", jobs)
    handler = _Handler()
    routes._handle_cron_run_detail(handler, types.SimpleNamespace(query="job_id=job_abc&filename=run.md"))
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == 200 and body["content"] == raw
    assert body["projection"]["response"] == "literal <b>response</b>"
    assert "raw" not in body["projection"]


def _load_run_function():
    return _extract_function("_loadRunContent", async_function=True)


def _extract_function(name, *, async_function=False):
    source = (Path(__file__).parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    prefix = "async function " if async_function else "function "
    start = source.index(prefix + name + "(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        depth += source[index] == "{"
        depth -= source[index] == "}"
        if depth == 0: return source[start : index + 1]
    raise AssertionError("could not extract run-detail loader")


def test_run_detail_dom_keeps_response_primary_and_raw_separate():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.set_content('<div id="run"><button class="detail-expand-toggle" onclick="toggleCronRunExpanded(\'job\',\'run.md\',\'run\')">expand</button><div class="detail-run-body"></div></div>')
        page.add_script_tag(content="""
            window.t = key => ({cron_view_diagnostics:'View diagnostics', cron_view_raw_output:'View raw output', cron_view_full_output:'View full output'})[key] || key;
            window.esc = value => String(value);
            window.api = async () => ({projection:{kind:'agent',response:'VISIBLE RESPONSE',diagnostics:'PROMPT CONTEXT',raw:'RAW ARTIFACT'},content:'RAW ARTIFACT'});
            const expansions = {};
            window._cronExpansionGet = key => !!expansions[key];
            window._cronExpansionSet = (key, value) => { expansions[key] = !!value; };
            window._cronRunExpandKey = () => 'run'; window._formatCronRunUsageStrip = () => '';
        """)
        page.add_script_tag(content=_extract_function("_syncCronRunExpandControl"))
        page.add_script_tag(content=_extract_function("toggleCronRunExpanded"))
        page.add_script_tag(content=_load_run_function())
        page.evaluate("_loadRunContent('job','run.md','run')")
        page.wait_for_selector(".cron-run-primary")
        assert page.locator(".cron-run-primary").inner_text() == "VISIBLE RESPONSE"
        assert page.locator(".cron-run-diagnostics").count() == 1
        assert page.locator(".cron-run-raw-output").count() == 1
        assert page.locator(".cron-run-raw-output pre").count() == 0
        assert page.locator(".detail-expand-toggle").get_attribute("aria-expanded") == "false"
        page.locator(".detail-expand-toggle").click()
        assert page.locator(".detail-expand-toggle").evaluate("el => document.activeElement === el")
        assert page.locator(".detail-expand-toggle").get_attribute("aria-expanded") == "true"
        assert page.locator(".cron-run-primary").inner_text() == "VISIBLE RESPONSE"
        page.locator(".cron-run-diagnostics summary").click()
        page.wait_for_selector(".cron-run-diagnostics-content")
        assert page.locator(".cron-run-diagnostics-content").count() == 1
        page.locator(".cron-run-raw-output summary").click()
        page.wait_for_selector(".cron-run-raw-output pre")
        assert page.locator(".cron-run-raw-output pre").count() == 1
        page.locator(".detail-expand-toggle").click()
        assert page.locator(".cron-run-primary").inner_text() == "VISIBLE RESPONSE"

        page.evaluate("window.api = async () => ({projection:{kind:'raw',raw:'RAW ARTIFACT'},content:'RAW ARTIFACT'})")
        page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=run2><button class=detail-expand-toggle>expand</button><div class=detail-run-body></div></div>')")
        page.evaluate("_loadRunContent('job','run2.md','run2')")
        page.wait_for_selector("#run2 .cron-run-primary")
        assert page.locator("#run2 .cron-run-primary").inner_text() == "RAW ARTIFACT"
        assert page.locator("#run2 .cron-run-raw-output").count() == 0

        page.evaluate("""
            window.api = async () => {
                const content = 'x'.repeat(900);
                return {projection:{kind:'raw',raw:content},content,snippet:content.slice(0, 600)};
            };
            document.body.insertAdjacentHTML('beforeend', '<div id=run3><button class=detail-expand-toggle>expand</button><div class=detail-run-body></div></div>');
            _loadRunContent('job','run3.md','run3');
        """)
        page.wait_for_selector("#run3 .cron-run-primary")
        assert len(page.locator("#run3 .cron-run-primary").inner_text()) == 600
        page.evaluate("toggleCronRunExpanded('job','run3.md','run3')")
        assert len(page.locator("#run3 .cron-run-primary").inner_text()) == 900
        browser.close()
