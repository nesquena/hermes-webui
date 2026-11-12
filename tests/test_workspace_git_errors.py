"""Authentication failures retain their API code across Git versions (#8085)."""

import pytest

from api.workspace_git import _classify_git_error


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "fatal: could not read Username for 'https://example.invalid': terminal prompts disabled",
            "auth_failed",
        ),
        ("fatal: unable to get password from user", "auth_failed"),
        ("fatal: UNABLE TO GET PASSWORD FROM USER", "auth_failed"),
        (
            "fatal: Cannot prompt because user interactivity has been disabled.\n"
            "   at GitCredentialManager.Authentication.AuthenticationBase"
            ".ThrowIfUserInteractionDisabled()",
            "auth_failed",
        ),
        ("fatal: Authentication failed for 'https://example.invalid/repo.git'", "auth_failed"),
        ("git@example.invalid: Permission denied (publickey).", "auth_failed"),
        ("fatal: unable to access 'https://example.invalid': connection refused", "git_failed"),
        ("fatal: not a git repository", "not_a_repo"),
        ("fatal: connection timed out", "timeout"),
    ],
)
def test_classify_git_error_preserves_authentication_code(message, expected):
    assert _classify_git_error(message, ["fetch", "origin"]) == expected
