"""``fetch_url`` tool: SSRF-hardened page fetch.

Every hop (the first request and each redirect) is validated, then the connection is made to
the *vetted IP address* rather than the hostname, so a second DNS lookup can never return a
different (private) address (DNS rebinding). TLS still validates the certificate against the
real hostname through the ``sni_hostname`` extension, and the ``Host`` header is set explicitly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable

import httpx

from aichat.tools import htmltext
from aichat.tools.registry import ToolResult

MAX_REDIRECTS = 3
MAX_BYTES = 1_000_000
TIMEOUT_S = 10.0
ALLOWED_TYPES = frozenset({"text/html", "text/plain", "application/json"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "Mozilla/5.0 (compatible; AIChat/0.1; +local desktop assistant)"
_ACCEPT = "text/html,text/plain;q=0.9,application/json;q=0.9,*/*;q=0.1"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.IGNORECASE)


class FetchError(Exception):
    """A fetch failed; the message is safe to show to the model."""


class BlockedAddress(FetchError):
    """The host resolves to an address that must not be contacted."""


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in seen:
            seen.append(ip)
    return seen


# Replaceable in tests: ``async (host, port) -> list[str]``.
_resolver: Callable[[str, int], Awaitable[list[str]]] = _default_resolver


def vet_ip(address: str) -> str:
    """Return the normalised address if it is publicly routable, else raise BlockedAddress."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        raise BlockedAddress("Blocked: host did not resolve to a valid address.") from None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            raise BlockedAddress("Blocked: IPv4-mapped IPv6 addresses are not allowed.")
        if ip in _NAT64 or ip.sixtofour is not None or ip.teredo is not None:
            raise BlockedAddress("Blocked: transition-mechanism IPv6 addresses are not allowed.")
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        raise BlockedAddress("Blocked: that address is private or local and cannot be fetched.")
    return str(ip)


async def _vetted_ips(host: str, port: int, block_private: bool) -> list[str]:
    try:
        ipaddress.ip_address(host.strip("[]"))
        raw = [host.strip("[]")]  # an IP literal needs no DNS lookup
    except ValueError:
        try:
            raw = await _resolver(host, port)
        except OSError:
            raise FetchError(f"Could not resolve host '{host}'.") from None
    if not raw:
        raise FetchError(f"Could not resolve host '{host}'.")
    if not block_private:
        return list(raw)
    # Every resolved address must be public; one private answer poisons the host.
    return [vet_ip(a) for a in raw]


def _decode(body: bytes, content_type: str, header_charset: str | None) -> str:
    charset = header_charset
    if charset is None and content_type == "text/html":
        m = _META_CHARSET.search(body[:4096])
        charset = m.group(1).decode("ascii", "ignore") if m else None
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


async def _fetch(
    url: str,
    *,
    max_chars: int,
    max_bytes: int,
    timeout_s: float,
    block_private: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> ToolResult:
    try:
        current = httpx.URL(url.strip())
    except httpx.InvalidURL:
        raise FetchError("Invalid URL.") from None

    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=False,
        trust_env=False,  # no proxy env vars: a proxy would bypass the IP pinning
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            response, body = await _one_hop(client, current, max_bytes, block_private)
            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"HTTP {response.status_code} redirect without a Location.")
                if hop == MAX_REDIRECTS:
                    raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS}).")
                try:
                    current = current.join(location)
                except httpx.InvalidURL:
                    raise FetchError("Redirect to an invalid URL.") from None
                continue
            break

    if response.status_code >= 400:
        raise FetchError(f"HTTP {response.status_code} error fetching the page.")
    ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype not in ALLOWED_TYPES:
        raise FetchError(f"Unsupported content type '{ctype or 'unknown'}'; only text/HTML/JSON.")
    text = _decode(body, ctype, response.charset_encoding)
    title = ""
    if ctype == "text/html":
        title, text = htmltext.extract(text)
    else:
        text = htmltext.collapse_text(text)
    parts = []
    if title:
        parts.append(f"Title: {title}")
    parts.append(f"URL: {current}")
    header = "\n".join(parts) + "\n\n"
    if not text:
        text = "(no readable text)"
    content = header + text
    if len(content) > max_chars:
        content = _clip(content, max_chars)
    return ToolResult(True, content, f"Fetched {current.host} ({len(body) // 1024 or 1} KB)")


def _host_header(host: str, port: int | None) -> str:
    name = f"[{host}]" if ":" in host else host
    return f"{name}:{port}" if port else name


async def _one_hop(
    client: httpx.AsyncClient, url: httpx.URL, max_bytes: int, block_private: bool
) -> tuple[httpx.Response, bytes]:
    scheme = url.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedAddress("Only http and https URLs can be fetched.")
    host = url.raw_host.decode("ascii")  # IDNA/punycode form
    if not host:
        raise FetchError("Invalid URL: missing host.")
    if url.userinfo:
        raise BlockedAddress("URLs with embedded credentials are not allowed.")
    port = url.port or (443 if scheme == "https" else 80)
    ips = await _vetted_ips(host, port, block_private)

    last_error: Exception | None = None
    for ip in ips[:3]:
        target = url.copy_with(host=ip)
        request = client.build_request(
            "GET",
            target,
            headers={
                "Host": _host_header(host, url.port),
                "User-Agent": _USER_AGENT,
                "Accept": _ACCEPT,
            },
            extensions={"sni_hostname": host},
        )
        try:
            response = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
            continue
        try:
            if response.status_code in _REDIRECT_CODES:
                return response, b""
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            return response, b"".join(chunks)[:max_bytes]
        finally:
            await response.aclose()
    raise FetchError(f"Could not connect to '{host}'.") from last_error


async def fetch(
    url: str,
    *,
    max_chars: int = 1500,
    max_bytes: int = MAX_BYTES,
    timeout_s: float = TIMEOUT_S,
    block_private: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    """Fetch ``url`` and return title, URL and extracted text (never raises for fetch errors)."""
    if not isinstance(url, str) or not url.strip():
        return ToolResult(False, "Fetch error: url is required.", "fetch error")
    try:
        async with asyncio.timeout(timeout_s):
            return await _fetch(
                url,
                max_chars=max_chars,
                max_bytes=max_bytes,
                timeout_s=timeout_s,
                block_private=block_private,
                transport=transport,
            )
    except FetchError as exc:
        return ToolResult(
            False,
            f"Fetch error: {exc}",
            "fetch blocked" if isinstance(exc, BlockedAddress) else "fetch failed",
        )
    except TimeoutError:
        return ToolResult(False, "Fetch error: the request timed out.", "fetch timed out")
    except httpx.TimeoutException:
        return ToolResult(False, "Fetch error: the request timed out.", "fetch timed out")
    except httpx.HTTPError as exc:
        return ToolResult(False, f"Fetch error: {type(exc).__name__}.", "fetch failed")
