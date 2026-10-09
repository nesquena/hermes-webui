"""Regression coverage for Docker update notices on the experimental channel."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import api.updates as updates


ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload, *, link=""):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Link": link} if link else {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _release_payload():
    return [
        {"name": "exp-v0.52.416", "commit": {"sha": "experimental-latest"}},
        {"name": "exp-v0.52.415", "commit": {"sha": "experimental-middle"}},
        {"name": "exp-v0.52.414", "commit": {"sha": "experimental-current"}},
        {"name": "v0.52.113", "commit": {"sha": "stable-latest"}},
        {"name": "v0.52.106", "commit": {"sha": "stable-current"}},
    ]


def _matching_ref_payload():
    return [
        {"ref": "refs/tags/exp-v0.52.416", "object": {"sha": "tag-object-latest"}},
        {"ref": "refs/tags/exp-v0.52.415", "object": {"sha": "tag-object-middle"}},
        {"ref": "refs/tags/exp-v0.52.414", "object": {"sha": "tag-object-current"}},
        {"ref": "refs/tags/v0.52.113", "object": {"sha": "stable-latest"}},
    ]


def test_no_git_experimental_channel_reports_experimental_release_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(_matching_ref_payload()),
    )
    monkeypatch.setattr(updates, "WEBUI_VERSION", "exp-v0.52.414")

    info = updates._check_repo(tmp_path, "webui", channel="experimental")

    assert info == {
        "name": "webui",
        "behind": 2,
        "current_sha": "exp-v0.52.414",
        "latest_sha": "exp-v0.52.416",
        "branch": "exp-v0.52.416",
        "repo_url": "https://github.com/nesquena/hermes-webui",
        "release_based": True,
        "current_version": "exp-v0.52.414",
        "latest_version": "exp-v0.52.416",
        "compare_url": (
            "https://github.com/nesquena/hermes-webui/compare/"
            "exp-v0.52.414...exp-v0.52.416"
        ),
        "manual_update": True,
        "channel": "experimental",
        "no_git": True,
    }


def test_no_git_stable_channel_ignores_experimental_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(_release_payload()),
    )
    monkeypatch.setattr(updates, "WEBUI_VERSION", "v0.52.106")

    info = updates._check_repo(tmp_path, "webui", channel="stable")

    assert info["behind"] == 1
    assert info["current_version"] == "v0.52.106"
    assert info["latest_version"] == "v0.52.113"
    assert info["channel"] == "stable"
    assert info["no_git"] is True


def test_no_git_update_uses_channel_prefix_for_older_installed_version(tmp_path, monkeypatch):
    payload = [
        {
            "ref": f"refs/tags/exp-v0.52.{version}",
            "object": {"sha": f"tag-object-{version}"},
        }
        for version in range(1, 417)
    ]
    seen_urls = []

    def fake_urlopen(request, timeout=0):
        seen_urls.append(request.full_url)
        return _FakeResponse(payload)

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(updates, "WEBUI_VERSION", "exp-v0.52.380")

    info = updates._check_repo(tmp_path, "webui", channel="experimental")

    assert info["behind"] == 36
    assert info["current_version"] == "exp-v0.52.380"
    assert info["latest_version"] == "exp-v0.52.416"
    assert seen_urls == [
        "https://api.github.com/repos/nesquena/hermes-webui/"
        "git/matching-refs/tags/exp-v?per_page=100",
    ]


def test_experimental_matching_refs_follows_pagination(tmp_path, monkeypatch):
    first_url = (
        "https://api.github.com/repos/nesquena/hermes-webui/"
        "git/matching-refs/tags/exp-v?per_page=100"
    )
    second_url = first_url + "&page=2"
    pages = {
        first_url: _FakeResponse(
            [{"ref": "refs/tags/exp-v0.52.416", "object": {"sha": "latest"}}],
            link=f'<{second_url}>; rel="next", <{second_url}>; rel="last"',
        ),
        second_url: _FakeResponse(
            [{"ref": "refs/tags/exp-v0.52.414", "object": {"sha": "current"}}]
        ),
    }
    seen_urls = []

    def fake_urlopen(request, timeout=0):
        seen_urls.append(request.full_url)
        return pages[request.full_url]

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(updates, "WEBUI_VERSION", "exp-v0.52.414")

    info = updates._check_repo(tmp_path, "webui", channel="experimental")

    assert info["behind"] == 1
    assert info["latest_version"] == "exp-v0.52.416"
    assert seen_urls == [first_url, second_url]


def test_experimental_release_candidates_are_not_offered(tmp_path, monkeypatch):
    payload = [
        {"ref": "refs/tags/exp-v0.52.416-rc1", "object": {"sha": "candidate"}},
        {"ref": "refs/tags/exp-v0.52.416", "object": {"sha": "final"}},
        {"ref": "refs/tags/exp-v0.52.415", "object": {"sha": "current"}},
    ]
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(payload),
    )
    monkeypatch.setattr(updates, "WEBUI_VERSION", "exp-v0.52.415")

    info = updates._check_repo(tmp_path, "webui", channel="experimental")

    assert info["behind"] == 1
    assert info["latest_version"] == "exp-v0.52.416"


def test_stable_image_can_opt_into_experimental_updates(tmp_path, monkeypatch):
    payload = [
        {"ref": "refs/tags/exp-v0.52.108", "object": {"sha": "latest"}},
        {"ref": "refs/tags/exp-v0.52.107", "object": {"sha": "middle"}},
        {"ref": "refs/tags/exp-v0.52.105", "object": {"sha": "older"}},
    ]
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(payload),
    )
    monkeypatch.setattr(updates, "WEBUI_VERSION", "v0.52.106")

    info = updates._check_repo(tmp_path, "webui", channel="experimental")

    assert info["behind"] == 2
    assert info["current_version"] == "v0.52.106"
    assert info["latest_version"] == "exp-v0.52.108"
    assert info["channel"] == "experimental"


def test_experimental_pagination_fails_closed_on_repeated_next_url(monkeypatch):
    first_url = updates._GITHUB_EXPERIMENTAL_REFS_URL

    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(
            [{"ref": "refs/tags/exp-v0.52.416", "object": {"sha": "latest"}}],
            link=f'<{first_url}>; rel="next"',
        ),
    )

    assert updates._github_release_tags(channel="experimental") == []


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\b.*?\n\}}", source, re.DOTALL)
    assert match, f"missing function {name}"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize(
    ("channel", "expected_tag"),
    [("stable", "latest"), ("experimental", "experimental")],
)
def test_manual_update_instruction_matches_selected_channel(channel, expected_tag):
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    function_source = _function_source(source, "_formatManualUpdateInstruction")
    script = f"""
const t = (_key, command) => command;
{function_source}
const value = _formatManualUpdateInstruction({{
  no_git: true,
  manual_update: true,
  behind: 1,
  channel: {json.dumps(channel)}
}});
process.stdout.write(JSON.stringify(value));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert json.loads(result.stdout) == (
        f"docker pull ghcr.io/nesquena/hermes-webui:{expected_tag}"
    )
