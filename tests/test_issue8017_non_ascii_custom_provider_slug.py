import pytest
from api.config import (
    _custom_provider_slug_from_name,
    _named_custom_provider_slug_for_provider,
    _unique_custom_provider_entry,
    _custom_provider_slug_key,
)


def test_non_ascii_custom_provider_slug_hash_fallback():
    # Pure non-ASCII name without provider_key or base_url derives stable hash
    slug1 = _custom_provider_slug_from_name("晨光鑫遇专用")
    slug2 = _custom_provider_slug_from_name("晨光鑫遇专用")
    assert slug1.startswith("custom:named-")
    assert slug1 == slug2
    assert len(slug1) > len("custom:named-")


def test_non_ascii_custom_provider_slug_provider_key_fallback():
    entry = {
        "name": "晨光鑫遇专用",
        "provider_key": "cgxy-cpa",
        "base_url": "http://127.0.0.1:8317/v1",
    }
    slug = _custom_provider_slug_from_name("晨光鑫遇专用", entry)
    assert slug == "custom:cgxy-cpa"

    # Also test dict-as-first-argument form
    slug_dict = _custom_provider_slug_from_name(entry)
    assert slug_dict == "custom:cgxy-cpa"


def test_non_ascii_custom_provider_slug_endpoint_fallback():
    entry = {
        "name": "晨光鑫遇专用",
        "base_url": "http://127.0.0.1:8317/v1",
    }
    slug = _custom_provider_slug_from_name("晨光鑫遇专用", entry)
    assert slug == "custom:127.0.0.1-8317"


def test_empty_or_ascii_custom_provider_slug():
    assert _custom_provider_slug_from_name("") == ""
    assert _custom_provider_slug_from_name("   ") == ""
    assert _custom_provider_slug_from_name(None) == ""
    assert _custom_provider_slug_from_name("custom:already") == "custom:already"
    assert (
        _custom_provider_slug_from_name("Local (127.0.0.1:15721)")
        == "custom:local-127.0.0.1-15721"
    )


def test_named_custom_provider_resolution_with_non_ascii_entry():
    config = {
        "custom_providers": [
            {
                "name": "晨光鑫遇专用",
                "base_url": "http://127.0.0.1:8317/v1",
                "provider_key": "cgxy-cpa",
            }
        ]
    }
    # Lookup by raw Chinese name
    assert _named_custom_provider_slug_for_provider("晨光鑫遇专用", config) == "custom:cgxy-cpa"
    # Lookup by provider_key
    assert _named_custom_provider_slug_for_provider("cgxy-cpa", config) == "custom:cgxy-cpa"
    # Lookup by slug
    assert _named_custom_provider_slug_for_provider("custom:cgxy-cpa", config) == "custom:cgxy-cpa"

    # Test unique entry resolution
    entry = _unique_custom_provider_entry(config["custom_providers"], "cgxy-cpa")
    assert entry is not None
    assert entry["name"] == "晨光鑫遇专用"
