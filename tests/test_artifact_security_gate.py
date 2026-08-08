"""Security regressions for the artifact gate (PR #6210 re-gate).

Each class here pins one of the six blocking findings from the static security
re-gate at f4e5f5c4c7c0. They drive the library directly (no live server) so
they can assert on the boundary rather than on a rendered response.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import api.artifacts as artifacts


@pytest.fixture(autouse=True)
def _isolated_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "artifacts_enabled", lambda: True)
    yield


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A profile home + WebUI state root, as the request would resolve them."""
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "workspace").mkdir(parents=True)
    state_dir = hermes_home / "webui"
    (state_dir / "sessions").mkdir(parents=True)
    (state_dir / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(artifacts, "STATE_DIR", state_dir)
    monkeypatch.setattr(artifacts, "_request_hermes_home", lambda: hermes_home.resolve())
    return hermes_home


def _write(path: Path, text: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _publish(path, *, owner="local@default", session_id=None, **kwargs):
    source = artifacts.validate_source_path(str(path))
    cap = artifacts.mint_source_capability(
        str(source), owner=owner, session_id=session_id,
        fingerprint=artifacts.source_fingerprint(source.stat()),
    )
    return artifacts.publish_artifact(
        str(source), owner=owner, session_id=session_id, capability=cap, **kwargs
    )


# ── Finding 1: source path may not cross profile/state boundaries ────────────


class TestSourceBoundary:
    def test_webui_state_is_denied_even_though_it_is_two_levels_deep(self, home):
        """`STATE_DIR` defaults to `<HERMES_HOME>/webui`.

        The old check compared only the FIRST path component below a Hermes
        root, so `webui/sessions/...` and `webui/artifacts/...` were never
        denied — the deny list named `sessions`, which is the second component.
        """
        victim = _write(home / "webui" / "sessions" / "secret.json", '{"a":1}')
        with pytest.raises(ValueError, match="state directories"):
            artifacts.validate_source_path(str(victim))

    def test_named_profile_webui_state_is_denied(self, home):
        """A NAMED profile's WebUI state lives at `<home>/webui_state`.

        Denying only `webui` (the default STATE_DIR layout) left every named
        profile's actual chat sessions publishable, because `<home>` is itself
        an allowed root. `/api/media` already knew about this directory.
        """
        victim = _write(
            home / "webui_state" / "sessions" / "session_abc123.json", "{}"
        )
        with pytest.raises(ValueError, match="state directories"):
            artifacts.validate_source_path(str(victim))

    def test_named_profile_webui_state_root_itself_is_denied(self, home):
        victim = _write(home / "webui_state" / "workspaces.json", "{}")
        with pytest.raises(ValueError, match="state directories"):
            artifacts.validate_source_path(str(victim))

    def test_named_profile_state_is_denied(self, home):
        """Named-profile state begins with `profiles/<name>/…`."""
        victim = _write(home / "profiles" / "work" / "sessions" / "s.json", "{}")
        with pytest.raises(ValueError, match="state directories"):
            artifacts.validate_source_path(str(victim))

    def test_a_workspace_carveout_cannot_re_admit_state(self, home, monkeypatch):
        """Pointing the workspace at a Hermes root used to disable ALL denial.

        `_in_denied_state_subdir` returned False outright for anything inside
        the active workspace, so a workspace set to the Hermes home re-admitted
        every state directory the deny list exists to protect.
        """
        monkeypatch.setattr(
            "api.workspace.get_last_workspace", lambda: str(home), raising=False
        )
        victim = _write(home / "webui" / "sessions" / "secret.json", "{}")
        with pytest.raises(ValueError, match="state directories"):
            artifacts.validate_source_path(str(victim))

    def test_a_real_deliverable_in_the_home_still_publishes(self, home):
        ok = _write(home / "workspace" / "report.html", "<p>fine</p>")
        assert artifacts.validate_source_path(str(ok)) == ok.resolve()

    def test_roots_come_from_the_requesting_profile(self, tmp_path, monkeypatch):
        """The root set is derived from THIS request's profile home.

        Asserted on the root set rather than on a rejection, because the test
        sandbox itself lives under /tmp — which is legitimately publishable —
        so a foreign path there would be admitted for an unrelated reason and
        the test would prove nothing.
        """
        mine = tmp_path / "mine"
        theirs = tmp_path / "theirs"
        (mine / "webui").mkdir(parents=True)
        theirs.mkdir(parents=True)
        monkeypatch.setattr(artifacts, "STATE_DIR", mine / "webui")
        monkeypatch.setattr(artifacts, "_request_hermes_home", lambda: mine.resolve())
        monkeypatch.setattr(
            "api.workspace.get_last_workspace", lambda: str(mine), raising=False
        )
        monkeypatch.delenv("ARTIFACT_ALLOWED_ROOTS", raising=False)

        roots = artifacts._allowed_source_roots()
        assert mine.resolve() in roots
        assert theirs.resolve() not in roots


class TestSourceCapability:
    def test_publish_without_a_capability_is_refused(self, home):
        src = _write(home / "workspace" / "a.html", "<p>x</p>")
        with pytest.raises(artifacts.ArtifactCapabilityError):
            artifacts.publish_artifact(str(src), owner="local@default")

    def test_a_capability_for_another_file_does_not_authorize_this_one(self, home):
        a = _write(home / "workspace" / "a.html", "<p>a</p>")
        b = _write(home / "workspace" / "b.html", "<p>b</p>")
        cap = artifacts.mint_source_capability(
            str(a.resolve()), owner="local@default",
            fingerprint=artifacts.source_fingerprint(a.stat()),
        )
        with pytest.raises(artifacts.ArtifactCapabilityError, match="does not authorize"):
            artifacts.publish_artifact(str(b), owner="local@default", capability=cap)

    def test_a_capability_minted_for_another_owner_is_refused(self, home):
        src = _write(home / "workspace" / "a.html", "<p>a</p>")
        cap = artifacts.mint_source_capability(
            str(src.resolve()), owner="other@default",
            fingerprint=artifacts.source_fingerprint(src.stat()),
        )
        with pytest.raises(artifacts.ArtifactCapabilityError):
            artifacts.publish_artifact(str(src), owner="local@default", capability=cap)

    def test_a_capability_bound_to_another_session_is_refused(self, home):
        src = _write(home / "workspace" / "a.html", "<p>a</p>")
        cap = artifacts.mint_source_capability(
            str(src.resolve()), owner="local@default", session_id="s1",
            fingerprint=artifacts.source_fingerprint(src.stat()),
        )
        with pytest.raises(artifacts.ArtifactCapabilityError):
            artifacts.publish_artifact(
                str(src), owner="local@default", session_id="s2", capability=cap
            )

    def test_an_expired_capability_is_refused(self, home, monkeypatch):
        src = _write(home / "workspace" / "a.html", "<p>a</p>")
        cap = artifacts.mint_source_capability(
            str(src.resolve()), owner="local@default",
            fingerprint=artifacts.source_fingerprint(src.stat()),
        )
        monkeypatch.setattr(
            time, "time", lambda: cap["exp"] + 1, raising=False
        )
        with pytest.raises(artifacts.ArtifactCapabilityError, match="expired"):
            artifacts.publish_artifact(str(src), owner="local@default", capability=cap)

    def test_a_forged_signature_is_refused(self, home):
        src = _write(home / "workspace" / "a.html", "<p>a</p>")
        cap = artifacts.mint_source_capability(
            str(src.resolve()), owner="local@default",
            fingerprint=artifacts.source_fingerprint(src.stat()),
        )
        cap["sig"] = "0" * len(cap["sig"])
        with pytest.raises(artifacts.ArtifactCapabilityError):
            artifacts.publish_artifact(str(src), owner="local@default", capability=cap)


# ── Finding 2: ownership is a stable principal, not a cookie ─────────────────


class TestStableOwnership:
    def test_a_re_login_keeps_the_same_owner(self, home):
        """The bug: ownership was the random session token.

        A re-login or an expiry minted a new token, so the user's own durable
        artifacts became invisible and unmanageable to them.
        """
        src = _write(home / "workspace" / "r.html", "<p>r</p>")
        first = _publish(src, owner="password:alice@default")
        # A new session for the SAME principal in the SAME profile.
        listed = artifacts.list_artifacts(owner="password:alice@default")
        assert [a["token"] for a in listed] == [first["token"]]

    def test_a_profile_switch_does_not_carry_ownership(self, home):
        """One long-lived cookie used to own artifacts across profiles."""
        src = _write(home / "workspace" / "p.html", "<p>p</p>")
        _publish(src, owner="password:alice@alpha")
        assert artifacts.list_artifacts(owner="password:alice@alpha")
        assert artifacts.list_artifacts(owner="password:alice@bravo") == []

    def test_a_different_principal_sees_nothing(self, home):
        src = _write(home / "workspace" / "q.html", "<p>q</p>")
        _publish(src, owner="password:alice@default")
        assert artifacts.list_artifacts(owner="oidc:bob@default") == []

    def test_a_legacy_record_fails_closed_for_every_principal(self, home):
        """"We cannot tell who owns this" must not mean "everyone owns this".

        Legacy records carry a dead session token as their owner, so no live
        request can match it. Treating that as claimable let ANY authenticated
        principal read a stranger\'s artifact and then adopt it — first-come
        takeover, in a deployment whose whole point is separate principals.
        The bytes stay on disk for an operator to migrate deliberately; what is
        gone is the silent claim.
        """
        src = _write(home / "workspace" / "legacy.html", "<p>legacy</p>")
        published = _publish(src, owner="password:alice@default")
        token = published["token"]

        # Rewrite the meta into the pre-fix shape.
        meta_path = artifacts._meta_path(token)
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("principal", None)
        meta.pop("profile", None)
        meta["owner"] = "d41d8cd98f00b204e9800998ecf8427e"  # an old session token
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        # Invisible to a stranger...
        assert artifacts.list_artifacts(owner="oidc:bob@default") == []
        # ...and not adoptable by acting on it.
        assert artifacts.revoke_artifact(token, owner="oidc:bob@default") is False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "principal" not in meta, "a stranger stamped themselves onto the record"
        assert meta["owner"] == "d41d8cd98f00b204e9800998ecf8427e"

        # The original publisher does not get it back either: the record simply
        # does not say who they were. That is the honest answer.
        assert artifacts.list_artifacts(owner="password:alice@default") == []

    def test_a_no_auth_deployment_still_sees_its_artifacts(self, home):
        """owner=None is the no-auth mode, where there is no principal to scope
        to and artifacts keep their historical shared behaviour."""
        src = _write(home / "workspace" / "shared.html", "<p>shared</p>")
        published = _publish(src, owner=None)
        listed = artifacts.list_artifacts(owner=None)
        assert [a["token"] for a in listed] == [published["token"]]


# ── Finding 3: public means redacted, and fails closed ───────────────────────


class TestPublicFailsClosed:
    def test_an_unredactable_format_cannot_be_published_publicly(self, home):
        """`.log`/`.yaml`/PDF/archives were copied verbatim and marked safe."""
        src = _write(home / "workspace" / "app.log", "token=sk-live-SECRET\n")
        with pytest.raises(artifacts.ArtifactPublicUnsafe):
            _publish(src, public=True)

    def test_a_binary_cannot_be_published_publicly_by_default(self, home):
        src = home / "workspace" / "report.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4\nsecret-bytes\n")
        with pytest.raises(artifacts.ArtifactPublicUnsafe):
            _publish(src, public=True)

    def test_an_explicit_verbatim_acknowledgement_is_required_and_recorded(self, home):
        src = _write(home / "workspace" / "app.log", "token=sk-live-SECRET\n")
        result = _publish(src, public=True, verbatim_public=True)
        meta = artifacts._load_meta(result["token"])
        ventry = meta["versions"][-1]
        assert ventry["public_safe"] is True
        assert ventry["verbatim_public"] is True
        assert ventry["redacted"] is False

    def test_a_redactable_format_is_redacted_and_marked_safe(self, home):
        src = _write(
            home / "workspace" / "r.html", "<p>api_key: sk-live-SECRET</p>"
        )
        result = _publish(src, public=True)
        meta, ventry, fpath = artifacts.resolve_artifact_file(result["token"])
        assert ventry["redacted"] is True
        assert ventry["public_safe"] is True
        assert "sk-live-SECRET" not in fpath.read_text(encoding="utf-8")

    def test_a_private_publish_of_an_unredactable_format_is_fine(self, home):
        src = _write(home / "workspace" / "app.log", "token=sk-live-SECRET\n")
        result = _publish(src)
        meta = artifacts._load_meta(result["token"])
        assert meta["versions"][-1]["public_safe"] is False


# ── Finding 4: revocation actually removes the content ───────────────────────


class TestRevocation:
    def test_revoke_deletes_the_stored_bytes_but_keeps_the_tombstone(self, home):
        src = _write(home / "workspace" / "bye.html", "<p>bye</p>")
        result = _publish(src)
        token = result["token"]
        vdir = artifacts._artifact_dir(token) / "v1"
        assert vdir.is_dir()

        assert artifacts.revoke_artifact(token, owner="local@default") is True
        assert not vdir.exists()
        assert artifacts._meta_path(token).is_file()
        assert artifacts.resolve_artifact_file(token) is None

    def test_gc_reclaims_revoked_storage(self, home):
        src = _write(home / "workspace" / "g.html", "<p>g</p>")
        token = _publish(src)["token"]
        # Simulate a pre-fix revoke that only timestamped the metadata.
        import json

        meta_path = artifacts._meta_path(token)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["revoked_at"] = time.time()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        assert (artifacts._artifact_dir(token) / "v1").is_dir()

        assert artifacts.gc_artifacts() >= 1
        assert not (artifacts._artifact_dir(token) / "v1").exists()


# ── Finding 5: no check/open race on the source ──────────────────────────────


class TestSourceRace:
    def test_a_swapped_source_between_validation_and_copy_is_detected(self, home, monkeypatch):
        """Validation stats a NAME; the copy must act on the same FILE."""
        src = _write(home / "workspace" / "race.html", "<p>original</p>")
        other = _write(home / "workspace" / "other.html", "<p>swapped</p>")
        source = artifacts.validate_source_path(str(src))
        good_stat = source.stat()
        # The name now points at a different inode.
        src.unlink()
        os.link(other, src)

        with pytest.raises(ValueError, match="changed while publishing"):
            artifacts._read_source_bytes(source, good_stat)

    def test_the_open_helper_refuses_a_symlink_name(self, home):
        """O_NOFOLLOW on the leaf, asserted directly on the helper.

        Scope note: in the real flow `validate_source_path()` has already
        resolved the link, so production never hands a symlink NAME to this
        helper. The mid-race component swap — the actual TOCTOU this protects
        against — is pinned by
        `test_a_swapped_source_between_validation_and_copy_is_detected`, which
        swaps the inode behind an already-validated name.
        """
        target = _write(home / "workspace" / "target.html", "<p>t</p>")
        link = home / "workspace" / "link.html"
        link.symlink_to(target)
        # ValueError, not OSError: the helper now walks from the allowed root
        # and converts every refusal along the way into the same user-facing
        # error type the rest of this module raises.
        with pytest.raises(ValueError):
            artifacts._open_source_checked(link, target.stat())

    def test_a_swapped_PARENT_between_validation_and_copy_is_refused(self, home):
        """O_NOFOLLOW on a full pathname protects only the LAST component.

        Containment was proven against a resolved string, and a string does not
        stay true: replacing a parent directory with a symlink after validation
        made the kernel resolve the read somewhere that was never checked. The
        open walks down from the allowed root now, refusing a symlink at every
        component, so the swap cannot redirect it.
        """
        real_dir = home / "workspace" / "sub"
        source = _write(real_dir / "doc.html", "<p>legitimate</p>")
        expected = source.stat()

        elsewhere = home / "workspace" / "elsewhere"
        _write(elsewhere / "doc.html", "<p>attacker content</p>")

        # Swap the PARENT for a symlink; the leaf name is untouched.
        import shutil

        shutil.rmtree(real_dir)
        real_dir.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(ValueError):
            artifacts._open_source_checked(source, expected)

    def test_a_file_replaced_between_prepare_and_publish_is_refused(self, home):
        """The capability authorizes BYTES, not a pathname.

        Under the previous signature a capability covered only the path, so
        whatever that name came to mean before publish was published under a
        signature issued for something else.
        """
        source = _write(home / "workspace" / "report.html", "<p>reviewed</p>")
        prepared = artifacts.prepare_source_capability(
            str(source), owner="local@default"
        )

        # Same path, different content — the swap a capability must not survive.
        source.write_text("<p>swapped in after approval</p>", encoding="utf-8")

        with pytest.raises(artifacts.ArtifactCapabilityError):
            artifacts.publish_artifact(
                str(source),
                owner="local@default",
                capability=prepared["capability"],
            )

    def test_an_unchanged_file_still_publishes(self, home):
        """The fingerprint check must not break the ordinary flow."""
        source = _write(home / "workspace" / "stable.html", "<p>unchanged</p>")
        prepared = artifacts.prepare_source_capability(
            str(source), owner="local@default"
        )
        result = artifacts.publish_artifact(
            str(source), owner="local@default", capability=prepared["capability"]
        )
        assert result["token"]


# ── Finding 6: storage and listing are bounded ───────────────────────────────


class TestBounds:
    def test_versions_are_capped_and_old_ones_removed(self, home, monkeypatch):
        monkeypatch.setattr(artifacts, "MAX_VERSIONS_PER_ARTIFACT", 3)
        src = _write(home / "workspace" / "v.html", "<p>1</p>")
        token = _publish(src)["token"]
        for i in range(2, 7):
            src.write_text(f"<p>{i}</p>", encoding="utf-8")
            _publish(src, token=token)
        meta = artifacts._load_meta(token)
        assert len(meta["versions"]) == 3
        assert [v["v"] for v in meta["versions"]] == [4, 5, 6]
        assert not (artifacts._artifact_dir(token) / "v1").exists()

    def test_artifact_count_is_capped_per_owner(self, home, monkeypatch):
        monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_OWNER", 2)
        for i in range(2):
            _publish(_write(home / "workspace" / f"a{i}.html", f"<p>{i}</p>"))
        with pytest.raises(artifacts.ArtifactQuotaExceeded, match="artifact limit"):
            _publish(_write(home / "workspace" / "a2.html", "<p>2</p>"))

    def test_aggregate_bytes_are_capped_per_owner(self, home, monkeypatch):
        monkeypatch.setattr(artifacts, "MAX_TOTAL_BYTES_PER_OWNER", 40)
        _publish(_write(home / "workspace" / "b0.html", "x" * 30))
        with pytest.raises(artifacts.ArtifactQuotaExceeded, match="storage limit"):
            _publish(_write(home / "workspace" / "b1.html", "y" * 30))

    def test_the_byte_cap_cannot_be_walked_past_by_re_publishing(self, home, monkeypatch):
        """A token's OWN retained versions must count toward its owner's cap.

        Excluding a re-published artifact wholesale meant each publish compared
        only the single incoming version against everything else, so the same
        token could grow to MAX_VERSIONS_PER_ARTIFACT x MAX_ARTIFACT_BYTES
        while every individual call passed the check.
        """
        monkeypatch.setattr(artifacts, "MAX_TOTAL_BYTES_PER_OWNER", 100)
        src = _write(home / "workspace" / "grow.html", "x" * 90)
        token = _publish(src)["token"]
        with pytest.raises(artifacts.ArtifactQuotaExceeded, match="storage limit"):
            for _ in range(5):
                src.write_text("y" * 90, encoding="utf-8")
                _publish(src, token=token)

    def test_quotas_are_per_owner_not_global(self, home, monkeypatch):
        monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_OWNER", 1)
        _publish(_write(home / "workspace" / "c0.html", "<p>0</p>"), owner="a@default")
        # A different principal still has their own budget.
        _publish(_write(home / "workspace" / "c1.html", "<p>1</p>"), owner="b@default")

    def test_the_list_is_paginated(self, home):
        for i in range(5):
            _publish(_write(home / "workspace" / f"p{i}.html", f"<p>{i}</p>"))
        page = artifacts.list_artifacts_page(owner="local@default", offset=0, limit=2)
        assert len(page["artifacts"]) == 2
        assert page["total"] == 5
        assert page["has_more"] is True
        last = artifacts.list_artifacts_page(owner="local@default", offset=4, limit=2)
        assert len(last["artifacts"]) == 1
        assert last["has_more"] is False

    def test_the_page_size_is_clamped(self, home):
        page = artifacts.list_artifacts_page(owner="local@default", limit=10_000)
        assert page["limit"] == artifacts.LIST_PAGE_SIZE_MAX


class TestDurablePublication:
    """A version is either entirely published or not published at all."""

    def test_a_failed_write_leaves_no_staging_directory(self, home, monkeypatch):
        """Writing straight to v<N>/ left bytes no record pointed at.

        The staged directory has to be cleaned up on every failure path, or the
        crash it protects against is replaced by a slow leak.
        """
        src = _write(home / "workspace" / "doc.html", "<p>hi</p>")

        def boom(*_a, **_kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(artifacts.os, "write", boom)
        # The disk error propagates as itself: the staging cleanup is a finally,
        # not a swallow, so the caller still learns the publish failed.
        with pytest.raises(OSError):
            _publish(src)

        art_root = artifacts.ARTIFACTS_DIR
        leftovers = list(art_root.glob("*/.staging-*")) if art_root.exists() else []
        assert leftovers == [], f"staging directory leaked: {leftovers}"

    def test_a_successful_publish_leaves_no_staging_directory(self, home):
        src = _write(home / "workspace" / "clean.html", "<p>clean</p>")
        published = _publish(src)
        art_dir = artifacts._artifact_dir(published["token"])
        assert (art_dir / "v1").is_dir()
        assert list(art_dir.glob(".staging-*")) == []

    def test_the_store_lock_is_shared_between_processes(self):
        """threading.Lock only orders callers inside one interpreter.

        Version numbers are allocated read-max-plus-one, so two processes over
        one store would hand out the same number and the second publication
        would overwrite the first.
        """
        import inspect

        src = inspect.getsource(artifacts._artifacts_lock)
        assert "flock" in src, "the store lock must be visible to other processes"
        # And it must not deadlock against itself when nested.
        with artifacts._artifacts_lock():
            with artifacts._artifacts_lock():
                pass
