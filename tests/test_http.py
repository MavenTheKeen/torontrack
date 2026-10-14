"""No network requests: exercise transport and response failures with MockTransport."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from transit.http import FeedClient, FetchError, retry_delay

URL = "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json"
PAYLOAD = {"version": "3.0", "data": {"stations": []}}


@pytest.fixture
def client_factory():
    clients = []

    def create(responses, **kwargs):
        pending = iter(responses)
        requests = []
        sleeps = []

        def handler(request):
            requests.append(request)
            response = next(pending)
            if isinstance(response, Exception):
                raise response
            return response

        transport = httpx.MockTransport(handler)
        client = FeedClient(client=httpx.Client(transport=transport), sleep=sleeps.append, **kwargs)
        clients.append(client)
        return client, requests, sleeps

    yield create
    for client in clients:
        client.close()


def test_success_makes_one_request(client_factory):
    client, requests, sleeps = client_factory([httpx.Response(200, json=PAYLOAD)])
    assert client.fetch(URL) == PAYLOAD
    assert len(requests) == 1
    assert str(requests[0].url) == URL
    assert sleeps == []


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")])
def test_transport_failure_is_retried_then_recovers(client_factory, failure):
    client, requests, sleeps = client_factory([failure, httpx.Response(200, json=PAYLOAD)])
    assert client.fetch(URL) == PAYLOAD
    assert len(requests) == 2
    assert sleeps == [1.0]


def test_timeouts_exhaust_retry_budget_visibly(client_factory):
    client, requests, sleeps = client_factory([httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(FetchError, match="transport failed after 3 attempts"):
        client.fetch(URL)
    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_status_recovers(client_factory, status):
    client, requests, sleeps = client_factory(
        [httpx.Response(status), httpx.Response(200, json=PAYLOAD)]
    )
    assert client.fetch(URL) == PAYLOAD
    assert len(requests) == 2
    assert sleeps == [1.0]


def test_transient_responses_exhaust_retry_budget(client_factory):
    client, requests, sleeps = client_factory([httpx.Response(503)] * 3)
    with pytest.raises(FetchError, match="503: retries exhausted"):
        client.fetch(URL)
    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]


def test_retry_after_seconds_is_respected(client_factory):
    client, requests, sleeps = client_factory(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=PAYLOAD),
        ]
    )
    assert client.fetch(URL) == PAYLOAD
    assert len(requests) == 2
    assert sleeps == [7.0]


def test_retry_after_http_date_is_respected(client_factory, monkeypatch):
    now = datetime(2026, 10, 9, 10, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr("transit.http.datetime", FrozenDatetime)
    client, requests, sleeps = client_factory(
        [
            httpx.Response(
                429, headers={"Retry-After": format_datetime(now + timedelta(seconds=13))}
            ),
            httpx.Response(200, json=PAYLOAD),
        ]
    )
    assert client.fetch(URL) == PAYLOAD
    assert len(requests) == 2
    assert sleeps == [13.0]


def test_large_retry_after_fails_instead_of_retrying_early(client_factory):
    client, requests, sleeps = client_factory([httpx.Response(429, headers={"Retry-After": "300"})])
    with pytest.raises(FetchError, match="no early retry"):
        client.fetch(URL)
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("header", [None, "not a date", "-9", "0"])
def test_invalid_or_short_retry_after_keeps_backoff(header):
    assert retry_delay(header, attempt=2) == 4.0


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422])
def test_nonretryable_response_fails_immediately(client_factory, status):
    client, requests, sleeps = client_factory([httpx.Response(status)])
    with pytest.raises(FetchError, match=f"{status}: non-retryable"):
        client.fetch(URL)
    assert len(requests) == 1
    assert sleeps == []


def test_malformed_json_is_not_replaced_by_empty_data(client_factory):
    client, requests, sleeps = client_factory([httpx.Response(200, content=b"{broken")])
    with pytest.raises(FetchError, match="invalid JSON"):
        client.fetch(URL)
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("payload", [[], None, "not an object", 4])
def test_nonobject_json_is_rejected(client_factory, payload):
    # httpx treats json=None as no content, so use explicit JSON bytes for null.
    response = (
        httpx.Response(200, content=b"null")
        if payload is None
        else httpx.Response(200, json=payload)
    )
    client, requests, sleeps = client_factory([response])
    with pytest.raises(FetchError, match="root must be an object"):
        client.fetch(URL)
    assert len(requests) == 1
    assert sleeps == []


def test_one_attempt_disables_retry_without_sleep(client_factory):
    client, requests, sleeps = client_factory([httpx.Response(503)], attempts=1)
    with pytest.raises(FetchError, match="retries exhausted"):
        client.fetch(URL)
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("attempts", [0, 6])
def test_retry_budget_must_be_bounded(attempts):
    with pytest.raises(ValueError, match="between 1 and 5"):
        FeedClient(attempts=attempts)


def test_default_client_has_explicit_timeouts_and_identification():
    client = FeedClient()
    try:
        assert client.client.timeout.connect == 5.0
        assert client.client.timeout.read == 20.0
        assert "Torontrack" in client.client.headers["User-Agent"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://toronto.publicbikesystem.net/gbfs.json",
        "https://127.0.0.1/",
        "https://example.test/",
        "https://toronto.publicbikesystem.net.evil.test/",
        "https://user:password@toronto.publicbikesystem.net/",
        "https://toronto.publicbikesystem.net:8080/",
    ],
)
def test_untrusted_destination_rejected_before_request(client_factory, url):
    client, requests, sleeps = client_factory([])
    with pytest.raises(FetchError, match="official Toronto HTTPS host"):
        client.fetch(url)
    assert requests == []


def test_redirect_cannot_reach_internal_service(client_factory):
    client, requests, sleeps = client_factory(
        [httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/"})]
    )
    client.client.follow_redirects = True
    with pytest.raises(FetchError, match="302: non-retryable"):
        client.fetch(URL)
    assert len(requests) == 1


def test_oversized_response_is_rejected(client_factory):
    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(161):
                yield b" " * 65536

    client, requests, sleeps = client_factory([httpx.Response(200, stream=LargeStream())])
    with pytest.raises(FetchError, match="10 MiB"):
        client.fetch(URL)
    assert len(requests) == 1


@pytest.mark.parametrize("header", ["inf", "nan", "-inf"])
def test_nonfinite_retry_after_uses_backoff(header):
    assert retry_delay(header, 2) == 4.0


def test_a_large_body_can_stream_to_a_file(client_factory, tmp_path):
    client, _, _ = client_factory([httpx.Response(200, content=b"x" * 200_000)])
    with open(tmp_path / "body", "w+b") as sink:
        assert client.fetch_bytes(URL, sink=sink) == b""
    assert (tmp_path / "body").stat().st_size == 200_000
