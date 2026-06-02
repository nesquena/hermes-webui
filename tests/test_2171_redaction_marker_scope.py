from api.helpers import _might_contain_sensitive_text, _redact_text


def test_plain_urls_no_longer_trigger_sensitive_prefilter():
    assert _might_contain_sensitive_text("See https://example.com/docs?page=1") is False


def test_url_userinfo_and_secret_query_params_still_trigger_prefilter():
    assert _might_contain_sensitive_text("https://user:secret@example.com/path") is True
    assert _might_contain_sensitive_text("https://api.example.com/v1?token=secret") is True
    assert _might_contain_sensitive_text("postgres://user:pass@db/app") is True


def test_url_userinfo_and_secret_query_values_are_redacted():
    text = "u=https://user:secret@example.com/path t=https://api.example.com/v1?token=abc"
    redacted = _redact_text(text)
    assert "secret" not in redacted
    assert "token=abc" not in redacted
