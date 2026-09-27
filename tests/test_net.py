"""The HTTP layer under PyPI and the model APIs: retries, readable errors and the size cap."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

import pytest

from since_cutoff import __version__, net
from tests.conftest import Reply


def test_a_request_names_the_tool_and_returns_the_body(http_server) -> None:
    http_server.routes["/x"] = [Reply(body=b"hello")]
    assert net.request(f"{http_server.url}/x") == b"hello"
    assert http_server.seen[0].headers["user-agent"] == net.USER_AGENT
    assert net.USER_AGENT.startswith(f"since-cutoff/{__version__} (+https://github.com/")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_transient_statuses_are_retried(http_server, slept, status) -> None:
    http_server.routes["/x"] = [Reply(status, b"busy"), Reply(body=b"ok")]
    assert net.request(f"{http_server.url}/x") == b"ok"
    assert len(http_server.seen) == 2 and slept == [1.0]


def test_retries_back_off_and_give_up_with_the_last_status(http_server, slept) -> None:
    http_server.routes["/x"] = [Reply(503, b"<h1>Service\n   Unavailable</h1>")]
    with pytest.raises(net.HTTPError) as info:
        net.request(f"{http_server.url}/x", retries=3)
    assert info.value.status == 503
    assert info.value.reason == "HTTP 503: <h1>Service Unavailable</h1>"
    assert len(http_server.seen) == 3 and slept == [1.0, 2.0]  # no wait after the last try


@pytest.mark.parametrize(
    ("retry_after", "delay"),
    [("7", 7.0), ("600", 30.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0), ("1.5", 1.0)],
    ids=["seconds", "capped", "http-date", "fraction"],
)
def test_retry_after_sets_the_delay_up_to_30_seconds(
    http_server, slept, retry_after, delay
) -> None:
    http_server.routes["/x"] = [Reply(429, headers={"Retry-After": retry_after}), Reply(body=b"ok")]
    assert net.request(f"{http_server.url}/x") == b"ok"
    assert slept == [delay]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_statuses_fail_at_once_with_the_body(http_server, slept, status) -> None:
    url = f"{http_server.url}/x"
    http_server.routes["/x"] = [Reply(status, {"error": "nope"}), Reply(body=b"never sent")]
    with pytest.raises(net.HTTPError) as info:
        net.request(url)
    assert (info.value.status, info.value.body) == (status, '{"error": "nope"}')
    assert str(info.value) == f'{url}: HTTP {status}: {{"error": "nope"}}'
    assert len(http_server.seen) == 1 and slept == []


def test_a_connection_cut_mid_body_is_retried(http_server, slept) -> None:
    http_server.routes["/x"] = [Reply(body=b"x" * 1000, cut=10), Reply(body=b"whole")]
    assert net.request(f"{http_server.url}/x") == b"whole"
    assert slept == [1.0]


def test_a_server_that_does_not_answer_in_time_is_retried(http_server, monkeypatch, slept) -> None:
    # A real socket timeout on the first try. The retry gets a generous one, so that a busy
    # machine cannot make it time out too; the timeouts asked for are what the test checks.
    http_server.routes["/x"] = [Reply(body=b"late", delay=5.0), Reply(body=b"ok")]
    real, asked = urllib.request.urlopen, []

    def urlopen(req: urllib.request.Request, timeout: float) -> object:
        asked.append(timeout)
        return real(req, timeout=0.3 if len(asked) == 1 else 30.0)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert net.request(f"{http_server.url}/x", timeout=12.5) == b"ok"
    assert asked == [12.5, 12.5] and len(http_server.seen) == 2 and slept == [1.0]


def test_network_errors_are_retried_then_reported_readably(monkeypatch, slept) -> None:
    urls: list[str] = []

    def refuse(req: urllib.request.Request, timeout: float) -> None:
        urls.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(net.HTTPError) as info:
        net.request("https://pypi.org/pypi/x/json")
    assert info.value.status is None
    assert info.value.reason == "network error ([Errno 10061] No connection could be made)"
    assert len(urls) == 3 and slept == [1.0, 2.0]


def test_a_tls_connection_dropped_mid_download_is_retried(http_server, monkeypatch, slept) -> None:
    """``ssl.SSLEOFError`` while reading a body is no URLError (urllib wraps only what happens
    before the response): it ended the whole scan with a traceback."""
    http_server.routes["/x"] = [Reply(body=b"ok")]
    real = urllib.request.urlopen
    calls: list[int] = []

    class Dropped:
        def __enter__(self) -> Dropped:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def read(self, amount: int | None = None) -> bytes:
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")

    def flaky(req: urllib.request.Request, timeout: float) -> object:
        calls.append(1)
        return Dropped() if len(calls) == 1 else real(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert net.request(f"{http_server.url}/x") == b"ok"
    assert len(calls) == 2 and slept == [1.0]


def test_a_response_above_max_bytes_is_refused_and_not_retried(http_server, slept) -> None:
    http_server.routes["/fits"] = [Reply(body=b"x" * 99)]
    http_server.routes["/big"] = [Reply(body=b"x" * 5000)]
    assert net.request(f"{http_server.url}/fits", max_bytes=100) == b"x" * 99
    with pytest.raises(net.ResponseTooLarge) as info:
        net.request(f"{http_server.url}/big", max_bytes=100)
    assert info.value.reason == "response of 100 bytes or more"  # not a "network error"
    assert http_server.paths() == ["/fits", "/big"] and slept == []


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (
            None,
            "<urlopen error [Errno 11001] getaddrinfo failed>",
            "network error ([Errno 11001] getaddrinfo failed)",
        ),
        (None, "", "network error (no response)"),
        (502, "", "HTTP 502"),
        (500, "Traceback:\n" + "x" * 300, "HTTP 500: Traceback: " + "x" * 189),
    ],
    ids=["urlopen-repr", "empty", "no-body", "long-body"],
)
def test_http_errors_read_as_one_short_line(status, body, reason) -> None:
    exc = net.HTTPError("https://pypi.org/pypi/x/json", status, body)
    assert exc.reason == reason
    assert str(exc) == f"https://pypi.org/pypi/x/json: {reason}"


def test_json_helpers_send_and_parse_utf8_json(http_server) -> None:
    http_server.routes["/j"] = [Reply(body={"a": [1, "é"]})]
    assert net.get_json(f"{http_server.url}/j") == {"a": [1, "é"]}
    assert net.post_json(f"{http_server.url}/j", {"q": "ü"}, headers={"X-Key": "k"}) == {
        "a": [1, "é"]
    }
    post = http_server.seen[1]
    assert post.method == "POST" and json.loads(post.body) == {"q": "ü"}
    assert post.headers["content-type"] == "application/json" and post.headers["x-key"] == "k"
