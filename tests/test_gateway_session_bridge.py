"""Container transport authority and recovery boundaries."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import socket
from types import SimpleNamespace

import pytest

from api import gateway_session_bridge as bridge, gateway_sessions as sessions


@pytest.mark.parametrize("profile", ["default", "work", "family", "lab"])
def test_explicit_profile_mapping(tmp_path, profile):
    home = tmp_path if profile == "default" else tmp_path / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    assert bridge.profile_home(tmp_path, profile, {profile}) == home.resolve()


@pytest.mark.parametrize("profile", ["../other", "work/../family", "/tmp", "", "unlisted"])
def test_profile_escape_or_unlisted_refused(tmp_path, profile):
    with pytest.raises(sessions.SessionTransportError):
        bridge.profile_home(tmp_path, profile, {"default", "work", "family", "lab"})


def test_unix_is_explicit_opt_in():
    assert sessions.configured({"source_tag": "desktop"}, {"webui_gateway_session_transport": "unix"})
    assert not sessions.configured({"source_tag": "cron"}, {"webui_gateway_session_transport": "unix"})


def test_recovery_refuses_different_bridge_before_connect(monkeypatch, tmp_path):
    from api import profiles
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _: tmp_path)
    monkeypatch.setattr(sessions, "config_for_session", lambda _: {
        "webui_gateway_session_transport": "unix", "webui_gateway_session_socket": str(tmp_path / "new.sock")})
    with pytest.raises(sessions.SessionTransportError, match="connection changed"):
        sessions.run_session(SimpleNamespace(profile="work"), "s", "never send", emit=None, cancel=None,
                             record=None, saved={"profile_home": str(tmp_path.resolve()),
                                                 "session_connection": {"mode": "local"}})


@pytest.mark.parametrize("handshake", ["not json", "[]", '{"profile":"not-allowed"}',
                                    '{"profile":"default","home":"/other"}', '"' + 'x' * 1024 + '"'])
def test_bad_handshake_never_bootstraps(monkeypatch, tmp_path, handshake):
    monkeypatch.setattr(bridge, "check_peer", lambda _: None)
    class Peer:
        closed = None
        async def recv(self): return handshake
        async def close(self, **kwargs): self.closed = kwargs
    peer = Peer()
    asyncio.run(bridge.handle(peer, root=tmp_path, profiles={"default"}))
    assert peer.closed == {"code": 1011, "reason": "Gateway session bridge unavailable"}


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credential integration")
def test_private_unix_roundtrip_and_cleanup(tmp_path, monkeypatch, allow_outbound_network):
    from websockets.asyncio.client import unix_connect
    tmp_path.chmod(0o700)
    path = tmp_path / "session.sock"
    # The suite's IP-only guard rejects Unix paths. Permit only this disposable
    # socket for the roundtrip; keep every TCP and other Unix connection blocked.
    real_connect = socket.socket.connect
    def connect_fixture(peer, address):
        if peer.family != socket.AF_UNIX or address != str(path):
            raise OSError("Only the test's private Unix socket may be connected")
        return real_connect(peer, address)
    monkeypatch.setattr(socket.socket, "connect", connect_fixture)
    selected = []
    @asynccontextmanager
    async def upstream(home):
        selected.append(home)
        queue = asyncio.Queue()
        class Echo:
            async def send(self, frame): await queue.put(frame)
            def __aiter__(self): return self
            async def __anext__(self): return await queue.get()
        yield Echo()
    monkeypatch.setattr(bridge, "connect_local_socket", upstream)
    async def exercise():
        server = asyncio.create_task(bridge.serve(str(path), tmp_path, {"default"}))
        try:
            for _ in range(100):
                if path.exists(): break
                await asyncio.sleep(.01)
            bridge.private_path(path)
            async with unix_connect(str(path), subprotocols=[bridge.PROTOCOL]) as ws:
                bridge.check_peer(ws)
                await ws.send(json.dumps({"profile": "default"}))
                assert json.loads(await ws.recv()) == {"ready": True, "profile": "default"}
                await ws.send('{"method":"session.resume","id":1}')
                assert await ws.recv() == '{"method":"session.resume","id":1}'
        finally:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
    asyncio.run(exercise())
    assert selected == [tmp_path]
    assert not path.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX socket permissions")
def test_unsafe_directory_and_symlink_refused(tmp_path):
    tmp_path.chmod(0o777)
    with pytest.raises(sessions.SessionTransportError, match="permissions"):
        bridge.private_path(tmp_path, directory=True)
    tmp_path.chmod(0o700)
    link = tmp_path / "alias"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(sessions.SessionTransportError, match="canonical"):
        bridge.private_path(link, directory=True)

