"""PR workflows cancel a superseded head's run; nothing else is ever cancelled or held back.

GitHub concurrency: a group admits one running and one pending run, replaces an older pending
run, and cancels a running one when cancel-in-progress is true. So only a PR's FIRST attempt may
share the per-PR group. Pushes to master, tag pushes, manual dispatches and re-runs (which keep
the original, possibly superseded, head) must each get a unique group (the run id).
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PR_WORKFLOWS = ("browser-smoke", "conversation-lifecycle", "docker-smoke", "docs-ci",
                "native-windows-startup", "tests")


def _load(name):
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def _evaluate(expr, ctx):
    """Evaluate the small subset of GitHub expression syntax these blocks use.

    `&&`/`||` return an operand like Python's and/or; `==` on these strings is plain equality.
    """
    inner = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr.strip())
    assert inner, expr
    py = inner.group(1).replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"format\('([^']*)\{0\}',\s*([\w.]+)\)", r"('\1' + str(\2))", py)
    names = {"github": ctx}
    py = re.sub(r"\bgithub\.([\w.]+)", lambda m: f"_get({m.group(1)!r})", py)

    def _get(path):
        value = ctx
        for part in path.split("."):
            value = value[part]
        return value

    return eval(py, {"__builtins__": {}, "str": str, "_get": _get, **names})  # noqa: S307 (test-only, fixed text)


def _group_and_cancel(name, ctx):
    block = _load(name)["concurrency"]
    workflow = _load(name)["name"]
    group_expr = block["group"].replace("${{ github.workflow }}", workflow)
    prefix, rest = group_expr.split("${{", 1)
    group = prefix + str(_evaluate("${{" + rest, ctx))
    cancel = block["cancel-in-progress"]
    cancel = _evaluate(cancel, ctx) if isinstance(cancel, str) else cancel
    return group, bool(cancel)


def _ctx(event, *, attempt="1", run_id="9001", pr=42):
    return {"event_name": event, "run_attempt": attempt, "run_id": run_id,
            "workflow": "x", "event": {"pull_request": {"number": pr}}}


@pytest.mark.parametrize("name", PR_WORKFLOWS)
def test_first_attempt_of_a_pr_run_shares_the_pr_group_and_cancels(name):
    group, cancel = _group_and_cancel(name, _ctx("pull_request"))
    assert group.endswith("-pr-42") and cancel


@pytest.mark.parametrize("name", PR_WORKFLOWS)
def test_a_rerun_never_cancels_or_replaces_the_latest_heads_run(name):
    group, cancel = _group_and_cancel(name, _ctx("pull_request", attempt="2", run_id="7001"))
    assert group.endswith("-7001") and not cancel


@pytest.mark.parametrize("name", PR_WORKFLOWS)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_pushes_and_dispatches_get_a_unique_group_and_are_never_cancelled(name, event):
    first, cancel = _group_and_cancel(name, _ctx(event, run_id="5001"))
    second, _ = _group_and_cancel(name, _ctx(event, run_id="5002"))
    assert first != second and not cancel


def test_release_workflow_has_no_concurrency_group():
    assert "concurrency" not in _load("release")


def test_every_pull_request_workflow_has_the_guard():
    for path in WORKFLOWS.glob("*.yml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = data.get(True) or data.get("on") or {}
        if "pull_request" in triggers:
            assert path.stem in PR_WORKFLOWS, f"{path.name} runs on pull_request without the guard"
            assert "concurrency" in data, path.name
