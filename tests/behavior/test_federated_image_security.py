"""Untrusted image requests are limited at the actual HTTP/DNS edge."""
from __future__ import annotations

import asyncio
from io import BytesIO
import socket
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from src import federated_media as media


def _png():
    """Produce a genuine tiny PNG, not merely a forged MIME header."""
    buffer = BytesIO()
    Image.new('RGB', (2, 2)).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.mark.parametrize('url', [
    'http://example.org/a.png', 'https://127.0.0.1/a.png', 'https://127.1/a.png',
    'https://[::1]/a.png', 'https://[::ffff:127.0.0.1]/a.png',
    'https://169.254.169.254/a.png', 'https://192.168.1.1/a.png',
    'https://example.org:444/a.png', 'https://a:b@example.org/a.png',
    'https://example.org/a.png#fragment', 'https://localhost/a.png',
    'https://[64:ff9b::a00:1]/a.png', 'https://224.0.0.1/a.png',
])
def test_reject_nonpublic_or_insecure_url_before_http(url):
    """A rejected literal or protocol never reaches a connector."""
    with pytest.raises(ValueError):
        media._validate_download_url(url)


@pytest.mark.asyncio
async def test_connector_uses_only_validated_dns_addresses(monkeypatch):
    """Mixed answers fail closed; public answers are pinned without a second DNS GET."""
    loop = asyncio.get_running_loop()
    def answer(ip):
        return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))
    resolver = media._PublicImageResolver()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[answer('8.8.8.8'), answer('127.0.0.1')]))
    with pytest.raises(ValueError):
        await resolver.resolve('example.org', 443)
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[answer('8.8.8.8')]))
    result = await resolver.resolve('example.org', 443)
    assert [r['host'] for r in result] == ['8.8.8.8']
    assert all(r['flags'] == socket.AI_NUMERICHOST for r in result)


class _Response:
    """A controlled HTTP response supports streaming, not an unbounded read."""
    def __init__(self, data=b'', *, status=200, mime='image/png', headers=None, length=None):
        self.status, self.content_type = status, mime
        self.headers, self.content_length = headers or {}, length
        self.data, self.content = data, self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return None
    async def iter_chunked(self, _):
        for start in range(0, len(self.data), 32):
            yield self.data[start:start+32]


@pytest.mark.asyncio
async def test_remote_image_http_redirect_size_and_mime_guards(monkeypatch):
    """The production downloader validates every hop and streams a bounded file."""
    responses = []
    requests = []
    class Session:
        """Keep the actual connector configuration, substitute HTTP responses only."""
        def __init__(self, **options):
            assert options['trust_env'] is False and options['auto_decompress'] is False
            self.connector = options['connector']
            assert isinstance(self.connector._resolver, media._PublicImageResolver)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            await self.connector.close()
        def get(self, url, **options):
            assert options['allow_redirects'] is False
            requests.append(str(url))
            return responses.pop(0)
    monkeypatch.setattr(media.aiohttp, 'ClientSession', Session)
    url = 'https://example.org/image'
    png = _png()
    responses.extend([_Response(status=302, headers={'Location': '/real.png'}), _Response(png)])
    assert (await media._download_image(url)).data == png
    assert requests == [url, 'https://example.org/real.png']
    for destination in ('http://example.org/image.png', 'https://127.0.0.1/x'):
        requests.clear()
        responses.append(_Response(status=302, headers={'Location': destination}))
        assert await media.download_images((url,)) == ()
        assert requests == [url]
    monkeypatch.setattr(media, 'MAX_IMAGE_BYTES', len(png))
    for invalid in (_Response(b'<html>', mime='text/html'), _Response(png, mime='image/jpeg'),
                    _Response(png+b'x'), _Response(png, length=len(png)+1),
                    _Response(png, headers={'Content-Encoding': 'gzip'})):
        responses.append(invalid)
        assert await media.download_images((url,)) == ()
    responses.append(_Response(png))
    assert len(await media.download_images((url,))) == 1


@pytest.mark.asyncio
async def test_image_count_and_failure_do_not_consume_healthy_file(monkeypatch):
    """One unavailable source cannot suppress subsequent valid attachments."""
    seen = []
    async def fetch(url):
        seen.append(url)
        if url.endswith('/0'):
            raise TimeoutError('unavailable')
        return media.DownloadedImage(_png(), 'png')
    monkeypatch.setattr(media, '_download_image', fetch)
    urls = tuple(f'https://example.org/{i}' for i in range(8))
    result = await media.download_images(urls)
    assert seen == list(urls[:media.MAX_IMAGES])
    assert len(result) == media.MAX_IMAGES - 1


def test_markdown_html_and_attachment_share_original_image_identity():
    """Multiple encodings contribute one source link and one upload candidate."""
    image = 'https://example.org/asset?id=2'
    body, urls = media.image_presentation(f'![]({image}) https://example.org/article', {
        'content': f'<img src="{image}"><img src="/second.png">',
        'attachment': [{'type': 'Image', 'url': {'type': 'Link', 'href': image}},
                       {'type': 'Document', 'mediaType': 'application/pdf', 'url': 'https://example.org/file.pdf'}],
    }, 'https://example.org/post/1')
    assert urls == (image, 'https://example.org/second.png')
    assert body.count(image) == 1
    assert 'https://example.org/second.png' in body and 'https://example.org/article' in body
    assert 'file.pdf' not in body
    body, urls = media.image_presentation('<img src="/picture.png">', None, 'https://example.org/post/1')
    assert body == 'https://example.org/picture.png' and urls == (body,)


def test_relative_markdown_image_source_is_clickable():
    """NodeBB relative image links resolve against the canonical Note origin."""
    body, urls = media.image_presentation('![photo](/assets/photo "caption")', None, 'https://example.org/post/1')
    assert urls == ('https://example.org/assets/photo',)
    assert body == '[photo](<https://example.org/assets/photo>)'
