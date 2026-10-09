"""Request-boundary URL validation for public-web scraping.

This is intentionally an initial-target guard. Scraper transports must still treat
redirects and browser subresources as untrusted when stronger isolation is needed.
"""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    """Raised when a requested URL is not an allowed public HTTP(S) target."""


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _max_url_length() -> int:
    try:
        return max(256, min(int(os.getenv("YADA_MAX_URL_LENGTH", "4096")), 32768))
    except ValueError:
        return 4096


# only 80/443 by default; unusual ports are usually probes at internal
# services (3306, 6379, 22...)
def _allowed_ports() -> set[int]:
    raw = os.getenv("YADA_ALLOWED_URL_PORTS", "80,443")
    ports = set()
    for value in raw.split(","):
        try:
            port = int(value.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports or {80, 443}


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


async def _resolve_addresses(host: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=5.0,
        )
    except asyncio.TimeoutError as exc:
        raise UnsafeUrlError("Target hostname resolution timed out") from exc
    except (OSError, ValueError) as exc:
        raise UnsafeUrlError("Target hostname could not be resolved") from exc
    return {record[4][0] for record in records}


async def normalize_public_url(raw_url: str) -> str:
    """Normalize and validate one initial public HTTP(S) target."""
    if not isinstance(raw_url, str):
        raise UnsafeUrlError("URL must be a string")
    candidate = raw_url.strip()
    if not candidate:
        raise UnsafeUrlError("URL must not be empty")
    if len(candidate) > _max_url_length():
        raise UnsafeUrlError(f"URL exceeds the {_max_url_length()} character limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise UnsafeUrlError("URL contains control characters")
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL contains an invalid host or port") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL user information is not allowed")

    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or len(host) > 253:
        raise UnsafeUrlError("URL must contain a valid hostname")
    allow_private = _truthy_env("YADA_ALLOW_PRIVATE_URLS")
    if not allow_private and (
        host == "localhost" or host.endswith((".localhost", ".local"))
    ):
        raise UnsafeUrlError("Local hostnames are not allowed")

    effective_port = (443 if scheme == "https" else 80) if port is None else port
    if effective_port not in _allowed_ports():
        raise UnsafeUrlError("Target port is not allowed")

    if allow_private:
        return urlunsplit((scheme, parsed.netloc, parsed.path or "", parsed.query, parsed.fragment))

    # resolve first, then check every record: a public name that resolves to
    # any internal IP is how DNS-rebinding gets past hostname checks
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        addresses = await _resolve_addresses(host, effective_port)
        if not addresses:
            raise UnsafeUrlError("Target hostname did not resolve")
        if any(not _public_address(address) for address in addresses):
            raise UnsafeUrlError("Target hostname resolves to a non-public address")
    else:
        if not _public_address(str(literal)):
            raise UnsafeUrlError("Non-public IP addresses are not allowed")

    return urlunsplit((scheme, parsed.netloc, parsed.path or "", parsed.query, parsed.fragment))
