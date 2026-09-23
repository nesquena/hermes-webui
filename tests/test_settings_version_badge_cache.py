"""`/api/settings` version badge: memoize the channel-scoped `git describe`.

`/api/settings` calls ``channel_version_badge()`` on EVERY request, which shells
out to ``git describe --tags --match <channel-glob>`` (plus a dirty-suffix probe
via ``git diff-index``/``git diff``). On a loaded box that measured 4.1-10.4 s
per request. The badge is display-only, so the string is now memoized per
(channel, repo) for a short TTL — bounded, and the response shape is unchanged.
"""

import pytest

import api.updates as updates


@pytest.fixture(autouse=True)
def _reset_badge_cache():
    updates._channel_version_badge_cache_reset()
    yield
    updates._channel_version_badge_cache_reset()


def _describes(calls):
    return [c for c in calls if c and c[0] == "describe"]


def _install_fake_git(monkeypatch, version="v9.9.9"):
    calls = []

    def _fake_run_git(args, cwd, timeout=10):
        calls.append(tuple(args))
        if args and args[0] == "describe":
            glob = args[args.index("--match") + 1] if "--match" in args else "v*"
            return (version if glob == "v*" else f"exp-{version}", True)
        if args and args[0] == "diff-index":
            return ("", True)  # clean tree → no dirty suffix
        return ("", True)

    monkeypatch.setattr(updates, "_run_git", _fake_run_git)
    return calls


def test_channel_version_badge_runs_git_at_most_once_per_ttl(monkeypatch):
    calls = _install_fake_git(monkeypatch)

    first = updates.channel_version_badge("stable")
    second = updates.channel_version_badge("stable")
    assert first == second == "v9.9.9"
    assert len(_describes(calls)) == 1, (
        "git describe must not run again inside the TTL"
    )

    # Backdate the memo past the TTL: the next call must recompute.
    with updates._channel_version_badge_cache_lock:
        for key, (ts, value) in list(updates._channel_version_badge_cache.items()):
            updates._channel_version_badge_cache[key] = (
                ts - updates._CHANNEL_VERSION_BADGE_TTL_SECONDS - 1.0,
                value,
            )

    third = updates.channel_version_badge("stable")
    assert third == "v9.9.9"
    assert len(_describes(calls)) == 2, "past the TTL the badge must be recomputed"


def test_channel_version_badge_memoizes_per_channel(monkeypatch):
    calls = _install_fake_git(monkeypatch)

    assert updates.channel_version_badge("stable") == "v9.9.9"
    assert updates.channel_version_badge("experimental") == "exp-v9.9.9"
    # Both channels are now memoized: no further git calls.
    assert updates.channel_version_badge("stable") == "v9.9.9"
    assert updates.channel_version_badge("experimental") == "exp-v9.9.9"
    assert len(_describes(calls)) == 2, (
        "each channel keeps its own memo; a channel switch must not serve the other's badge"
    )


def test_channel_version_badge_value_unchanged_by_memoization(monkeypatch):
    calls = _install_fake_git(monkeypatch)
    monkeypatch.setattr(updates, "_dirty_suffix", lambda _path: "-dirty-deadbeef")

    assert updates.channel_version_badge("stable") == "v9.9.9-dirty-deadbeef"
    assert updates.channel_version_badge("stable") == "v9.9.9-dirty-deadbeef"
    assert len(_describes(calls)) == 1, (
        "the dirty-suffixed value must be memoized like the clean one"
    )


def test_channel_version_badge_falls_back_to_webui_version_and_memoizes_it(monkeypatch):
    """No reachable channel tag → the neutral installed version (unchanged
    behaviour), and the fallback is memoized too (no repeated git probes)."""
    calls = []

    def _fake_run_git(args, cwd, timeout=10):
        calls.append(tuple(args))
        return ("fatal: no tag", False)

    monkeypatch.setattr(updates, "_run_git", _fake_run_git)
    monkeypatch.setattr(updates, "WEBUI_VERSION", "v0.52.0")

    assert updates.channel_version_badge("experimental") == "v0.52.0"
    assert updates.channel_version_badge("experimental") == "v0.52.0"
    assert len(_describes(calls)) == 1
