"""Offline regression tests for the shared feed-fetch boundary."""

import asyncio
import socket
import zlib
from types import SimpleNamespace

import pytest

from roger import feed_fetch
from roger.feed_fetch import FeedFetchError


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class _Writer:
    def __init__(self, *, drain_error=None, wait_error=None):
        self.written = bytearray()
        self.drain_error = drain_error
        self.wait_error = wait_error
        self.drained = False
        self.closed = False
        self.waited = False

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        self.drained = True
        if self.drain_error:
            raise self.drain_error

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True
        if self.wait_error:
            raise self.wait_error


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/feed.xml",
        "https:///feed.xml",
        "https://user:pass@example.com/feed.xml",
        "https://@example.com/feed.xml",
        "https://example.com:bad/feed.xml",
        "https://example.com:0/feed.xml",
        "https://bad host/feed.xml",
        "https://example.com/feed path.xml",
        "https://example.com../feed.xml",
        "https://example.com/%zz",
    ],
)
def test_rejects_invalid_schemes_hosts_and_credentials(url):
    with pytest.raises(FeedFetchError):
        feed_fetch._destination(url)


async def test_resolve_rejects_private_and_mixed_answers(monkeypatch):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
    ]
    loop = SimpleNamespace(getaddrinfo=lambda *args, **kwargs: asyncio.sleep(0, result=answers))
    monkeypatch.setattr(feed_fetch.asyncio, "get_running_loop", lambda: loop)

    with pytest.raises(FeedFetchError, match="not publicly routable"):
        await feed_fetch._resolve(feed_fetch._destination("http://example.com/feed"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",  # noqa: S104 - policy-rejection test, no socket is opened
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
        "::",
        "2001:db8::1",
    ],
)
async def test_resolve_rejects_every_non_global_address(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 80, 0, 0) if family == socket.AF_INET6 else (address, 80)
    answers = [
        (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
    ]
    loop = SimpleNamespace(getaddrinfo=lambda *args, **kwargs: asyncio.sleep(0, result=answers))
    monkeypatch.setattr(feed_fetch.asyncio, "get_running_loop", lambda: loop)

    with pytest.raises(FeedFetchError, match="not publicly routable"):
        await feed_fetch._resolve(feed_fetch._destination("http://example.com/feed"))


async def test_resolve_returns_only_deduplicated_public_answers(monkeypatch):
    answer = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("93.184.216.34", 80),
    )
    loop = SimpleNamespace(
        getaddrinfo=lambda *args, **kwargs: asyncio.sleep(0, result=[answer, answer])
    )
    monkeypatch.setattr(feed_fetch.asyncio, "get_running_loop", lambda: loop)

    assert await feed_fetch._resolve(feed_fetch._destination("http://example.com")) == [
        (answer[0], answer[1], answer[2], answer[4])
    ]


async def test_dns_failure_is_bounded_and_does_not_expose_details(monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("resolver leaked operator-secret detail")

    monkeypatch.setattr(
        feed_fetch.asyncio, "get_running_loop", lambda: SimpleNamespace(getaddrinfo=fail)
    )
    with pytest.raises(FeedFetchError, match="^destination lookup failed$") as exc_info:
        await feed_fetch._resolve(feed_fetch._destination("http://example.com"))
    assert "secret" not in str(exc_info.value)


async def test_connection_uses_the_exact_validated_answer(monkeypatch):
    answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("93.184.216.34", 80))
    opened = []
    reader = object()
    writer = object()

    async def resolve(destination):
        return [answer]

    async def open_address(destination, received):
        opened.append(received)
        return reader, writer

    monkeypatch.setattr(feed_fetch, "_resolve", resolve)
    monkeypatch.setattr(feed_fetch, "_open_address", open_address)

    assert await feed_fetch._open_stream(feed_fetch._destination("http://example.com")) == (
        reader,
        writer,
    )
    assert opened == [answer]


async def test_open_address_connects_the_socket_without_resolving_again(monkeypatch):
    sockaddr = ("93.184.216.34", 443)
    answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, sockaddr)
    connected = []

    class FakeSocket:
        def setblocking(self, value):
            assert value is False

        def close(self):
            raise AssertionError("successful socket should be owned by the stream")

    sock = FakeSocket()

    async def sock_connect(received_socket, received_address):
        connected.append((received_socket, received_address))

    async def open_connection(**kwargs):
        assert kwargs["sock"] is sock
        assert kwargs["server_hostname"] == "example.com"
        return "reader", "writer"

    monkeypatch.setattr(feed_fetch.socket, "socket", lambda *args: sock)
    monkeypatch.setattr(
        feed_fetch.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(sock_connect=sock_connect),
    )
    monkeypatch.setattr(feed_fetch.ssl, "create_default_context", lambda: object())
    monkeypatch.setattr(feed_fetch.asyncio, "open_connection", open_connection)

    destination = feed_fetch._destination("https://example.com/feed")
    assert await feed_fetch._open_address(destination, answer) == ("reader", "writer")
    assert connected == [(sock, sockaddr)]


async def test_open_address_closes_the_socket_when_connection_setup_fails(monkeypatch):
    sockaddr = ("93.184.216.34", 80)
    answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, sockaddr)

    class FakeSocket:
        closed = False

        def setblocking(self, value):
            assert value is False

        def close(self):
            self.closed = True

    sock = FakeSocket()

    async def sock_connect(received_socket, received_address):
        raise OSError("connection failed")

    monkeypatch.setattr(feed_fetch.socket, "socket", lambda *args: sock)
    monkeypatch.setattr(
        feed_fetch.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(sock_connect=sock_connect),
    )

    with pytest.raises(OSError, match="connection failed"):
        await feed_fetch._open_address(feed_fetch._destination("http://example.com"), answer)
    assert sock.closed is True


async def test_connection_failure_and_timeout_are_bounded(monkeypatch):
    answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("93.184.216.34", 80))

    async def resolve(destination):
        return [answer]

    async def fail(destination, received):
        raise OSError("connection leaked operator-secret detail")

    monkeypatch.setattr(feed_fetch, "_resolve", resolve)
    monkeypatch.setattr(feed_fetch, "_open_address", fail)
    with pytest.raises(FeedFetchError, match="^destination connection failed$"):
        await feed_fetch._open_stream(feed_fetch._destination("http://example.com"))

    async def hang(destination):
        await asyncio.Event().wait()

    monkeypatch.setattr(feed_fetch, "_resolve", hang)
    monkeypatch.setattr(feed_fetch, "CONNECT_TIMEOUT", 0.001)
    with pytest.raises(FeedFetchError, match="connection timed out"):
        await feed_fetch._open_stream(feed_fetch._destination("http://example.com"))


async def test_whole_request_timeout(monkeypatch):
    async def hang(destination):
        await asyncio.Event().wait()

    monkeypatch.setattr(feed_fetch, "_request_once", hang)
    monkeypatch.setattr(feed_fetch, "REQUEST_TIMEOUT", 0.001)
    with pytest.raises(FeedFetchError, match="feed request timed out"):
        await feed_fetch.fetch_feed_bytes("http://example.com/feed")


async def test_parses_valid_http_status_and_headers():
    reader = _reader(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/rss+xml\r\n"
        b"X-Test: one\r\n"
        b"X-Test: two\r\n"
        b"\r\n"
    )

    status, headers = await feed_fetch._read_headers(reader)

    assert status == 200
    assert headers == {
        "content-type": ["application/rss+xml"],
        "x-test": ["one", "two"],
    }


@pytest.mark.parametrize(
    "response",
    [
        b"not-http\r\n\r\n",
        b"HTTP/2 200 OK\r\n\r\n",
        b"HTTP/1.1 99 Nope\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n folded: value\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nmissing-colon\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nbad name: value\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n\xff: value\r\n\r\n",
    ],
)
async def test_rejects_malformed_http_status_and_headers(response):
    with pytest.raises(FeedFetchError, match="invalid HTTP response"):
        await feed_fetch._read_headers(_reader(response))


async def test_rejects_oversized_http_status_and_headers(monkeypatch):
    monkeypatch.setattr(feed_fetch, "_HEADER_LIMIT", 32)
    with pytest.raises(FeedFetchError, match="invalid HTTP response"):
        await feed_fetch._read_headers(_reader(b"HTTP/1.1 200 " + b"x" * 40 + b"\r\n"))

    response = b"HTTP/1.1 200 OK\r\nX-Test: " + b"x" * 20 + b"\r\n\r\n"
    with pytest.raises(FeedFetchError, match="headers are too large"):
        await feed_fetch._read_headers(_reader(response))


async def test_request_writes_target_and_host_reads_response_and_closes(monkeypatch):
    response = _reader(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nfeed")
    writer = _Writer()

    async def open_stream(destination):
        return response, writer

    monkeypatch.setattr(feed_fetch, "_open_stream", open_stream)
    destination = feed_fetch._destination("http://example.com:8080/feed.xml?q=1")

    assert await feed_fetch._request_once(destination) == (
        200,
        {"content-length": ["4"]},
        b"feed",
    )
    assert bytes(writer.written).startswith(
        b"GET /feed.xml?q=1 HTTP/1.1\r\nHost: example.com:8080\r\n"
    )
    assert writer.drained and writer.closed and writer.waited


async def test_request_closes_on_parse_failure(monkeypatch):
    writer = _Writer()

    async def open_stream(destination):
        return _reader(b"malformed\r\n"), writer

    monkeypatch.setattr(feed_fetch, "_open_stream", open_stream)

    with pytest.raises(FeedFetchError, match="invalid HTTP response"):
        await feed_fetch._request_once(feed_fetch._destination("http://example.com"))
    assert writer.closed and writer.waited


async def test_request_maps_connection_errors_and_still_closes(monkeypatch):
    writer = _Writer(drain_error=OSError("operator-secret connection detail"))

    async def open_stream(destination):
        return _reader(b""), writer

    monkeypatch.setattr(feed_fetch, "_open_stream", open_stream)

    with pytest.raises(FeedFetchError, match="^HTTP connection failed$") as exc_info:
        await feed_fetch._request_once(feed_fetch._destination("http://example.com"))
    assert "secret" not in str(exc_info.value)
    assert writer.closed and writer.waited


async def test_redirects_are_followed_and_revalidated(monkeypatch):
    calls = []

    async def request(destination):
        calls.append(destination.url)
        if len(calls) == 1:
            return 302, {"location": ["https://feeds.example.net/next"]}, b""
        return 200, {}, b"feed"

    monkeypatch.setattr(feed_fetch, "_request_once", request)
    assert await feed_fetch.fetch_feed_bytes("http://example.com/start") == b"feed"
    assert calls == ["http://example.com/start", "https://feeds.example.net/next"]


async def test_redirect_to_private_destination_is_rejected(monkeypatch):
    async def request(destination):
        if destination.host == "example.com":
            return 302, {"location": ["http://127.0.0.1/admin"]}, b""
        await feed_fetch._resolve(destination)
        raise AssertionError("unreachable")

    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))
    ]
    loop = SimpleNamespace(getaddrinfo=lambda *args, **kwargs: asyncio.sleep(0, result=answers))
    monkeypatch.setattr(feed_fetch.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(feed_fetch, "_request_once", request)

    with pytest.raises(FeedFetchError, match="not publicly routable"):
        await feed_fetch.fetch_feed_bytes("http://example.com/start")


@pytest.mark.parametrize("headers", [{}, {"location": [""]}, {"location": ["http://[::1"]}])
async def test_redirect_rejects_missing_or_invalid_location(monkeypatch, headers):
    async def request(destination):
        return 302, headers, b""

    monkeypatch.setattr(feed_fetch, "_request_once", request)
    with pytest.raises(FeedFetchError, match="no valid Location"):
        await feed_fetch.fetch_feed_bytes("http://example.com/start")


async def test_redirect_loop_and_limit_fail_cleanly(monkeypatch):
    async def loop(destination):
        return 302, {"location": ["/start"]}, b""

    monkeypatch.setattr(feed_fetch, "_request_once", loop)
    with pytest.raises(FeedFetchError, match="redirect loop"):
        await feed_fetch.fetch_feed_bytes("http://example.com/start")

    calls = 0

    async def overflow(destination):
        nonlocal calls
        calls += 1
        return 302, {"location": [f"/{calls}"]}, b""

    monkeypatch.setattr(feed_fetch, "_request_once", overflow)
    with pytest.raises(FeedFetchError, match="too many feed redirects"):
        await feed_fetch.fetch_feed_bytes("http://example.com/start")
    assert calls == feed_fetch.MAX_REDIRECTS + 1


async def test_declared_and_streamed_oversize_bodies_are_rejected():
    with pytest.raises(FeedFetchError, match="too large"):
        await feed_fetch._read_body(
            _reader(b""), {"content-length": [str(feed_fetch.MAX_BODY_BYTES + 1)]}
        )

    with pytest.raises(FeedFetchError, match="too large"):
        await feed_fetch._read_body(_reader(b"x" * (feed_fetch.MAX_BODY_BYTES + 1)), {})


@pytest.mark.parametrize(
    ("data", "headers", "error"),
    [
        (
            b"x",
            {"content-length": ["1"], "transfer-encoding": ["chunked"]},
            "ambiguous HTTP response framing",
        ),
        (b"x", {"content-length": ["1", "1"]}, "ambiguous HTTP response headers"),
        (b"x", {"content-length": ["nope"]}, "invalid HTTP Content-Length"),
        (b"x", {"content-length": ["-1"]}, "invalid HTTP Content-Length"),
        (b"x", {"transfer-encoding": ["compress"]}, "unsupported HTTP transfer encoding"),
        (b"x", {"content-encoding": ["br"]}, "unsupported HTTP content encoding"),
        (b"x", {"content-length": ["2"]}, "incomplete HTTP response body"),
        (b"nope\r\n", {"transfer-encoding": ["chunked"]}, "invalid chunked HTTP response"),
        (b"1\r\naXX", {"transfer-encoding": ["chunked"]}, "invalid chunked HTTP response"),
        (b"2\r\na", {"transfer-encoding": ["chunked"]}, "incomplete HTTP response body"),
    ],
)
async def test_invalid_body_framing_and_encodings_fail_closed(data, headers, error):
    with pytest.raises(FeedFetchError, match=f"^{error}$"):
        await feed_fetch._read_body(_reader(data), headers)


async def test_decompressed_oversize_body_is_rejected():
    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    compressed = compressor.compress(b"x" * (feed_fetch.MAX_BODY_BYTES + 1)) + compressor.flush()
    headers = {
        "content-length": [str(len(compressed))],
        "content-encoding": ["gzip"],
    }
    with pytest.raises(FeedFetchError, match="too large"):
        await feed_fetch._read_body(_reader(compressed), headers)


async def test_invalid_and_trailing_compressed_data_fail_closed():
    for data in (b"not-gzip", zlib.compress(b"feed")[:-1]):
        with pytest.raises(FeedFetchError, match="invalid compressed response"):
            await feed_fetch._read_body(
                _reader(data),
                {"content-length": [str(len(data))], "content-encoding": ["gzip"]},
            )

    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    data = compressor.compress(b"feed") + compressor.flush() + b"trailing"
    with pytest.raises(FeedFetchError, match="invalid compressed response"):
        await feed_fetch._read_body(
            _reader(data),
            {"content-length": [str(len(data))], "content-encoding": ["gzip"]},
        )


async def test_raw_deflate_is_supported():
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    data = compressor.compress(b"feed") + compressor.flush()
    assert await feed_fetch._read_body(
        _reader(data),
        {"content-length": [str(len(data))], "content-encoding": ["deflate"]},
    ) == b"feed"


async def test_chunk_framing_counts_toward_the_wire_limit(monkeypatch):
    monkeypatch.setattr(feed_fetch, "MAX_BODY_BYTES", 64)
    chunk = b"1;" + b"x" * 20 + b"\r\na\r\n"
    response = chunk * 3 + b"0\r\n\r\n"

    with pytest.raises(FeedFetchError, match="too large"):
        await feed_fetch._read_body(
            _reader(response),
            {"transfer-encoding": ["chunked"]},
        )


async def test_chunked_and_compressed_bodies_are_decoded_within_the_limit():
    chunked = _reader(b"4\r\nfeed\r\n0\r\n\r\n")
    assert await feed_fetch._read_body(chunked, {"transfer-encoding": ["chunked"]}) == b"feed"

    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    compressed = compressor.compress(b"feed") + compressor.flush()
    headers = {
        "content-length": [str(len(compressed))],
        "content-encoding": ["gzip"],
    }
    assert await feed_fetch._read_body(_reader(compressed), headers) == b"feed"


@pytest.mark.parametrize(
    ("document", "version", "title"),
    [
        (
            b"<rss version='2.0'><channel><title>RSS</title>"
            b"<item><title>One</title><guid>1</guid></item></channel></rss>",
            "rss20",
            "RSS",
        ),
        (
            b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Atom</title>"
            b"<entry><title>One</title><id>1</id></entry></feed>",
            "atom10",
            "Atom",
        ),
    ],
)
async def test_successful_rss_and_atom_are_parsed_from_bounded_bytes(
    monkeypatch, document, version, title
):
    async def fetch(url):
        return feed_fetch._FetchedFeed(
            body=document,
            url="https://example.com/feed",
            headers={"content-location": "https://example.com/feed"},
        )

    monkeypatch.setattr(feed_fetch, "_fetch_feed_response", fetch)
    parsed = await feed_fetch.fetch_feed("https://example.com/feed")
    assert parsed.version == version
    assert parsed.feed.title == title
    assert len(parsed.entries) == 1


async def test_relative_entry_link_uses_the_final_redirected_url(monkeypatch):
    document = (
        b"<rss version='2.0'><channel><title>RSS</title>"
        b"<item><title>One</title><guid>1</guid><link>story/1</link></item>"
        b"</channel></rss>"
    )

    async def request(destination):
        if destination.url == "https://example.com/start":
            return 302, {"location": ["/feeds/final.xml"]}, b""
        return 200, {"content-type": ["application/rss+xml"]}, document

    monkeypatch.setattr(feed_fetch, "_request_once", request)
    parsed = await feed_fetch.fetch_feed("https://example.com/start")

    assert parsed.entries[0].link == "https://example.com/feeds/story/1"
    assert parsed.headers == {
        "content-location": "https://example.com/feeds/final.xml",
        "content-type": "application/rss+xml",
    }


async def test_non_success_status_and_ambiguous_parser_headers_fail_cleanly(monkeypatch):
    async def unavailable(destination):
        return 503, {}, b""

    monkeypatch.setattr(feed_fetch, "_request_once", unavailable)
    with pytest.raises(FeedFetchError, match="^HTTP 503$"):
        await feed_fetch.fetch_feed_bytes("https://example.com/feed")

    async def ambiguous(destination):
        return 200, {"content-type": ["text/xml", "application/rss+xml"]}, b"feed"

    monkeypatch.setattr(feed_fetch, "_request_once", ambiguous)
    with pytest.raises(FeedFetchError, match="ambiguous HTTP response headers"):
        await feed_fetch.fetch_feed_bytes("https://example.com/feed")
