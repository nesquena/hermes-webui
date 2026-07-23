"""Regression test for #3929 — credential-pool exhaustion classification.

Opus-advise on #3929 flagged that the error text a zero-credential pool produces,
`"All 0 credential(s) exhausted for opencode_go"`, does NOT match
_is_quota_error_text ('credential(s) exhausted' != 'credits exhausted'), so it
fell through to the generic 'Error' label with an empty hint — and in the
exception path the else-branch discarded the classification hint entirely.

This does not by itself fix the partial-work loss (that needs the reload-reconcile
reasoning-restore work + reporter logs for the incident autopsy — tracked
separately on the issue), but it gives the user an accurate, actionable message
for the exact error they hit instead of a bare 'Error'.
"""
from api import streaming


CREDENTIAL_POOL_ERRORS = [
    "All 0 credential(s) exhausted for opencode_go",
    "All 3 credential(s) exhausted for openrouter",
    "credentials exhausted",
]


def test_credential_pool_exhaustion_classified_distinctly():
    """The 'N credential(s) exhausted' pool shape gets its own classification —
    not generic 'error', not mislabeled as account quota."""
    for err in CREDENTIAL_POOL_ERRORS:
        classified = streaming._classify_provider_error(err, Exception(err))
        assert classified["type"] == "credential_pool_empty", err
        # Must carry an actionable, pool-specific hint (not empty, not top-up-account).
        assert classified["hint"], f"empty hint for {err!r}"
        assert "credential pool" in classified["hint"].lower(), err
        assert classified["label"] == "No usable credentials", err


def test_credential_pool_is_not_misclassified_as_account_quota():
    """A zero-credential POOL is a config problem, not account credit exhaustion:
    it must NOT get the 'Out of credits / top up your account' quota hint."""
    classified = streaming._classify_provider_error(
        "All 0 credential(s) exhausted for opencode_go",
        Exception("All 0 credential(s) exhausted for opencode_go"),
    )
    assert classified["type"] != "quota_exhausted"
    assert "top up" not in classified["hint"].lower()


def test_real_account_quota_still_classifies_as_quota():
    """Guard against over-matching: genuine account-credit exhaustion phrases
    must still classify as quota_exhausted, not credential_pool_empty."""
    for err in ("insufficient credits", "credits exhausted", "you have exceeded your current quota"):
        classified = streaming._classify_provider_error(err, Exception(err))
        assert classified["type"] == "quota_exhausted", err
