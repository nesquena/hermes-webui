"""#7864 round 2: startup warning for a no-op TRUST_FORWARDED_FOR opt-in.

A reverse proxy on another host (Docker bridge, a separate nginx box) that is
not listed in HERMES_WEBUI_TRUSTED_PROXY_CIDRS silently loses the log's
forwarded_for field even with HERMES_WEBUI_TRUST_FORWARDED_FOR=1 — the correct
security outcome, but existing fail2ban-style jails stop matching with no
signal. These tests pin the startup warning that closes the feedback gap.
"""

import ipaddress

import pytest

import api.auth as auth_mod


@pytest.fixture(autouse=True)
def _restore_trusted_networks():
    """The networks helper is cached; clear it around every test."""
    from api import routes

    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


class TestForwardedForStartupWarning:
    def test_no_opt_in_is_silent(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
        monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
        assert auth_mod.get_forwarded_for_startup_warning() is None

    def test_opt_in_without_non_loopback_cidr_warns(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
        warning = auth_mod.get_forwarded_for_startup_warning()
        assert warning is not None
        assert "HERMES_WEBUI_TRUSTED_PROXY_CIDRS" in warning
        # The actionable part: tell the operator exactly what to set.
        assert "172.17.0.1" in warning

    def test_opt_in_with_loopback_only_entry_still_warns(self, monkeypatch):
        # A loopback CIDR entry adds nothing to the implicit default trust.
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
        assert auth_mod.get_forwarded_for_startup_warning() is not None

    def test_opt_in_with_non_loopback_cidr_is_silent(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "172.17.0.1")
        assert auth_mod.get_forwarded_for_startup_warning() is None

    def test_opt_in_with_ipv6_cidr_is_silent(self, monkeypatch):
        # IPv6 loopback (::1) is implicit; any other IPv6 entry counts.
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "fd00::/8")
        assert auth_mod.get_forwarded_for_startup_warning() is None

    def test_mixed_list_with_one_non_loopback_is_silent(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "127.0.0.1,10.9.9.0/24")
        assert auth_mod.get_forwarded_for_startup_warning() is None

    def test_malformed_cidrs_fall_back_to_warning(self, monkeypatch):
        # Invalid entries are skipped by the network helper (never widening
        # trust), so the list is effectively empty → warn.
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "not-a-cidr,:::bad")
        assert auth_mod.get_forwarded_for_startup_warning() is not None

    def test_aggregator_surfaces_single_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
        warnings = auth_mod.get_startup_warnings()
        assert len(warnings) == 1
        assert "HERMES_WEBUI_TRUSTED_PROXY_CIDRS" in warnings[0]

    def test_aggregator_empty_when_configured(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.9.9.0/24")
        assert auth_mod.get_startup_warnings() == []

    def test_aggregator_empty_without_opt_in(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
        monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
        assert auth_mod.get_startup_warnings() == []

    def test_printer_emits_warning_with_prefix(self, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
        auth_mod.print_startup_warnings()
        out = capsys.readouterr().out
        assert "[!!] WARNING:" in out
        assert "HERMES_WEBUI_TRUSTED_PROXY_CIDRS" in out

    def test_printer_silent_when_configured(self, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.9.9.0/24")
        auth_mod.print_startup_warnings()
        out = capsys.readouterr().out
        assert out == ""


class TestSafeSubnetOf:
    def test_subnet_true(self):
        assert auth_mod._safe_subnet_of(
            ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("127.0.0.0/8")
        )

    def test_not_subnet_false(self):
        assert not auth_mod._safe_subnet_of(
            ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("127.0.0.0/8")
        )

    def test_family_mismatch_is_false_not_raise(self):
        # IPv6-in-IPv4 comparison raises TypeError in stdlib; the helper must
        # return False so a mixed-family allowlist never crashes startup.
        assert not auth_mod._safe_subnet_of(
            ipaddress.ip_network("fd00::/8"), ipaddress.ip_network("127.0.0.0/8")
        )
        assert not auth_mod._safe_subnet_of(
            ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("::1/128")
        )
