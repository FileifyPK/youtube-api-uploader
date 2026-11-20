#!/usr/bin/env python3
"""Local TCP tunnel to Neon Postgres through the VM's egress proxy.

Why this exists: this VM's outbound TCP is intercepted — a direct
psycopg connection to Neon (port 5432) gets an HTTP response from the
egress gateway instead of a Postgres handshake ("received invalid
response to SSL negotiation"). The egress proxy DOES allow HTTP
CONNECT to the Neon hosts, so this forwarder relays local Postgres
traffic through a CONNECT tunnel. The TLS session stays end-to-end
(psycopg <-> Neon), so sslmode=require and channel_binding=require
keep working; the proxy only forwards bytes after CONNECT.

Usage (standalone):
    python scripts/neon_tunnel.py --listen-port 15433 \
        --target-host ep-xxx-pooler.c-9.us-east-2.aws.neon.tech

    # then connect psycopg to 127.0.0.1:15433
    # (DATABASE_URL host becomes 127.0.0.1:15433)

As a module (setup_neon.py uses this):
    from scripts.neon_tunnel import ensure_tunnel
    ensure_tunnel(listen_port=15433, target_host="ep-xxx-pooler...")

The proxy URL comes from HTTPS_PROXY / https_proxy in the
environment (sent as Proxy-Authorization when it carries
credentials). The proxy URL is NEVER printed or logged.

Persistence: the pooler tunnel runs as a user systemd service
(see scripts/neon-tunnel-pooler.service, installed to
~/.config/systemd/user/ with its proxy env in
~/.config/neon-tunnel/proxy.env, mode 600). The direct-host tunnel
is only needed for migrations and is started on demand by
setup_neon.py.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import socket
import sys
import threading
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Neon Postgres port.
TARGET_PORT = 5432


def _proxy_info(proxy_url: str | None) -> tuple[str, int, str | None]:
    """Parse the proxy URL into (host, port, basic-auth header).

    Returns the Proxy-Authorization header value (or None). The
    credentials are never logged.
    """
    url = proxy_url or os.environ.get("HTTPS_PROXY") or os.environ.get(
        "https_proxy"
    )
    if not url:
        raise ValueError(
            "No proxy configured: set HTTPS_PROXY in the environment"
        )
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        raise ValueError("Proxy URL has no host/port")
    auth = None
    if parsed.username:
        token = base64.b64encode(
            f"{parsed.username}:{parsed.password or ''}".encode()
        ).decode()
        auth = f"Basic {token}"
    return parsed.hostname, parsed.port, auth


def _connect_via_proxy(
    target_host: str, target_port: int, proxy_url: str | None = None
) -> socket.socket:
    """Open a CONNECT tunnel to the target through the proxy."""
    proxy_host, proxy_port, auth = _proxy_info(proxy_url)
    sock = socket.create_connection((proxy_host, proxy_port), timeout=30)
    target = f"{target_host}:{target_port}"
    lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
        "Proxy-Connection: keep-alive",
    ]
    if auth:
        lines.append(f"Proxy-Authorization: {auth}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError(
                "Proxy closed the connection during CONNECT"
            )
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200 " not in status_line:
        sock.close()
        raise ConnectionError(
            f"Proxy refused CONNECT to {target}: {status_line}"
        )
    return sock


def _relay(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src -> dst until EOF or error, then close both."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            src.close()
        except OSError:
            pass
        try:
            dst.close()
        except OSError:
            pass


def _handle_client(
    client: socket.socket,
    target_host: str,
    target_port: int,
    proxy_url: str | None,
) -> None:
    try:
        upstream = _connect_via_proxy(target_host, target_port, proxy_url)
    except Exception as exc:
        log.warning("CONNECT to %s failed: %s", target_host, exc)
        try:
            client.close()
        except OSError:
            pass
        return
    t1 = threading.Thread(
        target=_relay, args=(client, upstream), daemon=True
    )
    t2 = threading.Thread(
        target=_relay, args=(upstream, client), daemon=True
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def serve(
    listen_port: int,
    target_host: str,
    target_port: int = TARGET_PORT,
    proxy_url: str | None = None,
) -> None:
    """Listen on 127.0.0.1:listen_port and forward to the target."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", listen_port))
    server.listen(16)
    log.info(
        "Tunnel listening on 127.0.0.1:%d -> %s:%d",
        listen_port,
        target_host,
        target_port,
    )
    while True:
        client, _addr = server.accept()
        threading.Thread(
            target=_handle_client,
            args=(client, target_host, target_port, proxy_url),
            daemon=True,
        ).start()


def ensure_tunnel(
    listen_port: int,
    target_host: str,
    target_port: int = TARGET_PORT,
    proxy_url: str | None = None,
) -> bool:
    """Start a tunnel thread, unless something already listens on the
    port (e.g. the persistent systemd service). Returns True if this
    call started the tunnel, False if the port was already in use."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(("127.0.0.1", listen_port))
        probe.close()
        log.info(
            "Port %d already in use — reusing the existing tunnel",
            listen_port,
        )
        return False
    except OSError:
        pass
    finally:
        probe.close()
    thread = threading.Thread(
        target=serve,
        args=(listen_port, target_host, target_port, proxy_url),
        daemon=True,
    )
    thread.start()
    log.info(
        "Started in-process tunnel 127.0.0.1:%d -> %s:%d",
        listen_port,
        target_host,
        target_port,
    )
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Tunnel local Postgres traffic to Neon via HTTP CONNECT."
    )
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, default=TARGET_PORT)
    parser.add_argument("--proxy", default=None)
    args = parser.parse_args()
    try:
        serve(
            args.listen_port,
            args.target_host,
            args.target_port,
            args.proxy,
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
