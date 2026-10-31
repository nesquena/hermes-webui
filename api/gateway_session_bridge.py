"""Optional Linux, same-UID socket adapter for separately containerized WebUI.

Run beside the Gateway in its network namespace. The existing Gateway issues
and consumes native tickets; this adapter only forwards canonical session frames.
Its private socket grants operator authority to the explicitly allowed profiles.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct

from api.gateway_sessions import SessionTransportError, connect_local_socket

PROTOCOL = "hermes-webui-session-bridge-v1"
MAX_SIZE = 8 * 1024 * 1024


def private_path(path, *, directory=False):
    """Reject symlinks, foreign owners and group/world access before connecting."""
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise SessionTransportError("Gateway session socket must use a canonical absolute path.")
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISSOCK
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SessionTransportError("Gateway session socket permissions are unsafe.")
    return info


def check_peer(ws):
    """Kernel credentials close the socket-path check/connect race."""
    peer = ws.transport.get_extra_info("socket")
    if peer is None or not hasattr(socket, "SO_PEERCRED"):
        raise SessionTransportError("Gateway session bridge requires Linux peer credentials.")
    _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise SessionTransportError("Gateway session bridge peer identity does not match.")


def profile_home(root, profile, allowed):
    if profile not in allowed or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise SessionTransportError("Gateway session profile is not allowed.")
    root = Path(root).resolve(strict=True)
    home = root if profile == "default" else root / "profiles" / profile
    if home.resolve(strict=True) != home or not home.is_dir():
        raise SessionTransportError("Gateway session profile directory is unsafe.")
    return home


@asynccontextmanager
async def connect_bridge(path, profile):
    from hermes_cli.gateway_client import GatewayClient
    from websockets.asyncio.client import unix_connect

    private_path(Path(path).parent, directory=True)
    private_path(path)
    async with unix_connect(path, subprotocols=[PROTOCOL], open_timeout=10, max_size=MAX_SIZE) as ws:
        check_peer(ws)
        if ws.subprotocol != PROTOCOL:
            raise SessionTransportError("Gateway session bridge protocol mismatch.")
        await ws.send(json.dumps({"profile": profile}))
        response = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if response != {"ready": True, "profile": profile}:
            raise SessionTransportError("Gateway session bridge could not connect to this profile.")
        async with GatewayClient(ws) as client:
            yield client


async def forward(source, target):
    async for frame in source:
        await target.send(frame)


async def handle(ws, *, root, profiles):
    tasks = []
    try:
        check_peer(ws)
        raw = await asyncio.wait_for(ws.recv(), 10)
        if not isinstance(raw, str) or len(raw) > 1024:
            raise SessionTransportError("Invalid bridge handshake.")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"profile"} or not isinstance(request["profile"], str):
            raise SessionTransportError("Invalid bridge handshake.")
        profile = request["profile"]
        home = profile_home(root, profile, profiles)
        async with connect_local_socket(home) as upstream:
            await ws.send(json.dumps({"ready": True, "profile": profile}))
            tasks = [asyncio.create_task(forward(ws, upstream)), asyncio.create_task(forward(upstream, ws))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        # No endpoint, ticket, auth material, traceback or remote error reaches
        # the client or server logger. Pending admissions remain Gateway-owned.
        await ws.close(code=1011, reason="Gateway session bridge unavailable")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(path, root, profiles, *, stop=None):
    from websockets.asyncio.server import unix_serve

    private_path(Path(path).parent, directory=True)
    for profile in profiles:
        profile_home(root, profile, profiles)
    # Never unlink an existing listener (or a stale socket) automatically.
    if os.path.lexists(path):
        raise SessionTransportError("Gateway session socket already exists.")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        listener.bind(path)
        os.chmod(path, 0o600)
        identity = private_path(path)
        listener.listen(128)
        listener.setblocking(False)
        async def accept(ws):
            await handle(ws, root=root, profiles=profiles)
        async with unix_serve(accept, sock=listener, subprotocols=[PROTOCOL],
                              origins=[None], max_size=MAX_SIZE, open_timeout=10):
            if stop is None:
                await asyncio.Future()
            else:
                await stop.wait()
    finally:
        listener.close()
        if identity is not None:
            try:
                current = Path(path).lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    Path(path).unlink()
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--profile", action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        try:
            await serve(args.socket, args.home, frozenset(args.profile), stop=stop)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        parser.exit(1, "Gateway session bridge could not start; check private paths and profile configuration.\n")


if __name__ == "__main__":
    main()
