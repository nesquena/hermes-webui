"""Test-mode network isolation shared by server subprocesses."""

import os
import re
import socket


def install_test_network_block() -> None:
    if os.environ.get("HERMES_WEBUI_TEST_NETWORK_BLOCK", "").strip() not in ("1", "true", "yes"):
        return

    real_create_connection = socket.create_connection
    real_socket_connect = socket.socket.connect

    def addr_is_local(host):
        if not isinstance(host, str):
            return False
        host = host.strip().lower()
        if not host:
            return False
        if host in ("::1", "0:0:0:0:0:0:0:1") or host.startswith("fe80:") or re.match(r"^f[cd][0-9a-f]{0,2}:", host):
            return True
        if host == "localhost" or host.endswith((".localhost", ".local", ".test", ".invalid", ".example")):
            return True
        if any(host == domain or host.endswith("." + domain) for domain in ("example.com", "example.net", "example.org")):
            return True
        if host and host[0].isdigit() and host.count(".") == 3:
            try:
                o1, o2, o3, _o4 = [int(part) for part in host.split(".")]
            except ValueError:
                return False
            return (
                o1 == 127 or o1 == 10 or (o1 == 192 and o2 == 168)
                or (o1 == 172 and 16 <= o2 <= 31) or (o1 == 169 and o2 == 254)
                or (o1 == 203 and o2 == 0 and o3 == 113)
            )
        return False

    def blocked_create_connection(address, *args, **kwargs):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if addr_is_local(host):
            return real_create_connection(address, *args, **kwargs)
        raise OSError(f"hermes test network isolation: outbound to {address!r} blocked")

    def blocked_socket_connect(sock, address):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if addr_is_local(host):
            return real_socket_connect(sock, address)
        raise OSError(f"hermes test network isolation: socket.connect to {address!r} blocked")

    socket.create_connection = blocked_create_connection
    socket.socket.connect = blocked_socket_connect
