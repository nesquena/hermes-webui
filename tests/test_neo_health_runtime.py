"""Neo Sprint 2 health endpoints expose runtime VPS readings."""

from api import health


def test_vps_health_uses_runtime_metric_helpers():
    src = health.build_vps_health.__code__.co_names
    assert "_cpu_percent" in src
    assert "_memory_percent" in src
    assert "_disk_percent" in src
    assert "_network_percent" in src


def test_vps_health_metrics_are_bounded_and_source_tagged():
    data = health.build_vps_health()
    metrics = data["metrics"]
    assert [m["id"] for m in metrics] == ["cpu", "ram", "disk", "network"]
    for metric in metrics:
        assert 0 <= metric["value"] <= 100
        assert metric["source"] in {"procfs", "filesystem", "os", "fallback"}
