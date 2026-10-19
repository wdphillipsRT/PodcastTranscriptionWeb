"""Exercise the actual Requests/urllib3 transport without opening a socket."""

from io import BytesIO
import socket
import ssl
from types import SimpleNamespace

import pytest
import requests
import urllib3.connection

from feed_resolver import http_response
from ingestion_types import IngestionError


SETTINGS = SimpleNamespace(HTTP_CONNECT_TIMEOUT_SECONDS=3, HTTP_READ_TIMEOUT_SECONDS=7)
PUBLIC = "93.184.216.34"


class WireSocket:
    def __init__(self, attempts, *, fail=False, response=None):
        self.attempts, self.fail = attempts, fail
        self.response = response or b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nfeed"
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.attempts.append(address)
        if self.fail:
            raise OSError("synthetic secret transport failure")

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, *args):
        return BytesIO(self.response)

    def close(self):
        self.closed = True


def rebinding_dns(monkeypatch):
    calls = []
    def resolve(host, port, *args, **kwargs):
        calls.append(host)
        # The second lookup deliberately returns localhost, even for a numeric
        # address: the transport must not perform any second DNS resolution.
        address = PUBLIC if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return calls


def test_rebinding_never_connects_to_later_private_dns_answer(monkeypatch):
    calls = rebinding_dns(monkeypatch)
    attempts = []
    monkeypatch.setattr(socket, "socket", lambda *args: WireSocket(attempts, fail=True))
    with requests.Session() as session:
        session.trust_env = False
        with pytest.raises(IngestionError, match="http_failed") as caught:
            with http_response(session, "GET", "http://feed.example/rss", SETTINGS):
                pass
    assert attempts == [(PUBLIC, 80)]
    assert calls == ["feed.example"]
    assert "secret" not in str(caught.value) and caught.value.__cause__ is None


def test_https_pins_socket_but_preserves_host_sni_and_certificate_verification(monkeypatch):
    calls = rebinding_dns(monkeypatch)
    attempts, wrapped, sockets = [], [], []
    def make_socket(*args):
        sock = WireSocket(attempts)
        sockets.append(sock)
        return sock
    def wrap(**kwargs):
        wrapped.append(kwargs)
        return kwargs["sock"], True
    monkeypatch.setattr(socket, "socket", make_socket)
    monkeypatch.setattr(urllib3.connection, "_ssl_wrap_socket_and_match_hostname", wrap)
    with requests.Session() as session:
        session.trust_env = False
        # Caller/session configuration cannot change transport security.
        session.verify = False
        session.headers["Host"] = "attacker.invalid"
        with http_response(session, "GET", "https://feed.example:8443/rss", SETTINGS) as response:
            assert response.content == b"feed"
            assert response.url == "https://feed.example:8443/rss"
    assert attempts == [(PUBLIC, 8443)] and calls == ["feed.example"]
    assert wrapped[0]["server_hostname"] == "feed.example"
    assert wrapped[0]["assert_hostname"] in (None, "feed.example")
    assert wrapped[0]["cert_reqs"] in (ssl.CERT_REQUIRED, "CERT_REQUIRED")
    assert b"Host: feed.example:8443\r\n" in sockets[0].sent
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_redirect_revalidates_dns_before_any_second_socket(monkeypatch, target):
    calls, attempts, sockets = [], [], []
    def resolve(host, port, **kwargs):
        calls.append(host)
        address = PUBLIC if len(calls) == 1 else target
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]
    def make_socket(*args):
        sock = WireSocket(attempts, response=(
            b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n"
            b"Location: http://redirect.example/feed\r\n\r\n"))
        sockets.append(sock)
        return sock
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    with requests.Session() as session:
        session.trust_env = False
        with pytest.raises(IngestionError, match="unsafe_destination"):
            with http_response(session, "GET", "http://feed.example/rss", SETTINGS):
                pass
    assert calls == ["feed.example", "redirect.example"]
    assert attempts == [(PUBLIC, 80)] and sockets[0].closed


def test_public_redirect_connects_to_each_separately_vetted_address(monkeypatch):
    calls, attempts, sockets = [], [], []
    def resolve(host, port, **kwargs):
        calls.append(host)
        address = PUBLIC if host == "feed.example" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    def make_socket(*args):
        response = (b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n"
                    b"Location: http://redirect.example/feed\r\n\r\n") if not sockets else None
        sock = WireSocket(attempts, response=response)
        sockets.append(sock)
        return sock
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    with requests.Session() as session:
        with http_response(session, "GET", "http://feed.example/rss", SETTINGS) as response:
            assert response.content == b"feed"
            assert response.url == "http://redirect.example/feed"
    assert calls == ["feed.example", "redirect.example"]
    assert attempts == [(PUBLIC, 80), ("1.1.1.1", 80)]
    assert b"Host: redirect.example\r\n" in sockets[1].sent
    assert all(sock.closed for sock in sockets)


def test_certificate_failure_closes_socket_without_retry_or_http_fallback(monkeypatch):
    calls = rebinding_dns(monkeypatch)
    attempts, sockets = [], []
    def make_socket(*args):
        sock = WireSocket(attempts)
        sockets.append(sock)
        return sock
    def bad_certificate(**kwargs):
        raise ssl.SSLCertVerificationError("secret incorrect hostname certificate")
    monkeypatch.setattr(socket, "socket", make_socket)
    monkeypatch.setattr(urllib3.connection, "_ssl_wrap_socket_and_match_hostname", bad_certificate)
    with requests.Session() as session:
        session.trust_env = False
        with pytest.raises(IngestionError, match="http_failed") as caught:
            with http_response(session, "GET", "https://feed.example/rss", SETTINGS):
                pass
    assert attempts == [(PUBLIC, 443)] and calls == ["feed.example"]
    assert len(sockets) == 1 and sockets[0].closed
    assert "secret" not in str(caught.value) and caught.value.__cause__ is None


def test_ipv6_connects_directly_to_validated_sockaddr_without_resolution(monkeypatch):
    address = "2606:4700:4700::1111"
    attempts, families = [], []
    def no_dns(*args, **kwargs):
        pytest.fail("numeric IPv6 destinations must not be resolved")
    def make_socket(family, kind):
        families.append(family)
        return WireSocket(attempts)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket, "socket", make_socket)
    with requests.Session() as session:
        with http_response(session, "GET", f"http://[{address}]/rss", SETTINGS) as response:
            assert response.content == b"feed"
    assert attempts == [(address, 80, 0, 0)] and families == [socket.AF_INET6]


@pytest.mark.parametrize("address", ["100.64.0.1", "198.18.0.1", "192.0.2.1", "240.0.0.1",
    "2001:db8::1", "ff0e::1", "::ffff:169.254.169.254", "2606:4700:4700::1111%lo"])
def test_reserved_shared_and_scoped_addresses_fail_before_socket(monkeypatch, address):
    def no_socket(*args, **kwargs):
        pytest.fail("unsafe destination reached socket creation")
    monkeypatch.setattr(socket, "socket", no_socket)
    host = f"[{address}]" if ":" in address else address
    with requests.Session() as session:
        with pytest.raises(IngestionError, match="unsafe_destination"):
            with http_response(session, "GET", f"http://{host}/rss", SETTINGS):
                pass

