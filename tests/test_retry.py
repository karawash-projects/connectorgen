import email.utils
import warnings

import httpx
import pytest
import respx

from connectorgen import (
    ActionHTTPError,
    ConnectionFailedError,
    ConnectorClient,
    Credentials,
    RateLimitError,
    ResponseSchemaWarning,
    ResponseValidationError,
    RetryPolicy,
)

BASE = "https://us.api.petstore.test/v1"
NOW = 1_800_000_000.0


class FakeSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep():
    return FakeSleep()


@pytest.fixture
def make_client(petstore, sleep):
    clients = []

    def make(**policy_kwargs):
        policy = RetryPolicy(sleep=sleep, clock=lambda: NOW, jitter=False, **policy_kwargs)
        c = ConnectorClient(petstore, Credentials(api_key="k"), retry=policy)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.close()


def responses(*items):
    """respx side_effect returning the given responses in order."""
    it = iter(items)
    return lambda request: next(it)


# ------------------------------------------------------------------- rate limits


@respx.mock
def test_429_retry_after_seconds(make_client, sleep):
    route = respx.get(f"{BASE}/pets/1").mock(side_effect=responses(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"id": 1})))
    assert make_client().execute("get_pet", petId=1) == {"id": 1}
    assert route.call_count == 2
    assert sleep.calls == [2.0]


@respx.mock
def test_429_retry_after_http_date(make_client, sleep):
    when = email.utils.formatdate(NOW + 5, usegmt=True)
    respx.get(f"{BASE}/pets/1").mock(side_effect=responses(
        httpx.Response(429, headers={"Retry-After": when}),
        httpx.Response(200, json={})))
    make_client().execute("get_pet", petId=1)
    assert sleep.calls == [5.0]


@respx.mock
def test_github_style_403_with_reset_epoch(make_client, sleep):
    respx.get(f"{BASE}/pets/1").mock(side_effect=responses(
        httpx.Response(403, headers={"X-RateLimit-Remaining": "0",
                                     "X-RateLimit-Reset": str(int(NOW + 7))}),
        httpx.Response(200, json={})))
    make_client().execute("get_pet", petId=1)
    assert sleep.calls == [7.0]


@respx.mock
def test_plain_403_is_not_retried(make_client, sleep):
    respx.get(f"{BASE}/pets/1").respond(403, json={"error": "forbidden"})
    with pytest.raises(ActionHTTPError) as exc:
        make_client().execute("get_pet", petId=1)
    assert not isinstance(exc.value, RateLimitError)
    assert sleep.calls == []


@respx.mock
def test_429_without_header_uses_exponential_backoff(make_client, sleep):
    respx.get(f"{BASE}/pets/1").mock(side_effect=responses(
        httpx.Response(429), httpx.Response(429), httpx.Response(200, json={})))
    make_client(backoff_base=0.5).execute("get_pet", petId=1)
    assert sleep.calls == [0.5, 1.0]


@respx.mock
def test_429_retried_even_for_post(make_client, sleep):
    route = respx.post(f"{BASE}/pets").mock(side_effect=responses(
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(201, json={"id": 1, "name": "Rex"})))
    make_client().execute("create_pet", body={"name": "Rex"})
    assert route.call_count == 2
    assert route.calls[1].request.content == route.calls[0].request.content


@respx.mock
def test_rate_limit_exhausted_raises_with_retry_after(make_client, sleep):
    respx.get(f"{BASE}/pets/1").respond(429, headers={"Retry-After": "3"})
    with pytest.raises(RateLimitError) as exc:
        make_client(max_attempts=3).execute("get_pet", petId=1)
    assert exc.value.retry_after == 3.0
    assert len(sleep.calls) == 2


@respx.mock
def test_wait_longer_than_max_wait_fails_fast(make_client, sleep):
    route = respx.get(f"{BASE}/pets/1").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(RateLimitError, match="retry after 3600s"):
        make_client(max_wait=60).execute("get_pet", petId=1)
    assert route.call_count == 1
    assert sleep.calls == []


# ------------------------------------------------------------------- server errors


@respx.mock
def test_503_retried_for_get(make_client, sleep):
    respx.get(f"{BASE}/pets/1").mock(side_effect=responses(
        httpx.Response(503), httpx.Response(200, json={"ok": True})))
    assert make_client().execute("get_pet", petId=1) == {"ok": True}


@respx.mock
def test_503_not_retried_for_post(make_client, sleep):
    route = respx.post(f"{BASE}/pets").respond(503)
    with pytest.raises(ActionHTTPError):
        make_client().execute("create_pet", body={"name": "Rex"})
    assert route.call_count == 1


@respx.mock
def test_503_retried_for_post_with_idempotency_key(petstore, sleep):
    route = respx.post(f"{BASE}/pets").mock(side_effect=responses(
        httpx.Response(503), httpx.Response(201, json={})))
    http = httpx.Client(headers={"Idempotency-Key": "abc"})
    with ConnectorClient(petstore, Credentials(api_key="k"), http=http,
                         retry=RetryPolicy(sleep=sleep, jitter=False)) as c:
        c.execute("create_pet", body={"name": "Rex"})
    assert route.call_count == 2
    http.close()


@respx.mock
def test_404_never_retried(make_client, sleep):
    route = respx.get(f"{BASE}/pets/1").respond(404)
    with pytest.raises(ActionHTTPError):
        make_client().execute("get_pet", petId=1)
    assert route.call_count == 1


# ------------------------------------------------------------------- network errors


@respx.mock
def test_connect_error_retried_then_succeeds(make_client, sleep):
    respx.post(f"{BASE}/pets").mock(side_effect=[
        httpx.ConnectError("refused"), httpx.Response(201, json={})])
    make_client().execute("create_pet", body={"name": "Rex"})  # safe: never reached server
    assert len(sleep.calls) == 1


@respx.mock
def test_read_timeout_not_retried_for_post(make_client, sleep):
    route = respx.post(f"{BASE}/pets").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ConnectionFailedError, match="ReadTimeout"):
        make_client().execute("create_pet", body={"name": "Rex"})
    assert route.call_count == 1


@respx.mock
def test_connection_failure_after_retries(make_client, sleep):
    route = respx.get(f"{BASE}/pets/1").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ConnectionFailedError):
        make_client(max_attempts=3).execute("get_pet", petId=1)
    assert route.call_count == 3


@respx.mock
def test_retries_disabled(petstore):
    route = respx.get(f"{BASE}/pets/1").respond(503)
    with ConnectorClient(petstore, retry=RetryPolicy.disabled()) as c:
        with pytest.raises(ActionHTTPError):
            c.execute("get_pet", petId=1)
    assert route.call_count == 1


@respx.mock
def test_pagination_retries_mid_stream(make_client, sleep):
    respx.get(f"{BASE}/pets").mock(side_effect=responses(
        httpx.Response(200, json={"data": [{"id": 1}], "meta": {"next_cursor": "c2"}}),
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"data": [{"id": 2}], "meta": {"next_cursor": None}})))
    assert [p["id"] for p in make_client().paginate("list_pets")] == [1, 2]
    assert sleep.calls == [1.0]


# ------------------------------------------------------------------- response validation


@respx.mock
def test_response_validation_modes(petstore):
    respx.get(f"{BASE}/pets/1").respond(json={"id": "not-an-int", "name": "Rex"})
    with ConnectorClient(petstore, validate_responses="raise") as c:
        with pytest.raises(ResponseValidationError, match="at 'id'"):
            c.execute("get_pet", petId=1)
    with ConnectorClient(petstore, validate_responses="warn") as c:
        with pytest.warns(ResponseSchemaWarning):
            assert c.execute("get_pet", petId=1)["id"] == "not-an-int"
    with ConnectorClient(petstore) as c, warnings.catch_warnings():
        warnings.simplefilter("error")
        c.execute("get_pet", petId=1)  # off by default


@respx.mock
def test_valid_response_passes_strict_validation(petstore):
    respx.get(f"{BASE}/pets/1").respond(json={"id": 1, "name": "Rex", "tag": None})
    with ConnectorClient(petstore, validate_responses="raise") as c:
        assert c.execute("get_pet", petId=1)["name"] == "Rex"
