from api import streaming


CODEX_PLAN_LIMIT_ERROR = (
    'HTTP 429: {"error": {"type": "usage_limit_exceeded", '
    '"message": "Plan limit reached. You\'ve reached the limit of messages per 5 hours."}}'
)


def test_codex_oauth_usage_exhaustion_is_classified_as_quota():
    for err in [
        "Plan limit reached",
        "usage_limit_exceeded",
        "usage limit exceeded",
        "You've reached the limit of messages per 5 hours",
        "You've used up your usage",
        CODEX_PLAN_LIMIT_ERROR,
    ]:
        classified = streaming._classify_provider_error(err, Exception(err))
        assert classified["type"] == "quota_exhausted", err
        assert classified["label"] == "Out of credits"
        assert (
            "credits" in classified["hint"].lower()
            or "usage" in classified["hint"].lower()
        )


def test_silent_provider_failure_gets_specific_catch_all_error():
    classified = streaming._classify_provider_error("", None, silent_failure=True)

    assert classified["type"] == "no_response"
    assert classified["label"] == "No response from provider"
    assert "returned no content and no error" in classified["hint"]


def test_provider_error_payload_includes_bounded_redacted_details(monkeypatch):
    secret = "sk-proj-" + ("a" * 80)
    raw_error = CODEX_PLAN_LIMIT_ERROR + " token=" + secret

    monkeypatch.setattr(
        streaming, "_redact_text", lambda text: text.replace(secret, "[REDACTED]")
    )
    payload = streaming._provider_error_payload(
        raw_error, "quota_exhausted", "Switch providers"
    )

    assert payload["message"]
    assert secret not in payload["message"]
    assert payload["details"]
    assert secret not in payload["details"]
    assert "[REDACTED]" in payload["details"]
    assert len(payload["details"]) <= 1200


def test_frontend_renders_apperror_details_in_collapsible_block():
    messages_js = (
        streaming.Path(__file__).resolve().parent.parent / "static" / "messages.js"
    ).read_text()
    ui_js = (
        streaming.Path(__file__).resolve().parent.parent / "static" / "ui.js"
    ).read_text()
    style_css = (
        streaming.Path(__file__).resolve().parent.parent / "static" / "style.css"
    ).read_text()
    apperror_idx = messages_js.find("source.addEventListener('apperror'")
    warning_idx = messages_js.find("source.addEventListener('warning'", apperror_idx)
    assert apperror_idx != -1 and warning_idx != -1
    apperror_block = messages_js[apperror_idx:warning_idx]

    assert "d.details" in apperror_block
    assert "provider_details:details" in apperror_block
    assert "m.provider_details" in ui_js
    assert '<details class="provider-error-details"' in ui_js
    assert "Provider details" in ui_js
    assert ".provider-error-details" in style_css
