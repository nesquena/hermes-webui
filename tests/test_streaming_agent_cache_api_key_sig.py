"""Regression tests for runtime credential agent-cache signatures."""

import hashlib

from api.streaming import _agent_cache_api_key_sig


class CommandTokenSourceStub:
    """A lazy token source that must stay untouched while building cache keys."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("cache signature must not resolve a command token")

    def __str__(self):
        raise AssertionError("cache signature must not stringify a command token")


class OtherTokenSourceStub:
    def __call__(self):
        raise AssertionError("cache signature must not resolve a command token")


def _expected_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def test_callable_signature_does_not_invoke_or_stringify_source():
    source = CommandTokenSourceStub()

    signature = _agent_cache_api_key_sig(source, None)

    assert signature == (
        "callable:"
        f"{CommandTokenSourceStub.__module__}.{CommandTokenSourceStub.__qualname__}"
    )
    assert source.calls == 0


def test_callable_signature_is_stable_per_source_type():
    first = CommandTokenSourceStub()
    second = CommandTokenSourceStub()

    assert _agent_cache_api_key_sig(first, None) == _agent_cache_api_key_sig(second, None)
    assert _agent_cache_api_key_sig(first, None) != _agent_cache_api_key_sig(
        OtherTokenSourceStub(), None
    )


def test_string_and_empty_signatures_remain_content_based():
    assert _agent_cache_api_key_sig("token-a", None) == _expected_digest(b"token-a")
    assert _agent_cache_api_key_sig("token-a", None) != _agent_cache_api_key_sig(
        "token-b", None
    )
    assert _agent_cache_api_key_sig(None, None) == _expected_digest(b"")


def test_bytes_and_bytearray_use_their_raw_contents():
    expected = _expected_digest(b"token-a")

    assert _agent_cache_api_key_sig(b"token-a", None) == expected
    assert _agent_cache_api_key_sig(bytearray(b"token-a"), None) == expected
    assert _agent_cache_api_key_sig("token-a", None) == expected


def test_credential_pool_signature_takes_precedence():
    source = CommandTokenSourceStub()

    assert _agent_cache_api_key_sig(source, object()) == "credential-pool"
    assert _agent_cache_api_key_sig("token-a", object()) == "credential-pool"
    assert source.calls == 0
