"""Public acquisition URLs. This is defense in depth, not an egress firewall."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "Acquisition requires an HTTP(S) URL without embedded credentials"
        )
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("Acquisition hostname could not be resolved") from exc
    ips = [ipaddress.ip_address(address[4][0]) for address in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise ValueError("Acquisition URL resolves to a non-public address")
    return url


class PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_urlopen(request: Request, *, timeout: float = 30):
    validate_public_url(request.full_url)
    return build_opener(PublicRedirectHandler()).open(request, timeout=timeout)
