import json
import urllib.error
import urllib.parse
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR


def _get_logs(file="agent", tail=200):
    url = f"{BASE}/api/logs?file={urllib.parse.quote(str(file))}&tail={urllib.parse.quote(str(tail))}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read()), r.status


def _get_logs_error(file="agent", tail=200):
    url = f"{BASE}/api/logs?file={urllib.parse.quote(str(file))}&tail={urllib.parse.quote(str(tail))}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_logs_endpoint_tails_whitelisted_synthetic_agent_log():
    logs_dir = TEST_STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "agent.log").write_text(
        "\n".join(
            [f"2026-05-04 INFO synthetic-log-marker line {i}" for i in range(105)]
            + ["2026-05-04 ERROR synthetic-log-marker failed safely"]
        )
        + "\n",
        encoding="utf-8",
    )

    data, status = _get_logs("agent", 100)

    assert status == 200
    assert data["file"] == "agent"
    assert data["tail"] == 100
    assert len(data["lines"]) == 100
    assert data["lines"][0] == "2026-05-04 INFO synthetic-log-marker line 6"
    assert data["lines"][-1] == "2026-05-04 ERROR synthetic-log-marker failed safely"
    assert data["truncated"] is False
    assert data["total_bytes"] > 0
    assert data["mtime"] > 0
    assert data.get("hint") == ""


def test_logs_endpoint_rejects_path_traversal_and_unknown_files():
    for bad_file in ("../../etc/passwd", "agent.log", "private", "/tmp/agent"):
        data, status = _get_logs_error(bad_file, 200)
        assert status == 400
        assert "error" in data


def test_logs_endpoint_missing_file_returns_empty_lines_with_safe_hint():
    missing = TEST_STATE_DIR / "logs" / "gateway.log"
    if missing.exists():
        missing.unlink()

    data, status = _get_logs("gateway", 200)

    assert status == 200
    assert data["file"] == "gateway"
    assert data["lines"] == []
    assert data["truncated"] is False
    assert data["total_bytes"] == 0
    assert data["mtime"] is None
    assert "not found" in data["hint"].lower()
    assert str(TEST_STATE_DIR) not in data["hint"]


def test_logs_endpoint_tail_selector_is_allowlisted_and_defaults_to_200():
    logs_dir = TEST_STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "errors.log").write_text(
        "\n".join(f"2026-05-04 ERROR synthetic-log-marker line {i}" for i in range(250))
        + "\n",
        encoding="utf-8",
    )

    default_data, default_status = _get_logs("errors", "not-a-number")
    capped_data, capped_status = _get_logs("errors", 999999)
    allowed_data, allowed_status = _get_logs("errors", 100)

    assert default_status == capped_status == allowed_status == 200
    assert default_data["tail"] == 200
    assert len(default_data["lines"]) == 200
    assert capped_data["tail"] == 200
    assert len(capped_data["lines"]) == 200
    assert allowed_data["tail"] == 100
    assert len(allowed_data["lines"]) == 100


def test_logs_endpoint_reads_bounded_window_and_reports_truncation():
    logs_dir = TEST_STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    huge_prefix = "x" * (4 * 1024 * 1024 + 64)
    (logs_dir / "gateway.log").write_text(
        huge_prefix + "\n2026-05-04 INFO synthetic-log-marker tail survives\n",
        encoding="utf-8",
    )

    data, status = _get_logs("gateway", 1000)

    assert status == 200
    assert data["tail"] == 1000
    assert data["truncated"] is True
    assert data["lines"][-1] == "2026-05-04 INFO synthetic-log-marker tail survives"
    assert data["total_bytes"] > 4 * 1024 * 1024


def test_logs_endpoint_tests_use_only_synthetic_fixture_content():
    source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    assert "synthetic-log-marker" in source
    assert "/home/" + "michael" not in source
    assert "~/" + ".hermes/logs" not in source
    assert "TOK" + "EN=" not in source
    assert "PASS" + "WORD=" not in source
