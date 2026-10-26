import httpx
import pytest
import respx

from connectorgen import (
    ActionHTTPError,
    ConnectorClient,
    ConnectorError,
    Credentials,
    InputValidationError,
)

BASE = "https://us.api.petstore.test/v1"


@pytest.fixture
def client(petstore):
    with ConnectorClient(petstore, Credentials(api_key="secret")) as c:
        yield c


@respx.mock
def test_get_with_path_param_and_api_key(client):
    route = respx.get(f"{BASE}/pets/42").respond(json={"id": 42, "name": "Rex"})
    assert client.execute("get_pet", petId=42) == {"id": 42, "name": "Rex"}
    assert route.calls.last.request.headers["X-API-Key"] == "secret"


@respx.mock
def test_bearer_auth_selected_from_token(petstore):
    route = respx.get(f"{BASE}/pets/1").respond(json={})
    with ConnectorClient(petstore, Credentials(token="tok")) as c:
        c.execute("get_pet", petId=1)
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_post_json_body(client):
    route = respx.post(f"{BASE}/pets").respond(201, json={"id": 1, "name": "Rex"})
    client.execute("create_pet", body={"name": "Rex", "tag": None})
    assert route.calls.last.request.content == b'{"name":"Rex","tag":null}'


def test_validation_errors(client):
    with pytest.raises(InputValidationError, match="'body' is a required property"):
        client.execute("create_pet")
    with pytest.raises(InputValidationError, match="'name' is a required property"):
        client.execute("create_pet", body={"tag": "x"})
    with pytest.raises(InputValidationError, match="not one of"):
        client.execute("list_pets", status="lost")
    with pytest.raises(InputValidationError, match="Additional properties"):
        client.execute("get_pet", petId=1, typo=True)
    with pytest.raises(InputValidationError, match="is not of type 'integer'"):
        client.execute("get_pet", petId="abc")


@respx.mock
def test_http_error_raised(client):
    respx.get(f"{BASE}/pets/9").respond(404, json={"error": "not found"})
    with pytest.raises(ActionHTTPError) as exc:
        client.execute("get_pet", petId=9)
    assert exc.value.status_code == 404
    assert exc.value.body == {"error": "not found"}


@respx.mock
def test_empty_response_is_none(client):
    respx.delete(f"{BASE}/pets/3").respond(204)
    assert client.execute("delete_pet", petId=3) is None


@respx.mock
def test_cursor_pagination(client):
    def handler(request: httpx.Request):
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"data": [{"id": 1}, {"id": 2}], "meta": {"next_cursor": "c2"}})
        assert cursor == "c2"
        return httpx.Response(200, json={"data": [{"id": 3}], "meta": {"next_cursor": None}})

    route = respx.get(f"{BASE}/pets").mock(side_effect=handler)
    items = list(client.paginate("list_pets", page_size=2))
    assert [i["id"] for i in items] == [1, 2, 3]
    assert route.calls[0].request.url.params["limit"] == "2"


@respx.mock
def test_offset_pagination_stops_on_short_page(client):
    def handler(request: httpx.Request):
        offset = int(request.url.params.get("offset", 0))
        rows = list(range(offset, min(offset + 2, 5)))
        return httpx.Response(200, json=[{"n": n} for n in rows])

    route = respx.get(f"{BASE}/orders").mock(side_effect=handler)
    items = list(client.paginate("get_orders", limit=2))
    assert [i["n"] for i in items] == [0, 1, 2, 3, 4]
    assert route.call_count == 3


@respx.mock
def test_item_cursor_with_has_more(client):
    def handler(request: httpx.Request):
        after = request.url.params.get("starting_after")
        if after is None:
            return httpx.Response(200, json={"data": [{"id": "cus_1"}, {"id": "cus_2"}], "has_more": True})
        assert after == "cus_2"
        return httpx.Response(200, json={"data": [{"id": "cus_3"}], "has_more": False})

    respx.get(f"{BASE}/customers").mock(side_effect=handler)
    assert [c["id"] for c in client.paginate("list_customers")] == ["cus_1", "cus_2", "cus_3"]


@respx.mock
def test_url_cursor_followed_with_auth(client):
    respx.get(f"{BASE}/events", params={"page_token": "p2"}).respond(
        json={"events": [{"e": 2}], "next": None})
    first = respx.get(f"{BASE}/events").respond(
        json={"events": [{"e": 1}], "next": f"{BASE}/events?page_token=p2"})
    items = list(client.paginate("list_events"))
    assert [i["e"] for i in items] == [1, 2]
    assert all(c.request.headers["X-API-Key"] == "secret" for c in respx.calls)
    assert first.called


@respx.mock
def test_max_pages(client):
    respx.get(f"{BASE}/pets").respond(json={"data": [{"id": 1}], "meta": {"next_cursor": "same"}})
    assert len(list(client.paginate("list_pets", max_pages=1))) == 1


@respx.mock
def test_repeated_cursor_stops(client):
    respx.get(f"{BASE}/pets").respond(json={"data": [{"id": 1}], "meta": {"next_cursor": "same"}})
    assert len(list(client.paginate("list_pets"))) == 2  # second page repeats cursor -> stop


def test_unknown_action(client):
    with pytest.raises(KeyError, match="Available: list_pets"):
        client.execute("nope")


def test_missing_base_url(petstore):
    petstore.base_url = ""
    with pytest.raises(ConnectorError, match="base_url"):
        ConnectorClient(petstore)


@respx.mock
def test_link_header_pagination(client):
    respx.get(f"{BASE}/orders", params={"offset": "2"}).respond(
        json=[{"n": 3}])
    first = respx.get(f"{BASE}/orders").respond(
        json=[{"n": 1}, {"n": 2}],
        headers={"Link": f'<{BASE}/orders?offset=2>; rel="next", <{BASE}/orders?offset=0>; rel="first"'})
    items = list(client.paginate("get_orders"))
    assert [i["n"] for i in items] == [1, 2, 3]
    assert first.call_count == 1


def test_nested_form_body_uses_bracket_notation():
    from connectorgen import Connector
    from connectorgen.ir import Action

    c = Connector(name="t", base_url="https://x.test", actions=[Action(
        name="create", method="POST", path="/c", body_encoding="form",
        body_schema={"type": "object"})])
    req = ConnectorClient(c).build_request(c.actions[0], {"body": {
        "email": "a@b.co", "metadata": {"plan": "pro"}, "tags": ["x", "y"],
        "live": True, "skip": None}})
    assert req.content.decode() == (
        "email=a%40b.co&metadata%5Bplan%5D=pro&tags%5B0%5D=x&tags%5B1%5D=y&live=true")


# ------------------------------------------------------------------ regressions from the system test


def _secured():
    from connectorgen import connector_from_openapi
    return connector_from_openapi({
        "openapi": "3.0.3", "info": {"title": "S", "version": "1"},
        "servers": [{"url": "https://s.test"}],
        "components": {"securitySchemes": {
            "key": {"type": "apiKey", "in": "header", "name": "X-Key"},
            "tok": {"type": "http", "scheme": "bearer"}}},
        "security": [{"key": []}, {"tok": []}],
        "paths": {
            "/a": {"get": {"operationId": "a", "responses": {"200": {"description": "ok"}}}},
            "/me": {"get": {"operationId": "me", "security": [{"tok": []}],
                            "responses": {"200": {"description": "ok"}}}},
            "/open": {"get": {"operationId": "open", "security": [],
                              "responses": {"200": {"description": "ok"}}}}}})


@respx.mock
def test_per_operation_security_picks_the_right_credentials():
    c = _secured()
    assert (c.action("a").security, c.action("me").security, c.action("open").security) == \
        (["key", "tok"], ["tok"], [])
    a = respx.get("https://s.test/a").respond(json={})
    me = respx.get("https://s.test/me").respond(json={})
    opened = respx.get("https://s.test/open").respond(json={})
    with ConnectorClient(c, Credentials(api_key="k", token="t")) as client:
        client.execute("a"), client.execute("me"), client.execute("open")
    assert a.calls.last.request.headers["X-Key"] == "k"
    assert me.calls.last.request.headers["Authorization"] == "Bearer t"     # not the API key
    assert "X-Key" not in opened.calls.last.request.headers                 # anonymous endpoint
    with ConnectorClient(c, Credentials(api_key="k")) as client:
        with pytest.raises(ConnectorError, match="'me' requires tok credentials"):
            client.execute("me")                                            # explained, not sent


@respx.mock
def test_misconfigured_pagination_returns_one_page_instead_of_looping():
    from connectorgen import Connector
    from connectorgen.ir import Action, Pagination
    c = Connector(name="t", base_url="https://x.test", actions=[Action(
        name="list", method="GET", path="/items", output_schema={"type": "array"},
        pagination=Pagination(style="offset"))])
    route = respx.get("https://x.test/items").respond(json=[{"id": 1}, {"id": 2}])
    for validate in (True, False):
        with ConnectorClient(c, validate=validate) as client:
            assert len(list(client.paginate("list"))) == 2
    assert route.call_count == 2   # one request per run
