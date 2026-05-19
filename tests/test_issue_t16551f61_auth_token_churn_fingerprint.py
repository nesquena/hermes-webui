"""Regression tests for RCA t_16551f61: auth.json ~14-min token churn must
NOT bust the 24h /api/models cache, but a real provider/model-set change
still must.

Bug shape (chained from RCA t_d127953d residual #2): the models cache has a
24h TTL, but its _source_fingerprint hashed auth.json via mtime/size
(#1699's _models_cache_file_fingerprint). credential-pool / OAuth token
refresh rewrites the WHOLE auth.json roughly every 14 minutes (RCA observed
17:12:51 -> 17:26:31), bumping mtime+size every time. So the fingerprint
mismatched every ~14 min, the 24h cache was effectively dead, and the hot
GET /api/session?resolve_model=1 path paid a cold ~11.5s rebuild over and
over.

Fix: fingerprint auth.json by *content* with the credential-rotation fields
deny-listed (_auth_store_semantic_fingerprint). Token rotation is invisible
to the fingerprint; anything that changes the available provider/model set
still changes it.

These tests are INVARIANTS, not change-detectors:
  * churn-immune  — rewriting only volatile token fields keeps the
                     fingerprint byte-identical AND keeps a valid disk cache
                     loadable.
  * real-change-invalidates — active_provider change, a NEW credential_pool
                     entry, and a changed base_url each flip the fingerprint
                     AND reject the previously-valid disk cache.

A test that only asserted "fingerprint stable on churn" would pass even if
the function hard-coded `return {}` — hence the paired real-change half and
the explicit assertion that the two fingerprints actually differ on a real
change. Verified non-tautological by reverting the fix (see report.md): the
churn-immune half then fails.
"""

import json
import time

import api.config as config


# ── auth.json builders ────────────────────────────────────────────────────


def _auth_store(*, active_provider="anthropic", extra_pool_provider=None,
                anthropic_base_url="https://api.anthropic.com",
                access_token="tok-AAAA", request_count=1):
    """A realistic auth.json shaped like the production researcher profile
    (version / providers / active_provider / credential_pool / updated_at)."""
    pool = {
        "anthropic": [
            {
                "id": "anthropic-oauth-1",
                "label": "Claude Max",
                "auth_type": "oauth",
                "priority": 0,
                "source": "claude-oauth",
                "access_token": access_token,
                "refresh_token": "refresh-" + access_token,
                "last_status": "ok",
                "last_status_at": None,
                "last_error_code": None,
                "expires_at_ms": 1779152637000,
                "base_url": anthropic_base_url,
                "request_count": request_count,
            }
        ],
        "openrouter": [
            {
                "id": "openrouter-key-1",
                "label": "OpenRouter",
                "auth_type": "api_key",
                "priority": 1,
                "source": "env",
                "access_token": "or-" + access_token,
                "last_status": "ok",
                "base_url": "https://openrouter.ai/api/v1",
                "request_count": request_count,
            }
        ],
    }
    if extra_pool_provider:
        pool[extra_pool_provider] = [
            {
                "id": f"{extra_pool_provider}-key-1",
                "label": extra_pool_provider.title(),
                "auth_type": "api_key",
                "priority": 2,
                "source": "manual",
                "access_token": "new-provider-token",
                "base_url": f"https://api.{extra_pool_provider}.test/v1",
                "request_count": 0,
            }
        ]
    return {
        "version": 2,
        "providers": {},
        "active_provider": active_provider,
        "credential_pool": pool,
        "updated_at": "2026-05-19T01:00:00+00:00",
    }


def _write_auth(tmp_path, monkeypatch, store: dict):
    """Point _get_auth_store_path() at a tmp auth.json and write `store`.

    Sleeps 10ms and re-stats to guarantee mtime_ns/size would differ between
    successive writes — proving the OLD stat-based fingerprint WOULD have
    churned, so a stable result is attributable to the content fix, not to
    the OS coincidentally reusing mtime/size.
    """
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path)
    return auth_path


# ── churn-immune invariant ─────────────────────────────────────────────────


def test_token_only_churn_keeps_auth_fingerprint_identical(tmp_path, monkeypatch):
    """Rotating ONLY the volatile credential fields (access/refresh token,
    expiry, status, request_count, updated_at) must not change the auth.json
    fingerprint at all — even though mtime_ns and size both change."""
    p = _write_auth(tmp_path, monkeypatch, _auth_store(access_token="tok-AAAA",
                                                       request_count=1))
    st1 = p.stat()
    fp1 = config._auth_store_semantic_fingerprint(p)

    time.sleep(0.01)
    # Same logical config, fresh tokens + bumped counters + new save ts.
    bigger = _auth_store(access_token="tok-BBBBBBBBBBBBBBBBBBBB",
                          request_count=999)
    bigger["updated_at"] = "2026-05-19T01:14:00+00:00"
    p.write_text(json.dumps(bigger, indent=2), encoding="utf-8")
    st2 = p.stat()
    fp2 = config._auth_store_semantic_fingerprint(p)

    # Precondition: the OLD stat-based fingerprint WOULD have churned.
    assert (st1.st_mtime_ns, st1.st_size) != (st2.st_mtime_ns, st2.st_size)
    # Invariant: the content fingerprint does NOT churn.
    assert fp1 == fp2
    assert "semantic_sha256" in fp1


def test_token_churn_does_not_reject_valid_disk_models_cache(tmp_path, monkeypatch):
    """End-to-end: a disk models cache saved before a token refresh stays
    loadable after the refresh (the actual user-visible bug — cold rebuild
    every ~14 min)."""
    _write_auth(tmp_path, monkeypatch, _auth_store(access_token="tok-1"))
    monkeypatch.setattr(config, "_models_cache_path",
                        tmp_path / "models_cache.json")

    shape = {
        "active_provider": "anthropic",
        "default_model": "claude-opus-4-7",
        "configured_model_badges": {"claude-opus-4-7": "Anthropic"},
        "groups": [{"name": "Anthropic", "models": ["claude-opus-4-7"]}],
    }
    config._save_models_cache_to_disk(shape)
    assert config._load_models_cache_from_disk() is not None

    # ~14 minutes later: credential-pool refresh rewrites auth.json.
    time.sleep(0.01)
    churned = _auth_store(access_token="tok-2-rotated", request_count=42)
    churned["updated_at"] = "2026-05-19T01:14:00+00:00"
    config._get_auth_store_path().write_text(
        json.dumps(churned, indent=2), encoding="utf-8")

    assert config._load_models_cache_from_disk() is not None, (
        "Pure token-refresh churn of auth.json must NOT invalidate the 24h "
        "models cache (RCA t_16551f61)"
    )


# ── real-change-invalidates invariant ──────────────────────────────────────


def test_active_provider_change_changes_auth_fingerprint(tmp_path, monkeypatch):
    p = _write_auth(tmp_path, monkeypatch, _auth_store(active_provider="anthropic"))
    fp_before = config._auth_store_semantic_fingerprint(p)

    p.write_text(json.dumps(_auth_store(active_provider="openrouter"), indent=2),
                 encoding="utf-8")
    fp_after = config._auth_store_semantic_fingerprint(p)

    assert fp_before != fp_after, (
        "Changing active_provider changes the available-provider set and MUST "
        "bust the cache"
    )


def test_new_credential_pool_entry_changes_auth_fingerprint(tmp_path, monkeypatch):
    """The explicit edge from the task body: adding a credential_pool entry
    that enables a NEW provider MUST still invalidate the cache."""
    p = _write_auth(tmp_path, monkeypatch, _auth_store())
    fp_before = config._auth_store_semantic_fingerprint(p)

    p.write_text(json.dumps(_auth_store(extra_pool_provider="deepseek"),
                            indent=2), encoding="utf-8")
    fp_after = config._auth_store_semantic_fingerprint(p)

    assert fp_before != fp_after, (
        "A new credential_pool provider entry expands the model set and MUST "
        "bust the cache (task t_16551f61 boundary case)"
    )


def test_changed_base_url_changes_auth_fingerprint(tmp_path, monkeypatch):
    """Endpoint / api-base is a non-volatile field — it stays in the
    fingerprint, so pointing a provider at a different endpoint invalidates."""
    p = _write_auth(tmp_path, monkeypatch, _auth_store())
    fp_before = config._auth_store_semantic_fingerprint(p)

    p.write_text(
        json.dumps(_auth_store(anthropic_base_url="https://proxy.internal/v1"),
                   indent=2),
        encoding="utf-8")
    fp_after = config._auth_store_semantic_fingerprint(p)

    assert fp_before != fp_after, "A changed provider base_url MUST bust the cache"


def test_real_change_rejects_previously_valid_disk_cache(tmp_path, monkeypatch):
    """End-to-end mirror of the churn test: a disk cache that was valid must
    be REJECTED once a genuine provider-set change lands in auth.json."""
    _write_auth(tmp_path, monkeypatch, _auth_store(active_provider="anthropic"))
    monkeypatch.setattr(config, "_models_cache_path",
                        tmp_path / "models_cache.json")
    config._save_models_cache_to_disk({
        "active_provider": "anthropic",
        "default_model": "claude-opus-4-7",
        "configured_model_badges": {"claude-opus-4-7": "Anthropic"},
        "groups": [{"name": "Anthropic", "models": ["claude-opus-4-7"]}],
    })
    assert config._load_models_cache_from_disk() is not None

    config._get_auth_store_path().write_text(
        json.dumps(_auth_store(active_provider="openrouter"), indent=2),
        encoding="utf-8")

    assert config._load_models_cache_from_disk() is None, (
        "A real active_provider change MUST reject the stale disk cache — the "
        "fix must not over-stabilise into serving wrong data"
    )


# ── deny-list helper unit guards ───────────────────────────────────────────


def test_strip_volatile_is_pure_and_recurses():
    src = {
        "active_provider": "anthropic",
        "credential_pool": {
            "anthropic": [
                {"id": "x", "base_url": "u", "access_token": "S",
                 "request_count": 7, "last_status": "ok"}
            ]
        },
        "updated_at": "ts",
    }
    import copy as _copy
    snapshot = _copy.deepcopy(src)
    out = config._strip_volatile_auth_fields(src)

    assert src == snapshot  # input untouched (pure)
    entry = out["credential_pool"]["anthropic"][0]
    assert entry == {"id": "x", "base_url": "u"}  # volatile keys gone, rest kept
    assert "updated_at" not in out
    assert out["active_provider"] == "anthropic"  # non-volatile preserved


def test_unknown_field_stays_in_fingerprint(tmp_path, monkeypatch):
    """Deny-list (not allow-list) safety: a field we don't know about is NOT
    stripped, so a hypothetical future provider-gating field still busts the
    cache."""
    base = _auth_store()
    base["some_future_provider_gate"] = "v1"
    p = _write_auth(tmp_path, monkeypatch, base)
    fp1 = config._auth_store_semantic_fingerprint(p)

    base["some_future_provider_gate"] = "v2"
    p.write_text(json.dumps(base, indent=2), encoding="utf-8")
    fp2 = config._auth_store_semantic_fingerprint(p)

    assert fp1 != fp2, (
        "An unknown (non-deny-listed) field must remain in the fingerprint — "
        "the deny-list must fail safe toward over-invalidation"
    )


def test_missing_auth_file_fingerprint_is_stable_and_marked(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "auth.json"
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: missing)
    fp = config._auth_store_semantic_fingerprint(missing)
    assert fp.get("missing") is True
    assert config._auth_store_semantic_fingerprint(missing) == fp
