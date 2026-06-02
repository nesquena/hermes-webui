import importlib


def test_serialize_runs_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_SERIALIZE_RUNS", raising=False)
    streaming = importlib.import_module("api.streaming")
    assert streaming._serialize_runs_enabled() is True


def test_serialize_runs_enabled_accepts_explicit_off_values(monkeypatch):
    streaming = importlib.import_module("api.streaming")
    for value in ("0", "false", "False", "NO", "off"):
        monkeypatch.setenv("HERMES_WEBUI_SERIALIZE_RUNS", value)
        assert streaming._serialize_runs_enabled() is False


def test_serialize_runs_enabled_keeps_unknown_values_safe(monkeypatch):
    streaming = importlib.import_module("api.streaming")
    monkeypatch.setenv("HERMES_WEBUI_SERIALIZE_RUNS", "definitely")
    assert streaming._serialize_runs_enabled() is True
