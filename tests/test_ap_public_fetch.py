"""Security regressions for ActivityPub parent-post backfill HTTP retrieval."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from src.activitypub_safe_fetch import PublicOnlyResolver, _admit_public_ap_url, _public_socket


@pytest.mark.parametrize('url', [
    'http://remote.example/post/1',
    'https://127.0.0.1/post/1',
    'https://[::1]/post/1',
    'https://169.254.169.254/latest/meta-data',
    'https://user:password@remote.example/post/1',
    'https://remote.example/post/1#fragment',
    'https://localhost/post/1',
])
def test_untrusted_backfill_destination_fails_before_network(url):
    """Unsafe literal, scheme, credentials or local hostname never reach HTTP."""
    with pytest.raises(ValueError):
        _admit_public_ap_url(url)


def test_public_backfill_destination_is_admitted():
    """Ordinary global HTTPS URLs are valid remote ActivityPub object IDs."""
    admitted = _admit_public_ap_url('https://remote.example/post/1?lang=en')
    assert admitted.host == 'remote.example'
    assert admitted.scheme == 'https'


@pytest.mark.asyncio
async def test_dns_mixed_public_and_loopback_cannot_start_connection(monkeypatch):
    """A single private DNS answer poisons the entire resolution, not just its record."""
    async def mixed(*args, **kwargs):
        """Simulate public and internal addresses on one hostname."""
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443)),
        ]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', mixed)
    with pytest.raises(ValueError):
        await PublicOnlyResolver().resolve('remote.example', 443)


@pytest.mark.asyncio
async def test_public_dns_snapshot_can_only_emit_public_socket_addresses(monkeypatch):
    """Resolved transport records preserve the public IP passed to socket creation."""
    async def public(*args, **kwargs):
        """Simulate an independently resolved public DNS answer."""
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', public)
    records = await PublicOnlyResolver().resolve('remote.example', 443)
    assert records[0]['host'] == '93.184.216.34'
    assert records[0]['hostname'] == 'remote.example'
    sock = _public_socket((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)))
    sock.close()
    with pytest.raises(ValueError):
        _public_socket((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('10.0.0.1', 443)))


@pytest.mark.asyncio
async def test_dns_rebinding_to_internal_is_rejected_on_next_resolution(monkeypatch):
    """No positive DNS cache allows the second attempt to reuse an old public answer."""
    calls = 0
    async def changing(*args, **kwargs):
        """Swap from public to private DNS response on a later attempt."""
        nonlocal calls
        calls += 1
        ip = '93.184.216.34' if calls == 1 else '192.168.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', changing)
    resolver = PublicOnlyResolver()
    assert (await resolver.resolve('remote.example', 443))[0]['host'] == '93.184.216.34'
    with pytest.raises(ValueError):
        await resolver.resolve('remote.example', 443)


@pytest.mark.asyncio
async def test_ap_get_disables_redirects_and_bounds_response(monkeypatch):
    """Backfill treats redirects as errors and caps the response before parsing JSON."""
    from src import activitypub_safe_fetch

    class Response:
        """A redirect response with a tempting internal Location header."""
        status = 302
        headers = {'Location': 'https://127.0.0.1/internal'}
        async def __aenter__(self):
            """Enter fake HTTP response scope."""
            return self
        async def __aexit__(self, *_):
            """Leave fake HTTP response scope."""
            return False

    class Session:
        """Fake only the outbound HTTP transport while inspecting its arguments."""
        def __init__(self, **kwargs):
            """Remember the actual connector security choices."""
            assert kwargs.get('trust_env') is False
            connector = kwargs['connector']
            assert connector._use_dns_cache is False
            assert connector._force_close is True
        async def __aenter__(self):
            """Open fake HTTP client."""
            return self
        async def __aexit__(self, *_):
            """Close fake HTTP client."""
            return False
        def get(self, url, **kwargs):
            """Ensure neither redirected GET nor proxies are implicitly used."""
            assert str(url) == 'https://remote.example/post/1'
            assert kwargs['allow_redirects'] is False
            return Response()

    monkeypatch.setattr(activitypub_safe_fetch.aiohttp, 'ClientSession', Session)
    with pytest.raises(ValueError, match='redirect'):
        await activitypub_safe_fetch.fetch_public_ap_object('https://remote.example/post/1')

@pytest.mark.asyncio
@pytest.mark.parametrize('body,expected_error', [
    (b'x' * (1024 * 1024 + 1), 'size limit'),
    (b'[]', 'must be an object'),
    (b'{bad-json', 'not JSON'),
])
async def test_ap_backfill_rejects_oversized_or_non_object_body(monkeypatch, body, expected_error):
    """An untrusted remote response cannot bypass bounded JSON-object admission."""
    from src import activitypub_safe_fetch

    class Stream:
        """Emit real-sized chunks from an untrusted response body."""
        async def iter_chunked(self, chunk_size):
            """Stream body as chunks instead of providing pre-parsed JSON."""
            for start in range(0, len(body), chunk_size):
                yield body[start:start + chunk_size]

    class Response:
        """Return a 200 response with an untrusted JSON document."""
        status = 200
        content_length = None
        content = Stream()
        async def __aenter__(self):
            """Enter the HTTP response."""
            return self
        async def __aexit__(self, *_):
            """Close the HTTP response."""
            return False

    class Session:
        """Replace only network transport; leave production parsing intact."""
        def __init__(self, **kwargs):
            """Require a public-only resolver and no implicit proxies."""
            assert kwargs['trust_env'] is False
        async def __aenter__(self):
            """Open the fake client."""
            return self
        async def __aexit__(self, *_):
            """Close the fake client."""
            return False
        def get(self, url, **kwargs):
            """Do not allow automatic redirects in this transport path."""
            assert kwargs['allow_redirects'] is False
            return Response()

    monkeypatch.setattr(activitypub_safe_fetch.aiohttp, 'ClientSession', Session)
    with pytest.raises(ValueError, match=expected_error):
        await activitypub_safe_fetch.fetch_public_ap_object('https://remote.example/post/1')


@pytest.mark.asyncio
async def test_ap_backfill_accepts_bounded_json_object(monkeypatch):
    """A legitimate bounded Page document remains accepted after SSRF hardening."""
    from src import activitypub_safe_fetch
    payload = b'{"id":"https://remote.example/post/1","type":"Page"}'

    class Stream:
        """Provide a real async iterator of bounded HTTP body bytes."""
        async def iter_chunked(self, _):
            """Yield a single small AP Page payload."""
            yield payload

    class Response:
        """Provide an ordinary HTTP 200 response."""
        status = 200
        content_length = len(payload)
        content = Stream()
        async def __aenter__(self):
            """Enter the response context."""
            return self
        async def __aexit__(self, *_):
            """Leave the response context."""
            return False

    class Session:
        """Fake the external HTTPS edge only."""
        def __init__(self, **_):
            """Do not override parsing or other domain behavior."""
        async def __aenter__(self):
            """Enter HTTP session."""
            return self
        async def __aexit__(self, *_):
            """Exit HTTP session."""
            return False
        def get(self, _url, **_):
            """Return the bounded Page over a fake network."""
            return Response()

    monkeypatch.setattr(activitypub_safe_fetch.aiohttp, 'ClientSession', Session)
    assert (await activitypub_safe_fetch.fetch_public_ap_object(
        'https://remote.example/post/1'))['type'] == 'Page'
