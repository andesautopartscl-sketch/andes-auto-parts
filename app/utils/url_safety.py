"""Validaciones de URLs externas (anti-SSRF)."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def is_safe_external_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return False
    if hostname.lower() in {"localhost"}:
        return False

    try:
        addr = ipaddress.ip_address(hostname)
        return not (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or str(addr) == "0.0.0.0"
        )
    except ValueError:
        pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or str(addr) == "0.0.0.0"
        ):
            return False
    return True
