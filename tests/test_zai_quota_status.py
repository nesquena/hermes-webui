"""Z.AI quota status + peak-rate helper tests (plan v4, Tasks 2-4).

Behavioral tests for the z.ai monitor-endpoint integration:
- peak window membership (billing timezone anchored)
- env-overridable multipliers (plan-doc annotation, not API fact)
- timezone resolution fallbacks and display rendering
- response parsing (pure, no clock)
- quota branch envelope + single-flight cache

Run via ./scripts/test.sh per AGENTS.md.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import api.providers as providers

EDMONTON = ZoneInfo("America/Edmonton")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_zai_state(monkeypatch):
    """Isolate env overrides and any module-level zai caches per test."""
    for name in ("ZAI_PEAK_TZ", "ZAI_PEAK_MULTIPLIER", "ZAI_OFFPEAK_MULTIPLIER"):
        monkeypatch.delenv(name, raising=False)
    for attr in ("_zai_quota_cache", "_zai_quota_flights"):
        cache = getattr(providers, attr, None)
        if isinstance(cache, dict):
            with getattr(providers, "_zai_quota_cache_lock", _NULL_LOCK):
                cache.clear()
    if hasattr(providers, "_zai_quota_epoch"):
        providers._zai_quota_epoch = 0
    yield
    for attr in ("_zai_quota_cache", "_zai_quota_flights"):
        cache = getattr(providers, attr, None)
        if isinstance(cache, dict):
            with getattr(providers, "_zai_quota_cache_lock", _NULL_LOCK):
                cache.clear()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_LOCK = _NullLock()


# ── Peak membership (billing tz anchored) ────────────────────────────────────

def test_peak_weekday_afternoon_is_peak(monkeypatch):
    monkeypatch.setenv("ZAI_PEAK_MULTIPLIER", "3")
    monkeypatch.setenv("ZAI_OFFPEAK_MULTIPLIER", "1")
    s = providers._zai_peak_status(_utc(2026, 8, 19, 7))  # Wed 15:00 CST
    assert s["is_peak"] is True
    assert s["multiplier"] == 3
    assert s["source"] == "plan_docs"


def test_peak_boundaries():
    assert providers._zai_peak_status(_utc(2026, 8, 19, 6))["is_peak"] is True  # 14:00 CST inclusive
    assert providers._zai_peak_status(_utc(2026, 8, 19, 5, 59))["is_peak"] is False  # 13:59 CST
    assert providers._zai_peak_status(_utc(2026, 8, 19, 10))["is_peak"] is False  # 18:00 CST exclusive
    assert providers._zai_peak_status(_utc(2026, 8, 19, 9, 59))["is_peak"] is True  # 17:59 CST


def test_peak_weekend_is_off_peak():
    assert providers._zai_peak_status(_utc(2026, 8, 22, 7))["is_peak"] is False  # Sat 15:00 CST
    assert providers._zai_peak_status(_utc(2026, 8, 23, 7))["is_peak"] is False  # Sun 15:00 CST


def test_next_boundary_friday_evening_is_monday():
    # Fri 2026-08-21 19:00 CST (11:00 UTC) -> next boundary Mon 14:00 CST
    s = providers._zai_peak_status(_utc(2026, 8, 21, 11))
    assert s["is_peak"] is False
    assert datetime.fromisoformat(s["next_change"].replace("Z", "+00:00")) == _utc(2026, 8, 24, 6)


def test_next_boundary_during_peak_is_same_day():
    s = providers._zai_peak_status(_utc(2026, 8, 19, 7))  # Wed 15:00 CST
    assert datetime.fromisoformat(s["next_change"].replace("Z", "+00:00")) == _utc(2026, 8, 19, 10)


def test_next_boundary_saturday_is_monday():
    s = providers._zai_peak_status(_utc(2026, 8, 22, 2))  # Sat 10:00 CST
    assert datetime.fromisoformat(s["next_change"].replace("Z", "+00:00")) == _utc(2026, 8, 24, 6)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        providers._zai_peak_status(datetime(2026, 8, 19, 7))


def test_next_change_is_z_normalized_utc():
    s = providers._zai_peak_status(_utc(2026, 8, 19, 7))
    assert s["next_change"].endswith("Z")
    assert "+" not in s["next_change"].replace("Z", "") or s["next_change"].endswith("Z")


# ── Multiplier env overrides ─────────────────────────────────────────────────

def test_multiplier_env_override(monkeypatch):
    monkeypatch.setenv("ZAI_PEAK_MULTIPLIER", "5")
    monkeypatch.setenv("ZAI_OFFPEAK_MULTIPLIER", "4")
    assert providers._zai_peak_status(_utc(2026, 8, 19, 7))["multiplier"] == 5
    assert providers._zai_peak_status(_utc(2026, 8, 19, 20))["multiplier"] == 4


@pytest.mark.parametrize("bad", ["garbage", "nan", "inf", "-inf", "0", "-1", "", "  "])
def test_invalid_peak_multiplier_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv("ZAI_PEAK_MULTIPLIER", bad)
    assert providers._zai_peak_status(_utc(2026, 8, 19, 7))["multiplier"] == 3


@pytest.mark.parametrize("bad", ["garbage", "nan", "inf", "0", "-2", ""])
def test_invalid_offpeak_multiplier_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv("ZAI_OFFPEAK_MULTIPLIER", bad)
    assert providers._zai_peak_status(_utc(2026, 8, 19, 20))["multiplier"] == 2


def test_multipliers_independent(monkeypatch):
    monkeypatch.setenv("ZAI_PEAK_MULTIPLIER", "5")
    monkeypatch.setenv("ZAI_OFFPEAK_MULTIPLIER", "garbage")
    assert providers._zai_peak_status(_utc(2026, 8, 19, 7))["multiplier"] == 5
    assert providers._zai_peak_status(_utc(2026, 8, 19, 20))["multiplier"] == 2


# ── Billing timezone resolution ──────────────────────────────────────────────

@pytest.mark.parametrize("bad_tz", ["Mars/Olympus", "../../etc/passwd", "Not/AZone"])
def test_invalid_billing_tz_falls_back_to_shanghai(monkeypatch, bad_tz):
    monkeypatch.setenv("ZAI_PEAK_TZ", bad_tz)
    assert providers._zai_billing_tz() == SHANGHAI
    # membership still follows Shanghai
    assert providers._zai_peak_status(_utc(2026, 8, 19, 7))["is_peak"] is True


def test_empty_billing_tz_env_is_unset(monkeypatch):
    monkeypatch.setenv("ZAI_PEAK_TZ", "")
    assert providers._zai_billing_tz() == SHANGHAI


def test_valid_alternate_tz_intentionally_changes_membership(monkeypatch):
    """ZAI_PEAK_TZ is a deliberate billing-rule override: with UTC the window
    is weekdays 14:00-18:00 UTC. Documented dangerous-by-design."""
    monkeypatch.setenv("ZAI_PEAK_TZ", "UTC")
    assert providers._zai_peak_status(_utc(2026, 8, 17, 15))["is_peak"] is True  # Mon 15:00 UTC
    assert providers._zai_peak_status(_utc(2026, 8, 17, 13))["is_peak"] is False  # Mon 13:00 UTC
    assert "UTC" in providers._zai_peak_status(_utc(2026, 8, 17, 15))["window"]


def test_tzdata_less_host_falls_back_to_fixed_offset(monkeypatch):
    def broken(_name):
        raise providers.ZoneInfoNotFoundError(_name)

    monkeypatch.setattr(providers, "ZoneInfo", broken)
    tz = providers._zai_billing_tz()
    assert tz == timezone(timedelta(hours=8), name="CST")
    # membership still correct under fixed +8
    assert providers._zai_peak_status(_utc(2026, 8, 19, 7))["is_peak"] is True


# ── Display timezone rendering ───────────────────────────────────────────────

def test_display_renders_edmonton_summer():
    # Peak Wed 2026-08-20 16:00 CST -> boundary 18:00 CST = 10:00 UTC = 04:00 MDT
    s = providers._zai_peak_status(_utc(2026, 8, 20, 8), display_tz=EDMONTON)
    assert "04:00" in s["next_change_local"]
    assert "MDT" in s["next_change_local"]
    assert "MDT" in s["summary"]


def test_display_renders_edmonton_winter():
    # Peak Tue 2026-01-20 16:00 CST -> boundary 18:00 CST = 10:00 UTC = 03:00 MST
    s = providers._zai_peak_status(_utc(2026, 1, 20, 8), display_tz=EDMONTON)
    assert "03:00" in s["next_change_local"]
    assert "MST" in s["next_change_local"]


def test_display_dst_transitions():
    # Spring forward 2026-03-08 (Edmonton, historical rules): MST before, MDT after.
    before = providers._zai_peak_status(_utc(2026, 3, 6, 8), display_tz=EDMONTON)
    after = providers._zai_peak_status(_utc(2026, 3, 9, 8), display_tz=EDMONTON)
    assert "MST" in before["next_change_local"]
    assert "MDT" in after["next_change_local"]
    # Post-2026-11 rules come from tzdata (this machine's tzdata keeps Edmonton
    # at UTC-6 labeled CST after the 2026-11-01 transition). Assert against the
    # tzdata oracle rather than hardcoding assumptions about future rules.
    october = providers._zai_peak_status(_utc(2026, 10, 30, 8), display_tz=EDMONTON)
    november = providers._zai_peak_status(_utc(2026, 11, 2, 8), display_tz=EDMONTON)
    expected_nov = _utc(2026, 11, 2, 10).astimezone(EDMONTON).strftime("%Z")
    assert expected_nov in november["next_change_local"]
    assert november["next_change_local"] != october["next_change_local"].replace("Fri", "Mon")


def test_membership_independent_of_display_tz():
    now = _utc(2026, 8, 20, 8)
    states = [providers._zai_peak_status(now, display_tz=tz) for tz in (EDMONTON, SHANGHAI, timezone.utc)]
    assert len({s["is_peak"] for s in states}) == 1
    assert len({s["next_change"] for s in states}) == 1


def test_display_tz_string_rejected():
    with pytest.raises(TypeError):
        providers._zai_peak_status(_utc(2026, 8, 19, 7), display_tz="America/Edmonton")


# ── Response parsing (pure — no clock, no peak) ──────────────────────────────

_LITE_FIXTURE = {
    "code": 200,
    "msg": "Operation successful",
    "success": True,
    "data": {
        "level": "lite",
        "limits": [
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 100, "currentValue": 0,
             "remaining": 100, "percentage": 0, "nextResetTime": 1788374107999},
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": None, "currentValue": None,
             "remaining": None, "percentage": 9, "nextResetTime": 1787281782649},
        ],
    },
}


def test_parse_lite_prefers_percentage_when_counts_null():
    snap = providers._sanitize_zai_quota(_LITE_FIXTURE)
    assert snap.windows[0].label == "5-hour"  # priority sort puts 5-hour first
    assert snap.windows[0].used_percent == 9.0
    assert snap.windows[0].reset_at == datetime(2026, 8, 21, 3, 9, 42, 649000, tzinfo=timezone.utc)
    assert {w.label for w in snap.windows} == {"5-hour", "Monthly"}
    assert snap.plan == "lite"
    assert snap.available is True


def test_parse_counts_when_percentage_missing():
    payload = {"data": {"level": "pro", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": 200000, "remaining": 150000,
         "percentage": None, "nextResetTime": 1787281782649}]}}
    snap = providers._sanitize_zai_quota(payload)
    assert snap.windows[0].used_percent == 25.0


def test_parse_label_special_cases():
    payload = {"data": {"level": "pro", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 1, "number": 1, "percentage": 10, "nextResetTime": 1787281782649},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 20, "nextResetTime": 1787281782649},
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 5, "nextResetTime": 1787281782649}]}}
    snap = providers._sanitize_zai_quota(payload)
    assert [w.label for w in snap.windows] == ["5-hour", "Daily", "Weekly"]


def test_parse_priority_beats_earlier_monthly_reset():
    # Monthly resets BEFORE 5-hour: priority sort must still put 5-hour first.
    payload = {"data": {"level": "pro", "limits": [
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 40, "nextResetTime": 1787200000000},
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 10, "nextResetTime": 1787281782649}]}}
    snap = providers._sanitize_zai_quota(payload)
    assert snap.windows[0].label == "5-hour"


@pytest.mark.parametrize("payload", [
    [1, 2], "error", 42, None,
    {"data": "x"},
    {"data": {"limits": "not-a-list"}},
    {"success": False, "code": 500, "msg": "boom", "data": {"limits": []}},
    {"code": 403, "data": {"limits": []}},
])
def test_parse_malformed_top_level_fails_soft(payload):
    snap = providers._sanitize_zai_quota(payload)
    assert snap.available is False
    assert snap.windows == []
    assert snap.unavailable_reason


@pytest.mark.parametrize("limit", [
    None, "not-a-dict", 17,
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": float("nan"), "nextResetTime": 1787281782649},
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": float("inf"), "nextResetTime": 1787281782649},
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 9, "nextResetTime": 1e300},
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 9, "nextResetTime": "soon"},
    {"type": "WEIRD", "unit": 99, "number": 1, "percentage": 5, "nextResetTime": 1787281782649},
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5},  # no percentage, no counts
])
def test_parse_bad_limits_skipped_not_fatal(limit):
    payload = {"data": {"level": "lite", "limits": [
        limit,
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 9, "nextResetTime": 1787281782649},
    ]}}
    snap = providers._sanitize_zai_quota(payload)
    assert snap.available is True
    assert [w.label for w in snap.windows] == ["5-hour"]


def test_parse_overflow_timestamp_yields_none_reset():
    payload = {"data": {"level": "lite", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 9, "nextResetTime": 1e300}]}}
    snap = providers._sanitize_zai_quota(payload)
    assert snap.windows[0].reset_at is None


def test_parse_empty_limits_unavailable():
    snap = providers._sanitize_zai_quota({"data": {"level": "lite", "limits": []}})
    assert snap.available is False
    assert snap.windows == []


def test_parse_fetched_at_passthrough():
    stamp = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    snap = providers._sanitize_zai_quota(_LITE_FIXTURE, fetched_at=stamp)
    assert snap.fetched_at == stamp


# ── Branch envelope + cache behavior (mocked transport) ──────────────────────

class _Response:
    def __init__(self, body):
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestZaiQuotaBranch:
    @pytest.fixture(autouse=True)
    def branch_isolation(self, monkeypatch):
        monkeypatch.delenv("ZAI_PEAK_TZ", raising=False)
        monkeypatch.delenv("ZAI_PEAK_MULTIPLIER", raising=False)
        monkeypatch.delenv("ZAI_OFFPEAK_MULTIPLIER", raising=False)
        with providers._zai_quota_cache_lock:
            providers._zai_quota_cache.clear()
            providers._zai_quota_flights.clear()
        providers._zai_quota_epoch = 0
        yield
        with providers._zai_quota_cache_lock:
            providers._zai_quota_cache.clear()
            providers._zai_quota_flights.clear()

    def _mock_fetch(self, monkeypatch, payloads):
        """Payloads: list returned per call; records call count on .calls."""
        state = {"calls": 0, "gate": None}

        def fake_fetch(api_key):
            if state["gate"] is not None:
                state["gate"].wait(timeout=10)
            state["calls"] += 1
            item = payloads[min(state["calls"] - 1, len(payloads) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        recorder = SimpleNamespace(calls=0, gate=None)

        def tracked_fetch(api_key):
            if recorder.gate is not None:
                recorder.gate.wait(timeout=10)
            recorder.calls += 1
            item = payloads[min(recorder.calls - 1, len(payloads) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", tracked_fetch)
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "test-key")
        return recorder

    def test_available_envelope(self, monkeypatch):
        self._mock_fetch(monkeypatch, [_LITE_FIXTURE])
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["ok"] is True
        assert result["status"] == "available"
        assert result["supported"] is True
        assert result["quota"] is None
        windows = result["account_limits"]["windows"]
        assert windows[0]["label"] == "5-hour"
        assert windows[0]["remaining_percent"] == 91.0
        assert result["account_limits"]["plan"] == "lite"
        peak = result["peak"]
        assert peak["source"] == "plan_docs"
        assert isinstance(peak["is_peak"], bool)
        assert result["account_limits"]["details"] == [peak["summary"]]
        assert peak["summary"] in result["message"]
        assert "peak" not in result["account_limits"]

    def test_alias_routes_and_normalizes(self, monkeypatch):
        self._mock_fetch(monkeypatch, [_LITE_FIXTURE])
        result = providers.get_provider_quota("z.ai", refresh=True)
        assert result["status"] == "available"
        assert result["provider"] == "z.ai"
        assert result["display_name"] == "Z.AI / GLM"

    def test_no_key(self, monkeypatch):
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: None)
        result = providers.get_provider_quota("zai")
        assert result["ok"] is False
        assert result["status"] == "no_key"
        assert result["supported"] is True
        assert "peak" not in result

    def test_invalid_key_http_401(self, monkeypatch):
        self._mock_fetch(monkeypatch, [urllib.error.HTTPError(
            "url", 401, "Unauthorized", None, None)])
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["status"] == "invalid_key"
        assert result["ok"] is False
        assert "peak" not in result

    def test_http_500_unavailable(self, monkeypatch):
        self._mock_fetch(monkeypatch, [urllib.error.HTTPError(
            "url", 500, "Server Error", None, None)])
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["status"] == "unavailable"

    def test_transport_error_unavailable(self, monkeypatch):
        self._mock_fetch(monkeypatch, [urllib.error.URLError("timeout")])
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["status"] == "unavailable"
        assert result["ok"] is False

    def test_success_false_payload_unavailable_with_reason(self, monkeypatch):
        self._mock_fetch(monkeypatch, [{"success": False, "code": 500, "msg": "boom"}])
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["status"] == "unavailable"
        assert result["account_limits"]["available"] is False
        assert result["account_limits"]["unavailable_reason"]

    def test_cache_hit_single_fetch(self, monkeypatch):
        recorder = self._mock_fetch(monkeypatch, [_LITE_FIXTURE])
        first = providers.get_provider_quota("zai", refresh=True)
        assert recorder.calls == 1
        second = providers.get_provider_quota("zai")  # no refresh -> cache
        assert recorder.calls == 1
        assert second["status"] == "available"
        assert second["account_limits"]["fetched_at"] == first["account_limits"]["fetched_at"]

    def test_key_rotation_refetches(self, monkeypatch):
        recorder = self._mock_fetch(monkeypatch, [_LITE_FIXTURE, _LITE_FIXTURE])
        providers.get_provider_quota("zai", refresh=True)
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "rotated-key")
        providers.get_provider_quota("zai", refresh=True)
        assert recorder.calls == 2

    def test_refresh_bypasses_cache(self, monkeypatch):
        recorder = self._mock_fetch(monkeypatch, [_LITE_FIXTURE, _LITE_FIXTURE])
        providers.get_provider_quota("zai", refresh=True)
        providers.get_provider_quota("zai", refresh=True)
        assert recorder.calls == 2

    def test_invalidation_during_fetch_prevents_publish(self, monkeypatch):
        recorder = self._mock_fetch(monkeypatch, [_LITE_FIXTURE, _LITE_FIXTURE])
        real_invalidate = providers.invalidate_zai_quota_cache

        def fetch_then_invalidate(api_key):
            # Simulate a credential mutation landing mid-flight.
            real_invalidate("zai")
            recorder.calls += 0
            return _LITE_FIXTURE

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", fetch_then_invalidate)
        providers.get_provider_quota("zai", refresh=True)
        with providers._zai_quota_cache_lock:
            assert providers._zai_quota_cache == {}  # epoch guard blocked publish
        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", lambda k: _LITE_FIXTURE)
        result = providers.get_provider_quota("zai", refresh=True)
        assert result["status"] == "available"

    def test_single_flight_joins_concurrent_misses(self, monkeypatch):
        recorder = self._mock_fetch(monkeypatch, [_LITE_FIXTURE])
        gate = threading.Event()
        recorder.gate = gate
        results = []

        def caller():
            results.append(providers.get_provider_quota("zai"))  # plain miss, no refresh

        t1 = threading.Thread(target=caller)
        t1.start()
        # Wait until the flight is registered before starting the joiner.
        for _ in range(200):
            with providers._zai_quota_cache_lock:
                if providers._zai_quota_flights:
                    break
            time.sleep(0.01)
        t2 = threading.Thread(target=caller)
        t2.start()
        gate.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert recorder.calls == 1
        assert all(r["status"] == "available" for r in results)
        assert len(results) == 2

    def test_peak_once_per_available_response(self, monkeypatch):
        self._mock_fetch(monkeypatch, [_LITE_FIXTURE])
        calls = {"n": 0}
        real_peak = providers._zai_peak_status

        def counting_peak(*args, **kwargs):
            calls["n"] += 1
            return real_peak(*args, **kwargs)

        monkeypatch.setattr(providers, "_zai_peak_status", counting_peak)
        providers.get_provider_quota("zai", refresh=True)
        assert calls["n"] == 1
        providers.get_provider_quota("zai")  # cache hit still computes peak once
        assert calls["n"] == 2
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: None)
        providers.get_provider_quota("zai")
        assert calls["n"] == 2  # no_key never computes peak


class TestZaiConcurrencyRegressions:
    """Deterministic regressions for the code-review REJECT findings."""

    @pytest.fixture(autouse=True)
    def _isolation(self, monkeypatch):
        monkeypatch.delenv("ZAI_PEAK_TZ", raising=False)
        monkeypatch.delenv("ZAI_PEAK_MULTIPLIER", raising=False)
        monkeypatch.delenv("ZAI_OFFPEAK_MULTIPLIER", raising=False)
        with providers._zai_quota_cache_lock:
            providers._zai_quota_cache.clear()
            providers._zai_quota_flights.clear()
        providers._zai_quota_epoch = 0
        yield
        with providers._zai_quota_cache_lock:
            providers._zai_quota_cache.clear()
            providers._zai_quota_flights.clear()

    def _mock(self, monkeypatch, payloads):
        rec = SimpleNamespace(calls=0, gates=None)

        def fetch(api_key):
            rec.calls += 1
            item = payloads[min(rec.calls - 1, len(payloads) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", fetch)
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "k")
        return rec

    @staticmethod
    def _payload(pct):
        return {"success": True, "code": 200, "data": {"level": "lite", "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": pct,
             "nextResetTime": 1787281782649}]}}

    def test_older_fetch_cannot_overwrite_newer_refresh(self, monkeypatch):
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "k")
        # T0: slow older miss starts (will return 80)
        gates = [threading.Event()]
        real = providers._zai_fetch_quota_payload

        def slow_old(api_key):
            gates[0].wait(timeout=10)
            return real.__wrapped__ if False else self._payload(80)

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", slow_old)
        t_old = threading.Thread(target=lambda: providers.get_provider_quota("zai"))
        t_old.start()
        for _ in range(200):
            with providers._zai_quota_cache_lock:
                if providers._zai_quota_flights:
                    break
            time.sleep(0.005)
        # T1: newer refresh supersedes, returns quickly with 10
        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", lambda k: self._payload(10))
        newer = providers.get_provider_quota("zai", refresh=True)
        assert newer["account_limits"]["windows"][0]["used_percent"] == 10.0
        # T2: release the old fetch; it must NOT publish its older payload
        gates[0].set()
        t_old.join(timeout=10)
        with providers._zai_quota_cache_lock:
            entries = list(providers._zai_quota_cache.values())
        assert len(entries) == 1
        used = providers.get_provider_quota("zai")["account_limits"]["windows"][0]["used_percent"]
        assert used == 10.0  # stale 80 must not overwrite the fresh 10

    def test_waiter_never_sees_empty_cache_after_owner_success(self, monkeypatch):
        calls = {"n": 0}
        release = threading.Event()

        def owner_fetch(api_key):
            release.wait(timeout=10)
            calls["n"] += 1
            return self._payload(42)

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", owner_fetch)
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "k")
        results = {}
        t_owner = threading.Thread(target=lambda: results.setdefault("owner", providers.get_provider_quota("zai")))
        t_owner.start()
        for _ in range(200):
            with providers._zai_quota_cache_lock:
                if providers._zai_quota_flights:
                    break
            time.sleep(0.005)
        t_waiter = threading.Thread(target=lambda: results.setdefault("waiter", providers.get_provider_quota("zai")))
        t_waiter.start()
        time.sleep(0.1)
        release.set()
        t_owner.join(timeout=10)
        t_waiter.join(timeout=10)
        assert results["owner"]["status"] == "available"
        assert results["waiter"]["status"] == "available"
        # The invariant: the waiter must NOT have needed its own transport
        # call — the owner's publish happened before the flight was signaled.
        assert calls["n"] == 1, f"extra fetches beyond the owner: {calls['n'] - 1}"

    def test_superseded_owner_cannot_evict_newer_flight(self, monkeypatch):
        # Older refresh holds flight A; newer refresh registers flight B.
        # The newer owner is gated so flight B is still REGISTERED when the
        # older owner finishes — asserting A's cleanup cannot remove B.
        old_release = threading.Event()
        new_release = threading.Event()

        def old_refresh(api_key):
            old_release.wait(timeout=10)
            return self._payload(5)

        def new_refresh(api_key):
            new_release.wait(timeout=10)
            return self._payload(70)

        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", old_refresh)
        monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "k")
        t_old = threading.Thread(target=lambda: providers.get_provider_quota("zai", refresh=True))
        t_old.start()
        for _ in range(200):
            with providers._zai_quota_cache_lock:
                if providers._zai_quota_flights:
                    break
            time.sleep(0.005)
        monkeypatch.setattr(providers, "_zai_fetch_quota_payload", new_refresh)
        t_new = threading.Thread(target=lambda: None if False else None)  # placeholder
        # Newer refresh registers its own flight while still gated.
        newer_result = {}

        def run_newer():
            newer_result["r"] = providers.get_provider_quota("zai", refresh=True)

        t_new = threading.Thread(target=run_newer)
        t_new.start()
        for _ in range(200):
            with providers._zai_quota_cache_lock:
                flights_now = list(providers._zai_quota_flights.values())
                if len(flights_now) == 1:
                    newer_event = flights_now[0]
                    if newer_event is not None:
                        break
            time.sleep(0.005)
        # Wait until the newer flight object actually replaced the older one.
        cache_key = f"zai|{providers._get_hermes_home()}|{hashlib.sha256(b'k').hexdigest()}"
        newer_event = None
        for _ in range(400):
            with providers._zai_quota_cache_lock:
                current = providers._zai_quota_flights.get(cache_key)
                if current is not None:
                    newer_event = current
                    break
            time.sleep(0.005)
        # Old owner completes while the newer flight is still registered.
        old_release.set()
        t_old.join(timeout=10)
        with providers._zai_quota_cache_lock:
            still_registered = any(e is newer_event for e in providers._zai_quota_flights.values())
        assert still_registered, "older owner evicted the newer flight"
        # Now let the newer owner finish and confirm its payload wins.
        new_release.set()
        t_new.join(timeout=10)
        used = providers.get_provider_quota("zai")["account_limits"]["windows"][0]["used_percent"]
        assert used == 70.0  # newer refresh's payload, not the older 5
