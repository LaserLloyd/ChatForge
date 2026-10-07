"""fetch_url: SSRF blocks, DNS-rebinding pinning, redirects, size cap, content types.

No real network: ``httpx.MockTransport`` plus a monkeypatched resolver.
"""

import gzip

import httpx
import pytest

from chatforge.tools import fetch_url
from chatforge.tools.fetch_url import BlockedAddress, fetch, vet_ip

PUBLIC = "93.184.216.34"
PUBLIC2 = "151.101.1.69"

HTML = (
    b"<html><head><title>Example Title</title><script>x=1</script></head>"
    b"<body><nav>menu</nav><p>Hello   world</p><footer>foot</footer></body></html>"
)


class Recorder:
    """A resolver table plus a MockTransport that records every request."""

    def __init__(self, hosts=None):
        self.hosts = hosts or {}
        self.resolved = []
        self.requests = []
        self.handler = self.default_handler

    async def resolver(self, host, port):
        self.resolved.append((host, port))
        answer = self.hosts.get(host)
        if answer is None:
            raise OSError("no such host")
        if callable(answer):
            return answer(len(self.resolved))
        return list(answer)

    def default_handler(self, request):
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    @property
    def transport(self):
        def handler(request):
            self.requests.append(request)
            return self.handler(request)

        return httpx.MockTransport(handler)


@pytest.fixture
def net(monkeypatch):
    rec = Recorder({"example.com": [PUBLIC], "other.org": [PUBLIC2]})
    monkeypatch.setattr(fetch_url, "_resolver", rec.resolver)
    return rec


async def do_fetch(rec, url, **kw):
    kw.setdefault("max_chars", 5000)
    return await fetch(url, transport=rec.transport, **kw)


# --- refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18611",
        "http://127.0.0.1:18611/v3/models",
        "http://localhost:18611",
        "file:///C:/Windows/win.ini",
        "file://localhost/etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:127.0.0.1]",
        "http://[::ffff:7f00:1]",
        "http://[::ffff:8.8.8.8]",
        "http://0.0.0.0",
        "http://0.0.0.0:80/",
        "http://[::]",
        "http://[::1]",
        "http://10.0.0.5",
        "http://192.168.1.1",
        "http://172.16.0.1",
        "http://100.64.0.1",
        "http://224.0.0.1",
        "http://240.0.0.1",
        "http://[fe80::1]",
        "http://[fc00::1]",
        "http://[64:ff9b::7f00:1]",
        "http://user:pw@example.com/",
        "javascript:alert(1)",
        "not a url",
        "",
        "http://",
    ],
)
async def test_refused_without_any_request(net, monkeypatch, url):
    net.hosts["localhost"] = ["127.0.0.1", "::1"]
    result = await do_fetch(net, url)
    assert not result.ok
    assert net.requests == []


async def test_blocked_messages_are_explanatory(net):
    r = await do_fetch(net, "http://127.0.0.1:18611")
    assert "Blocked" in r.content
    r = await do_fetch(net, "file:///x")
    assert "http" in r.content


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.1.2.3", "169.254.1.1", "::1", "0.0.0.0", "::ffff:1.2.3.4"]
)
def test_vet_ip_blocks(ip):
    with pytest.raises(BlockedAddress):
        vet_ip(ip)


@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_vet_ip_allows_public(ip):
    assert vet_ip(ip) == ip


async def test_hostname_resolving_to_private_is_blocked(net):
    net.hosts["evil.test"] = ["127.0.0.1"]
    r = await do_fetch(net, "http://evil.test/")
    assert not r.ok
    assert net.requests == []


async def test_any_private_answer_poisons_the_host(net):
    net.hosts["mixed.test"] = [PUBLIC, "10.0.0.1"]
    r = await do_fetch(net, "http://mixed.test/")
    assert not r.ok
    assert net.requests == []


async def test_unresolvable_host(net):
    r = await do_fetch(net, "http://nope.invalid/")
    assert not r.ok
    assert "resolve" in r.content


# --- success + rebinding ------------------------------------------------------------------


async def test_success_extracts_title_url_text(net):
    r = await do_fetch(net, "https://example.com/page?q=1")
    assert r.ok
    assert r.content == "Title: Example Title\nURL: https://example.com/page?q=1\n\nHello world"
    assert "menu" not in r.content
    assert "example.com" in r.summary


async def test_html_extraction_runs_off_the_event_loop_thread(net, monkeypatch):
    import threading

    seen = []
    real = fetch_url.htmltext.extract

    def spy(text):
        seen.append(threading.current_thread())
        return real(text)

    monkeypatch.setattr(fetch_url.htmltext, "extract", spy)
    r = await do_fetch(net, "https://example.com/")
    assert r.ok
    assert seen
    assert seen[0] is not threading.main_thread()


async def test_connects_to_vetted_ip_with_host_header_and_sni(net):
    await do_fetch(net, "https://example.com:8443/a/b?x=1")
    (req,) = net.requests
    assert req.url.host == PUBLIC  # pinned: we never connect to the name
    assert req.url.port == 8443
    assert req.url.path == "/a/b"
    assert req.headers["host"] == "example.com:8443"
    assert req.extensions["sni_hostname"] == "example.com"
    assert net.resolved == [("example.com", 8443)]


async def test_default_port_host_header_has_no_port(net):
    await do_fetch(net, "http://example.com/")
    assert net.requests[0].headers["host"] == "example.com"
    assert net.resolved == [("example.com", 80)]


async def test_dns_rebinding_first_public_then_private(net):
    """First lookup is public, a second would be private: we must resolve once and pin."""
    net.hosts["rebind.test"] = lambda n: [PUBLIC] if n == 1 else ["127.0.0.1"]
    r = await do_fetch(net, "http://rebind.test/")
    assert r.ok
    assert len(net.resolved) == 1  # no second lookup for the same hop
    assert [q.url.host for q in net.requests] == [PUBLIC]


async def test_rebinding_private_on_redirect_hop_is_blocked(net):
    """Each redirect hop is resolved and vetted afresh."""
    net.hosts["rebind.test"] = lambda n: [PUBLIC] if n == 1 else ["127.0.0.1"]

    def handler(request):
        return httpx.Response(302, headers={"location": "http://rebind.test/next"})

    net.handler = handler
    r = await do_fetch(net, "http://rebind.test/")
    assert not r.ok
    assert "Blocked" in r.content
    assert len(net.requests) == 1  # the private hop never got a request


async def test_ipv6_public_literal_is_pinned(net):
    await do_fetch(net, "http://[2606:4700:4700::1111]/")
    assert net.requests[0].url.host == "2606:4700:4700::1111"
    assert net.requests[0].headers["host"] == "[2606:4700:4700::1111]"


async def test_connect_error_falls_back_to_next_vetted_ip(net):
    net.hosts["multi.test"] = [PUBLIC, PUBLIC2]

    def handler(request):
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    net.handler = handler
    r = await do_fetch(net, "http://multi.test/")
    assert r.ok
    assert [q.url.host for q in net.requests] == [PUBLIC, PUBLIC2]


async def test_all_connects_fail(net):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    net.handler = handler
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok
    assert "connect" in r.content.lower()


# --- redirects ----------------------------------------------------------------------------


async def test_follows_redirects_and_reports_final_url(net):
    def handler(request):
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://other.org/landing"})
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    net.handler = handler
    r = await do_fetch(net, "http://example.com/")
    assert r.ok
    assert "URL: https://other.org/landing" in r.content
    assert [q.url.host for q in net.requests] == [PUBLIC, PUBLIC2]
    assert net.requests[1].headers["host"] == "other.org"
    assert net.requests[1].extensions["sni_hostname"] == "other.org"


async def test_relative_redirect(net):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, content=b"new page", headers={"content-type": "text/plain"})

    net.handler = handler
    r = await do_fetch(net, "http://example.com/old")
    assert r.ok
    assert "URL: http://example.com/new" in r.content


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:18611/",
        "http://169.254.169.254/latest",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "http://localhost/",
    ],
)
async def test_redirect_to_forbidden_target_is_revetted(net, target):
    net.hosts["localhost"] = ["127.0.0.1"]
    net.handler = lambda request: httpx.Response(302, headers={"location": target})
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok
    assert len(net.requests) == 1  # only the public hop was ever contacted


async def test_redirect_limit_of_three(net):
    net.handler = lambda request: httpx.Response(302, headers={"location": "/again"})
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok
    assert "redirect" in r.content.lower()
    assert len(net.requests) == 4  # original + 3 followed redirects


async def test_three_redirects_are_allowed(net):
    def handler(request):
        n = int(request.url.path.strip("/") or 0)
        if n < 3:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return httpx.Response(200, content=b"done", headers={"content-type": "text/plain"})

    net.handler = handler
    r = await do_fetch(net, "http://example.com/")
    assert r.ok
    assert r.content.endswith("done")


async def test_redirect_without_location(net):
    net.handler = lambda request: httpx.Response(302)
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok


# --- size, types, status ------------------------------------------------------------------


async def test_size_cap_stops_reading(net):
    delivered = []

    async def stream():
        for _ in range(100):
            delivered.append(1)
            yield b"a" * 100_000

    net.handler = lambda request: httpx.Response(
        200, content=stream(), headers={"content-type": "text/plain"}
    )
    r = await do_fetch(net, "http://example.com/", max_chars=10**7)
    assert r.ok
    body = r.content.split("\n\n", 1)[1]
    assert len(body) == 1_000_000
    assert len(delivered) <= 11  # 10 chunks reach the cap; nothing near the full 10 MB


async def test_size_cap_applies_to_decompressed_bytes(net):
    bomb = gzip.compress(b"b" * 5_000_000)
    net.handler = lambda request: httpx.Response(
        200, content=bomb, headers={"content-type": "text/plain", "content-encoding": "gzip"}
    )
    r = await do_fetch(net, "http://example.com/", max_chars=10**7)
    assert r.ok
    assert len(r.content.split("\n\n", 1)[1]) == 1_000_000


async def test_custom_byte_cap(net):
    net.handler = lambda request: httpx.Response(
        200, content=b"x" * 5000, headers={"content-type": "text/plain"}
    )
    r = await do_fetch(net, "http://example.com/", max_bytes=1000, max_chars=10**6)
    assert len(r.content.split("\n\n", 1)[1]) == 1000


@pytest.mark.parametrize(
    ("ctype", "body", "expected"),
    [
        ("text/html; charset=utf-8", HTML, True),
        ("TEXT/HTML", HTML, True),
        ("text/plain", b"plain text", True),
        ("application/json", b'{"a": 1}', True),
        ("application/json; charset=utf-8", b'{"a": 1}', True),
        ("application/pdf", b"%PDF-1.4", False),
        ("image/png", b"\x89PNG", False),
        ("application/octet-stream", b"\x00\x01", False),
        ("text/css", b"a{}", False),
        ("application/xml", b"<a/>", False),
        ("video/mp4", b"x", False),
    ],
)
async def test_content_types(net, ctype, body, expected):
    net.handler = lambda request: httpx.Response(200, content=body, headers={"content-type": ctype})
    r = await do_fetch(net, "http://example.com/")
    assert r.ok is expected
    if not expected:
        assert "content type" in r.content


async def test_missing_content_type_rejected(net):
    net.handler = lambda request: httpx.Response(200, content=b"data")
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok


async def test_json_and_plain_text_have_no_title(net):
    net.handler = lambda request: httpx.Response(
        200, content=b'{"a":   1}\n\n\n{"b": 2}', headers={"content-type": "application/json"}
    )
    r = await do_fetch(net, "http://example.com/data.json")
    assert r.content == 'URL: http://example.com/data.json\n\n{"a": 1}\n{"b": 2}'


async def test_http_error_status(net):
    net.handler = lambda request: httpx.Response(
        404, content=b"gone", headers={"content-type": "text/plain"}
    )
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok
    assert "404" in r.content


async def test_max_chars_truncation(net):
    net.handler = lambda request: httpx.Response(
        200, content=b"word " * 2000, headers={"content-type": "text/plain"}
    )
    r = await do_fetch(net, "http://example.com/", max_chars=300)
    assert r.ok
    assert len(r.content) <= 300
    assert r.content.endswith("…")
    assert r.content.startswith("URL: http://example.com/")


async def test_charset_from_header_and_meta(net):
    net.handler = lambda request: httpx.Response(
        200,
        content="café".encode("latin-1"),
        headers={"content-type": "text/plain; charset=latin-1"},
    )
    assert (await do_fetch(net, "http://example.com/")).content.endswith("café")
    page = b'<html><head><meta charset="windows-1252"><title>t</title></head><body>caf\xe9</body></html>'
    net.handler = lambda request: httpx.Response(
        200, content=page, headers={"content-type": "text/html"}
    )
    assert (await do_fetch(net, "http://example.com/")).content.endswith("café")


async def test_timeout_is_reported(net):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    net.handler = handler
    r = await do_fetch(net, "http://example.com/")
    assert not r.ok
    assert "timed out" in r.content


async def test_overall_deadline(net):
    import asyncio

    async def slow_resolver(host, port):
        await asyncio.sleep(5)
        return [PUBLIC]

    net.hosts.clear()
    fetch_url._resolver = slow_resolver  # restored by the ``net`` fixture's monkeypatch
    r = await fetch("http://example.com/", timeout_s=0.05, max_chars=100, transport=net.transport)
    assert not r.ok
    assert "timed out" in r.content


async def test_block_private_can_be_disabled_for_hostnames(net):
    net.hosts["intranet.test"] = ["10.1.2.3"]
    r = await do_fetch(net, "http://intranet.test/", block_private=False)
    assert r.ok
    assert net.requests[0].url.host == "10.1.2.3"


async def test_no_proxy_env_is_used(net, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    r = await do_fetch(net, "http://example.com/")
    assert r.ok


async def test_idn_host_uses_punycode_in_host_header_and_sni(net):
    net.hosts["xn--bcher-kva.de"] = [PUBLIC]
    r = await do_fetch(net, "http://bücher.de/")
    assert r.ok
    assert net.requests[0].headers["host"] == "xn--bcher-kva.de"
    assert net.requests[0].extensions["sni_hostname"] == "xn--bcher-kva.de"
