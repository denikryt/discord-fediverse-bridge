"""Bounded, ephemeral remote image retrieval for bridge-owned Discord messages.

Media URLs are presentation data, not ActivityPub identity. Only explicit image
references are fetched, with DNS validation at the actual connection boundary.
A failed image is omitted from uploads, never from the source text.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from ipaddress import ip_address, ip_network
import json
from pathlib import PurePosixPath
import re
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.helpers import is_ip_address
from yarl import URL
from PIL import Image

from .formatting import _feed_markdown_link_end, _feed_protected_spans

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_SECONDS = 5
MAX_REDIRECTS = 3
MAX_PIXELS = 25_000_000
MAX_URL_LENGTH = 2048
MAX_REFERENCES = 32
_FORMATS = {'image/png': ('PNG', 'png'), 'image/jpeg': ('JPEG', 'jpg'),
            'image/gif': ('GIF', 'gif'), 'image/webp': ('WEBP', 'webp')}
_EXTENSIONS = {'.png', '.jpeg', '.jpg', '.gif', '.webp'}
# This fallback covers simple relative/optional-title image destinations. The
# normal protected-span parser handles balanced absolute Markdown URLs.
_SIMPLE_IMAGE = re.compile(r'''!\[([^\]\n]*)\]\((<[^<>\n]+>|[^\s()<>]+)(?:[ \t]+["'][^\n]*?["'])?\)''')


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """Validated compressed raster bytes, retained only until Discord upload."""

    data: bytes
    extension: str


class _HTMLImages(HTMLParser):
    """Read img sources without interpreting or executing remote HTML."""

    def __init__(self) -> None:
        """Keep a bounded number of explicit image references."""
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        """Capture only actual img src attributes, not arbitrary HTML links."""
        if tag == 'img' and len(self.urls) < MAX_REFERENCES:
            src = dict(attrs).get('src')
            if isinstance(src, str):
                self.urls.append(src)


def media_source_from_note(note: object) -> dict | None:
    """Keep only source fields needed after normalization strips HTML tags."""
    if not isinstance(note, dict):
        return None
    source = {}
    if note.get('attachment') is not None:
        source['attachment'] = note['attachment']
    content = note.get('content')
    if isinstance(content, str) and '<img' in content.lower():
        source['content'] = content
    return source or None


def note_from_activity(source: str | None) -> dict:
    """Read the retained Person Note, not a newly dereferenced AP object."""
    try:
        parsed = json.loads(source or '{}')
        note = parsed.get('object') if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return {}
    return note if isinstance(note, dict) else {}


def _url(value: object, base: str) -> str | None:
    """Accept a displayable HTTP(S) IRI; download eligibility is stricter."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ch in '<>\\`' for ch in value):
        return None
    try:
        # Resolve relative image sources against the canonical Note URL.
        value = urljoin(base, unescape(value))
        if any(ch.isspace() or ord(ch) < 32 or ch in '<>\\`' for ch in value):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or len(value) > MAX_URL_LENGTH):
            return None
    except ValueError:
        return None
    return value


def _has_image_suffix(url: str) -> bool:
    """Use a known raster suffix only as a candidate, never as MIME proof."""
    return PurePosixPath(urlsplit(url).path).suffix.lower() in _EXTENSIONS


def _attachment_references(note: dict, base: str) -> list[tuple[str, bool]]:
    """Recognize Image/Document/Link URLs, including nested Link href arrays."""
    raw = note.get('attachment')
    items = raw if isinstance(raw, list) else [raw]
    result = []
    for item in items[:MAX_REFERENCES]:
        if not isinstance(item, dict):
            continue
        types = item.get('type')
        types = types if isinstance(types, list) else [types]
        media_type = item.get('mediaType')
        addresses = item.get('url') or item.get('href')
        addresses = addresses if isinstance(addresses, list) else [addresses]
        for address in addresses[:MAX_REFERENCES]:
            mime = media_type
            if isinstance(address, dict):
                mime = address.get('mediaType') or mime
                address = address.get('href') or address.get('id')
            url = _url(address, base)
            if not url:
                continue
            # Explicit non-image MIME wins over an ambiguous extension.
            image = (isinstance(mime, str) and mime.lower().startswith('image/')) if mime else (
                'Image' in types or _has_image_suffix(url))
            result.append((url, image))
            if len(result) >= MAX_REFERENCES:
                return result
    return result


def _body_references(body: str, base: str) -> list[tuple[str, bool]]:
    """Read complete Markdown links and bare image URLs without probing articles."""
    result = []
    # Protected spans include both the whole Markdown atom and its inner raw
    # URL. Prefer the outer span so a closing parenthesis is not part of the URL.
    end_seen = 0
    for start, end in sorted(_feed_protected_spans(body), key=lambda span: (span[0], -span[1])):
        if start < end_seen:
            continue
        end_seen = end
        token = body[start:end]
        explicit = False
        if token.startswith('[') and _feed_markdown_link_end(body, start) == end:
            # The closing label delimiter can itself be escaped; the URL
            # starts at the final valid ]( before the protected destination.
            match = re.search(r'\]\((?=<?https?://)', token)
            if match is None:
                continue
            token = token[match.end():-1].strip('<>')
            explicit = start > 0 and body[start-1] == '!'
        else:
            token = token.strip('<>')
            # Trim ordinary prose punctuation from bare URLs, without cutting
            # balanced parentheses that legitimately belong to the path.
            token = token.rstrip('.,;!?')
            while token.endswith(')') and token.count(')') > token.count('('):
                token = token[:-1]
        url = _url(token, base)
        if url:
            result.append((url, explicit or _has_image_suffix(url)))
    return result


def _html_references(html: object, base: str) -> list[str]:
    """Extract absolute display URLs from original HTML image elements."""
    if not isinstance(html, str) or '<img' not in html.lower():
        return []
    parser = _HTMLImages()
    try:
        parser.feed(html)
    except ValueError:
        return []
    return [url for item in parser.urls if (url := _url(item, base))]


def image_presentation(body: str | None, note: dict | None, object_id: str,
                       *, include_other_attachments: bool = False,
                       external_url: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Return link-preserving body and ordered unique image candidates.

    Raw image links already represented in Markdown are not appended again.
    HTML and attachment-only images retain a readable original source link.
    Non-image attachments are retained for Person roots as before.
    """
    body = body or ''
    note = note or {}
    html_urls = _html_references(body, object_id)
    # NodeBB can supply HTML when source Markdown is absent. Remove only img
    # tags, then present their sources as ordinary links below.
    if html_urls:
        body = re.sub(r"""<img\b(?:[^"'>]|"[^"]*"|'[^']*')*>""", '', body, flags=re.I).strip()
    explicit_images = []

    def render_image(match: re.Match[str]) -> str:
        """Make an explicit image's source clickable in Discord's link syntax."""
        url = _url(match.group(2).strip('<>'), object_id)
        if url is None:
            return match.group(0)
        explicit_images.append(url)
        label = match.group(1).strip()
        return f'[{label}](<{url}>)' if label else f'<{url}>'

    body = _SIMPLE_IMAGE.sub(render_image, body)
    body_refs = _body_references(body, object_id)
    seen = {url for url, _ in body_refs}
    # HTML tags are not visible links in Discord; preserve their sources
    # explicitly even if the original normalized body still contains HTML.
    refs = [(url, True) for url in _html_references(note.get('content'), object_id)]
    refs += [(url, True) for url in html_urls]
    refs += _attachment_references(note, object_id)
    candidates = list(dict.fromkeys([*explicit_images, *(url for url, image in body_refs if image)]))
    for url, image in refs:
        if image:
            if url not in candidates:
                candidates.append(url)
        if (image or include_other_attachments) and url not in seen and url != object_id:
            body = f'{body}\n\n{url}' if body else url
            seen.add(url)
    if external_url and external_url != object_id:
        url = _url(external_url, object_id)
        if url:
            if _has_image_suffix(url) and url not in candidates:
                candidates.append(url)
            if url not in seen:
                body = f'{body}\n\n{url}' if body else url
    return body, tuple(candidates[:MAX_IMAGES])


def _public_ip(value: str) -> bool:
    """Reject private, multicast, reserved and IPv4-mapped non-public addresses."""
    ip = ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _public_ip(str(ip.ipv4_mapped))
        # Transition addresses can tunnel to IPv4 destinations not apparent
        # from the outer public-looking IPv6 address.
        if (ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None
                or ip in ip_network('64:ff9b::/96') or ip in ip_network('64:ff9b:1::/48')):
            return False
    return True


def _validate_download_url(url: str) -> URL:
    """Require HTTPS/443 without credentials, fragments or unsafe literals."""
    if _url(url, '') != url:
        raise ValueError('invalid image URL')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError('unsupported image origin')
    # Validate the same parsed hostname that aiohttp will connect to. Its
    # fast-path considers abbreviated/octal IPv4 literals to be IP addresses
    # and bypasses the resolver; ipaddress rejects these ambiguous forms.
    target = URL(url)
    host = target.raw_host or ''
    if '%' in host or target.user is not None or target.password is not None:
        raise ValueError('invalid image hostname')
    try:
        ip_address(host)
    except ValueError:
        if '.' not in host or is_ip_address(host):
            raise ValueError('non-public image hostname')
    else:
        if not _public_ip(host):
            raise ValueError('non-public image address')
    return target


class _PublicImageResolver(AbstractResolver):
    """Pin validated public DNS answers to the connector's actual sockets."""

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        """Reject mixed public/private answers instead of selecting an unsafe IP."""
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM)
        if not answers or any(not _public_ip(answer[4][0]) for answer in answers):
            raise ValueError('non-public image DNS answer')
        return [{'hostname': host, 'host': answer[4][0], 'port': port,
                 'family': answer[0], 'proto': answer[2], 'flags': socket.AI_NUMERICHOST}
                for answer in answers]

    async def close(self):
        """The stateless resolver owns no sockets."""


def _validate_raster(data: bytes, mime: str) -> DownloadedImage:
    """Reject mislabeled or oversized decoded raster images without re-encoding."""
    fmt, extension = _FORMATS[mime]
    with Image.open(BytesIO(data)) as image:
        if image.format != fmt or image.width * image.height > MAX_PIXELS:
            raise ValueError('invalid or oversized image raster')
        image.verify()
    return DownloadedImage(data, extension)


async def _download_image(url: str) -> DownloadedImage:
    """Retrieve one bounded raster via public HTTPS, validating every redirect."""
    connector = aiohttp.TCPConnector(resolver=_PublicImageResolver(),
        use_dns_cache=False, force_close=True, limit=1, family=socket.AF_UNSPEC)
    # One wall-clock timeout includes DNS, redirects and streamed reads.
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=MAX_IMAGE_SECONDS)) as session:
        async with asyncio.timeout(MAX_IMAGE_SECONDS):
            for _ in range(MAX_REDIRECTS + 1):
                target = _validate_download_url(url)
                async with session.get(target, allow_redirects=False,
                        headers={'Accept': ', '.join(_FORMATS), 'Accept-Encoding': 'identity'}) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('image redirect without location')
                        url = urljoin(url, location)
                        continue
                    mime = response.content_type.lower()
                    if (response.status != 200 or mime not in _FORMATS
                            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'
                            or (response.content_length is not None and response.content_length > MAX_IMAGE_BYTES)):
                        raise ValueError('image response rejected')
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise ValueError('image exceeds byte limit')
                        data.extend(chunk)
                    return _validate_raster(bytes(data), mime)
    raise ValueError('image redirect limit exceeded')


async def download_images(urls: tuple[str, ...]) -> tuple[DownloadedImage, ...]:
    """Best-effort sequential downloads with bounded total memory/time.

    Never log remote URLs or raw exceptions (they may contain credentials).
    Cancellation still propagates to the existing durable delivery lifecycle.
    """
    images = []
    for url in tuple(dict.fromkeys(urls))[:MAX_IMAGES]:
        try:
            images.append(await _download_image(url))
        except Exception:
            continue
    return tuple(images)


def can_attach_files(channel: object) -> bool:
    """Use effective Discord channel permissions, falling back to text only."""
    guild = getattr(channel, 'guild', None)
    if guild is None or getattr(guild, 'me', None) is None:
        return False
    try:
        return bool(channel.permissions_for(guild.me).attach_files)
    except (AttributeError, TypeError):
        return False


@contextmanager
def discord_files(images: tuple[DownloadedImage, ...]):
    """Construct fresh Discord files per request; close every in-memory stream."""
    files = []
    streams = []
    try:
        if images:
            import discord
            for index, image in enumerate(images, 1):
                stream = BytesIO(image.data)
                streams.append(stream)
                files.append(discord.File(stream, filename=f'image-{index}.{image.extension}'))
        yield files
    finally:
        for file in files:
            file.close()
        for stream in streams:
            stream.close()
