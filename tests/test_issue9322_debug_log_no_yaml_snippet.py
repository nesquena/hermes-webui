"""DEBUG records for an unparsable profile config must not carry the bad line (#9322).

The YAML parser's traceback quotes the offending line, which can hold a value the
operator does not want in a log file. Both profile-config load sites log the path
and the error class instead, at DEBUG, and keep their empty-config fallback.
"""

import logging

SECRET = "REDACTED-9322-API-KEY-VALUE"
# Unterminated quoted scalar: the parser's error message quotes the line it broke on.
MALFORMED = f'api_key: "{SECRET}\nother: 1\n'


def _write_config(tmp_path, name="profile"):
    home = tmp_path / name
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(MALFORMED, encoding="utf-8")
    return home, config_path


def _module_records(caplog, logger_name):
    return [record for record in caplog.records if record.name == logger_name]


def _assert_record_is_safe(record, config_path):
    message = record.getMessage()
    assert record.levelno == logging.DEBUG
    assert record.exc_info is None
    assert str(config_path) in message
    assert SECRET not in message
    assert "SEGREDO" not in message
    # The error class is named so the record stays diagnosable without the snippet.
    assert "ScannerError" in message


def test_load_profile_config_debug_record_omits_the_malformed_line(tmp_path, caplog):
    from api.profiles import load_profile_config

    home, config_path = _write_config(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="api.profiles"):
        assert load_profile_config(home) == {}

    records = _module_records(caplog, "api.profiles")
    assert records, "an unparsable config should still leave a DEBUG record"
    _assert_record_is_safe(records[0], config_path)
    # Nothing formatted out of the records (traceback included) carries the line.
    assert SECRET not in caplog.text
    assert "api_key" not in caplog.text


def test_external_skill_roots_debug_record_omits_the_malformed_line(tmp_path, caplog):
    from api.skill_runtime import profile_external_skill_dirs

    home, config_path = _write_config(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="api.skill_runtime"):
        assert profile_external_skill_dirs(home) == []

    records = _module_records(caplog, "api.skill_runtime")
    assert records, "an unparsable config should still leave a DEBUG record"
    _assert_record_is_safe(records[0], config_path)
    assert SECRET not in caplog.text
    assert "api_key" not in caplog.text


def test_malformed_profile_config_still_degrades_to_defaults(tmp_path):
    """The fallback the callers see is unchanged: empty config, no external roots."""
    from api.profiles import load_profile_config
    from api.skill_runtime import profile_external_skill_dirs

    home, _ = _write_config(tmp_path)
    assert load_profile_config(home) == {}
    assert profile_external_skill_dirs(home) == []
