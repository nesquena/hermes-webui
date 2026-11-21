"""Filesystem regressions for Web Push key creation and explicit rotation."""
import json
import os
import stat

import pytest

from scripts import generate_vapid_keys as generator


def _args(state, *, force=False):
    return ["--state-dir", str(state), "--subject", "mailto:test@example.com"] + (["--force"] if force else [])


def test_dangling_symlink_is_not_followed_without_force(tmp_path, capsys):
    victim = tmp_path / "unrelated.json"
    target = tmp_path / "webui_vapid.json"
    target.symlink_to(victim)
    assert generator.main(_args(tmp_path)) == 1
    assert target.is_symlink()
    assert not victim.exists()
    assert "use --force" in capsys.readouterr().err


def test_concurrent_creator_is_not_overwritten(tmp_path, monkeypatch, capsys):
    target = tmp_path / "webui_vapid.json"
    real_open = os.open
    raced = []

    def competing_open(path, flags, mode=0o777, **kwargs):
        if os.fspath(path) == str(target) and not raced:
            raced.append(True)
            target.write_text("concurrently created keys")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(generator.os, "open", competing_open)
    assert generator.main(_args(tmp_path)) == 1
    assert raced
    assert target.read_text() == "concurrently created keys"
    assert "use --force" in capsys.readouterr().err


@pytest.mark.parametrize("symlink", [False, True])
def test_force_atomically_replaces_destination_without_following_it(tmp_path, monkeypatch, capsys, symlink):
    target = tmp_path / "webui_vapid.json"
    victim = tmp_path / "unrelated.json"
    victim.write_text("unrelated data")
    if symlink:
        target.symlink_to(victim)
    else:
        target.write_text("old keys")
        target.chmod(0o644)
    old = target.read_text()
    dump = json.dump

    def observe_write(payload, fh, **kwargs):
        # Until the new file is completely written, readers see the old one.
        assert target.read_text() == old
        assert victim.read_text() == "unrelated data"
        return dump(payload, fh, **kwargs)

    monkeypatch.setattr(generator.json, "dump", observe_write)
    assert generator.main(_args(tmp_path, force=True)) == 0
    assert not target.is_symlink()
    assert victim.read_text() == "unrelated data"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    data = json.loads(target.read_text())
    assert data["private_key"] not in capsys.readouterr().out
    assert set(tmp_path.iterdir()) == {victim, target}


def test_failed_force_write_preserves_old_keys_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / "webui_vapid.json"
    target.write_text("old keys")

    def failed_dump(payload, fh, **kwargs):
        fh.write("partial new keys")
        raise OSError("injected write failure")

    monkeypatch.setattr(generator.json, "dump", failed_dump)
    with pytest.raises(OSError, match="injected"):
        generator.main(_args(tmp_path, force=True))
    assert target.read_text() == "old keys"
    assert set(tmp_path.iterdir()) == {target}
