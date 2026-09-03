"""Bounded, public-network-only fetching for RSS and Atom feeds."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import ssl
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import feedparser

CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 15.0
MAX_BODY_BYTES = 1024 * 1024
MAX_REDIRECTS = 3

_HEADER_LIMIT = 64 * 1024
_URL_LIMIT = 2048
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class FeedFetchError(Exception):
    """A bounded, safe-to-display feed fetch failure."""


@dataclass(frozen=True)
class _Destination:
    url: str
    scheme: str
    host: str
    port: int
    target: str
    host_header: str


@dataclass(frozen=True)
class _FetchedFeed:
    body: bytes
    url: str
    headers: dict[str, str]


def _destination(url: str) -> _Destination:
    if not isinstance(url, str) or not url or len(url) > _URL_LIMIT:
        raise FeedFetchError("invalid feed URL")
    if not url.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise FeedFetchError("invalid feed URL")
    if _BAD_PERCENT_ESCAPE.search(url):
        raise FeedFetchError("invalid feed URL")

    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise FeedFetchError("invalid feed URL") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.netloc or host is None:
        raise FeedFetchError("feed URL must use HTTP(S) and include a hostname")
    if "@" in parts.netloc:
        raise FeedFetchError("feed URL must not include credentials")
    if not host or not _valid_host(host):
        raise FeedFetchError("invalid feed URL hostname")

    port = port if port is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise FeedFetchError("invalid feed URL port")

    normalized_host = host.lower()
    path = parts.path or "/"
    target = path + (f"?{parts.query}" if parts.query else "")
    default_port = 443 if scheme == "https" else 80
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    host_header = display_host if port == default_port else f"{display_host}:{port}"
    normalized = SplitResult(scheme, parts.netloc, parts.path, parts.query, "")
    return _Destination(
        url=urlunsplit(normalized),
        scheme=scheme,
        host=normalized_host,
        port=port,
        target=target,
        host_header=host_header,
    )


def _valid_host(host: str) -> bool:
    if "%" in host or host.endswith(".."):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host.rstrip(".")) > 253:
            return False
        labels = host.rstrip(".").split(".")
        return bool(labels) and all(_HOST_LABEL.fullmatch(label) for label in labels)
    return True


def _public_address(sockaddr: tuple[Any, ...]) -> bool:
    try:
        address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global and not any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_unspecified,
            address.is_reserved,
            getattr(address, "is_site_local", False),
        )
    )


async def _resolve(destination: _Destination) -> list[tuple[int, int, int, tuple[Any, ...]]]:
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(
            destination.host,
            destination.port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (OSError, UnicodeError) as exc:
        raise FeedFetchError("destination lookup failed") from exc
    if not answers:
        raise FeedFetchError("destination lookup failed")
    if not all(_public_address(answer[4]) for answer in answers):
        raise FeedFetchError("destination is not publicly routable")

    unique: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for family, socktype, proto, _, sockaddr in answers:
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            unique.append((family, socktype, proto, sockaddr))
    return unique


async def _open_stream(
    destination: _Destination,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            answers = await _resolve(destination)
            last_error: OSError | None = None
            for answer in answers:
                try:
                    return await _open_address(destination, answer)
                except OSError as exc:
                    last_error = exc
            raise FeedFetchError("destination connection failed") from last_error
    except TimeoutError as exc:
        raise FeedFetchError("destination connection timed out") from exc


async def _open_address(
    destination: _Destination,
    answer: tuple[int, int, int, tuple[Any, ...]],
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    family, socktype, proto, sockaddr = answer
    sock = socket.socket(family, socktype, proto)
    try:
        sock.setblocking(False)
        # sockaddr came from the validated getaddrinfo result. Passing the numeric tuple directly
        # avoids a second name lookup between policy validation and the connection.
        await asyncio.get_running_loop().sock_connect(sock, sockaddr)
        ssl_context = ssl.create_default_context() if destination.scheme == "https" else None
        return await asyncio.open_connection(
            sock=sock,
            ssl=ssl_context,
            server_hostname=destination.host if ssl_context else None,
            limit=_HEADER_LIMIT,
        )
    except BaseException:
        sock.close()
        raise


async def _readline(reader: asyncio.StreamReader, *, message: str) -> bytes:
    try:
        line = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise FeedFetchError(message) from exc
    if not line or len(line) > _HEADER_LIMIT:
        raise FeedFetchError(message)
    return line


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, list[str]]]:
    status_line = await _readline(reader, message="invalid HTTP response")
    try:
        version, status_text, _ = status_line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
        status = int(status_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise FeedFetchError("invalid HTTP response") from exc
    if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status <= 599:
        raise FeedFetchError("invalid HTTP response")

    headers: dict[str, list[str]] = {}
    total = len(status_line)
    while True:
        line = await _readline(reader, message="invalid HTTP response headers")
        total += len(line)
        if total > _HEADER_LIMIT:
            raise FeedFetchError("HTTP response headers are too large")
        if line in {b"\r\n", b"\n"}:
            return status, headers
        if line[:1] in {b" ", b"\t"} or b":" not in line:
            raise FeedFetchError("invalid HTTP response headers")
        name_bytes, value_bytes = line.split(b":", 1)
        try:
            name = name_bytes.decode("ascii").lower()
            value = value_bytes.decode("iso-8859-1").strip()
        except UnicodeDecodeError as exc:
            raise FeedFetchError("invalid HTTP response headers") from exc
        if not name or not all(char.isalnum() or char in "!#$%&'*+-.^_`|~" for char in name):
            raise FeedFetchError("invalid HTTP response headers")
        headers.setdefault(name, []).append(value)


class _BodyDecoder:
    def __init__(self, encoding: str) -> None:
        self.output = bytearray()
        self.encoding = encoding.strip().lower()
        self._decoder: zlib.Decompress | None = None
        self._deflate_fallback = False
        if self.encoding in {"", "identity"}:
            return
        if self.encoding == "gzip":
            self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif self.encoding == "deflate":
            self._decoder = zlib.decompressobj()
        else:
            raise FeedFetchError("unsupported HTTP content encoding")

    def feed(self, data: bytes) -> None:
        if self._decoder is None:
            self._append(data)
            return
        try:
            decoded = self._decoder.decompress(data, MAX_BODY_BYTES - len(self.output) + 1)
        except zlib.error as exc:
            if self.encoding != "deflate" or self.output or self._deflate_fallback:
                raise FeedFetchError("invalid compressed response") from exc
            self._deflate_fallback = True
            self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                decoded = self._decoder.decompress(data, MAX_BODY_BYTES + 1)
            except zlib.error as fallback_exc:
                raise FeedFetchError("invalid compressed response") from fallback_exc
        self._append(decoded)
        if self._decoder.unconsumed_tail:
            raise FeedFetchError("feed response is too large")

    def finish(self) -> bytes:
        if self._decoder is not None:
            try:
                self._append(self._decoder.flush(MAX_BODY_BYTES - len(self.output) + 1))
            except zlib.error as exc:
                raise FeedFetchError("invalid compressed response") from exc
            if not self._decoder.eof or self._decoder.unused_data:
                raise FeedFetchError("invalid compressed response")
        return bytes(self.output)

    def _append(self, data: bytes) -> None:
        self.output.extend(data)
        if len(self.output) > MAX_BODY_BYTES:
            raise FeedFetchError("feed response is too large")


def _single_header(headers: dict[str, list[str]], name: str) -> str | None:
    values = headers.get(name, [])
    if not values:
        return None
    if len(values) != 1:
        raise FeedFetchError("ambiguous HTTP response headers")
    return values[0]


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, list[str]]) -> bytes:
    content_length_value = _single_header(headers, "content-length")
    transfer_encoding = _single_header(headers, "transfer-encoding")
    content_encoding = _single_header(headers, "content-encoding") or "identity"
    if content_length_value is not None and transfer_encoding is not None:
        raise FeedFetchError("ambiguous HTTP response framing")

    content_length: int | None = None
    if content_length_value is not None:
        try:
            content_length = int(content_length_value)
        except ValueError as exc:
            raise FeedFetchError("invalid HTTP Content-Length") from exc
        if content_length < 0:
            raise FeedFetchError("invalid HTTP Content-Length")
        if content_length > MAX_BODY_BYTES:
            raise FeedFetchError("feed response is too large")

    decoder = _BodyDecoder(content_encoding)
    received = 0
    wire_bytes = 0

    def count_wire(size: int) -> None:
        nonlocal wire_bytes
        wire_bytes += size
        if wire_bytes > MAX_BODY_BYTES:
            raise FeedFetchError("feed response is too large")

    async def consume(data: bytes) -> None:
        nonlocal received
        count_wire(len(data))
        received += len(data)
        if received > MAX_BODY_BYTES:
            raise FeedFetchError("feed response is too large")
        decoder.feed(data)

    try:
        if transfer_encoding is not None:
            if transfer_encoding.lower() != "chunked":
                raise FeedFetchError("unsupported HTTP transfer encoding")
            trailer_bytes = 0
            while True:
                line = await _readline(reader, message="invalid chunked HTTP response")
                count_wire(len(line))
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError as exc:
                    raise FeedFetchError("invalid chunked HTTP response") from exc
                if size < 0 or size > MAX_BODY_BYTES - received:
                    raise FeedFetchError("feed response is too large")
                if size == 0:
                    while True:
                        trailer = await _readline(
                            reader, message="invalid chunked HTTP response"
                        )
                        count_wire(len(trailer))
                        trailer_bytes += len(trailer)
                        if trailer_bytes > _HEADER_LIMIT:
                            raise FeedFetchError("HTTP response trailers are too large")
                        if trailer in {b"\r\n", b"\n"}:
                            break
                    break
                if size > MAX_BODY_BYTES - wire_bytes:
                    raise FeedFetchError("feed response is too large")
                await consume(await reader.readexactly(size))
                framing = await reader.readexactly(2)
                count_wire(len(framing))
                if framing != b"\r\n":
                    raise FeedFetchError("invalid chunked HTTP response")
        elif content_length is not None:
            remaining = content_length
            while remaining:
                chunk = await reader.read(min(64 * 1024, remaining))
                if not chunk:
                    raise FeedFetchError("incomplete HTTP response body")
                remaining -= len(chunk)
                await consume(chunk)
        else:
            while True:
                chunk = await reader.read(min(64 * 1024, MAX_BODY_BYTES - wire_bytes + 1))
                if not chunk:
                    break
                await consume(chunk)
    except asyncio.IncompleteReadError as exc:
        raise FeedFetchError("incomplete HTTP response body") from exc
    return decoder.finish()


async def _request_once(destination: _Destination) -> tuple[int, dict[str, list[str]], bytes]:
    reader, writer = await _open_stream(destination)
    request = (
        f"GET {destination.target} HTTP/1.1\r\n"
        f"Host: {destination.host_header}\r\n"
        "User-Agent: Roger/0.1 feed fetcher\r\n"
        "Accept: application/atom+xml, application/rss+xml, application/xml, text/xml, */*\r\n"
        "Accept-Encoding: gzip, deflate\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        writer.write(request)
        await writer.drain()
        status, headers = await _read_headers(reader)
        body = b"" if status in _REDIRECT_STATUSES else await _read_body(reader, headers)
        return status, headers, body
    except (ConnectionError, OSError) as exc:
        raise FeedFetchError("HTTP connection failed") from exc
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _feedparser_headers(url: str, headers: dict[str, list[str]]) -> dict[str, str]:
    safe = {"content-location": url}
    for name in ("content-type", "content-language"):
        value = _single_header(headers, name)
        if value:
            safe[name] = value
    return safe


async def _fetch_feed_response(url: str) -> _FetchedFeed:
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            current = _destination(url)
            visited = {current.url}
            for redirects in range(MAX_REDIRECTS + 1):
                status, headers, body = await _request_once(current)
                if status not in _REDIRECT_STATUSES:
                    if not 200 <= status < 300:
                        raise FeedFetchError(f"HTTP {status}")
                    return _FetchedFeed(
                        body=body,
                        url=current.url,
                        headers=_feedparser_headers(current.url, headers),
                    )
                location = _single_header(headers, "location")
                if not location:
                    raise FeedFetchError("redirect response has no valid Location")
                if redirects == MAX_REDIRECTS:
                    raise FeedFetchError("too many feed redirects")
                try:
                    next_destination = _destination(urljoin(current.url, location))
                except (TypeError, ValueError) as exc:
                    raise FeedFetchError("redirect response has no valid Location") from exc
                if next_destination.url in visited:
                    raise FeedFetchError("feed redirect loop")
                visited.add(next_destination.url)
                current = next_destination
    except TimeoutError as exc:
        raise FeedFetchError("feed request timed out") from exc
    raise AssertionError("unreachable")


async def fetch_feed_bytes(url: str) -> bytes:
    """Fetch one feed within fixed network, redirect, time, and size boundaries."""
    return (await _fetch_feed_response(url)).body


async def fetch_feed(url: str) -> Any:
    """Fetch bounded bytes and parse them without granting feedparser network access."""
    fetched = await _fetch_feed_response(url)
    return await asyncio.to_thread(
        feedparser.parse,
        fetched.body,
        response_headers=fetched.headers,
    )
