import base64
import hashlib
import socket
import stat
import sys
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from connectorgen import ConnectorClient, Credentials, connector_from_openapi
from connectorgen.ir import OAuthFlow
from connectorgen.runtime.oauth import (
    FileTokenStore,
    MemoryTokenStore,
    OAuth2Session,
    OAuthError,
    TokenSet,
    run_loopback_flow,
)

AUTH_CODE = OAuthFlow(flow="authorization_code", authorization_url="https://auth.acme.test/authorize",
                      token_url="https://auth.acme.test/token", scopes={"read": "Read", "write": "Write"})
CLIENT_CREDS = OAuthFlow(flow="client_credentials", token_url="https://auth.acme.test/token")

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Acme", "version": "1"},
    "servers": [{"url": "https://api.acme.test"}],
    "components": {"securitySchemes": {"oauth": {"type": "oauth2", "flows": {
        "authorizationCode": {"authorizationUrl": AUTH_CODE.authorization_url,
                              "tokenUrl": AUTH_CODE.token_url, "scopes": AUTH_CODE.scopes},
        "clientCredentials": {"tokenUrl": CLIENT_CREDS.token_url, "scopes": {}},
        "implicit": {"authorizationUrl": "https://x", "scopes": {}},
    }}}},
    "paths": {"/me": {"get": {"operationId": "getMe", "responses": {"200": {
        "description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}}}}},
}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def token_server(responses):
    """MockTransport token endpoint that records form posts."""
    seen = []

    def handler(request):
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()} |
                    {"_auth": request.headers.get("authorization")})
        return responses.pop(0) if responses else httpx.Response(500)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def tok(access, refresh=None, expires_in=3600):
    body = {"access_token": access, "token_type": "bearer", "expires_in": expires_in}
    if refresh:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


def test_flows_parsed_from_spec():
    c = connector_from_openapi(SPEC)
    flows = c.auth[0].oauth_flows
    assert [f.flow for f in flows] == ["authorization_code", "client_credentials"]  # implicit ignored
    assert flows[0].scopes == {"read": "Read", "write": "Write"}


def test_authorization_url_uses_pkce_s256():
    session = OAuth2Session(AUTH_CODE, "cid")
    req = session.authorization_url("http://127.0.0.1:8765/callback")
    q = {k: v[0] for k, v in parse_qs(urlparse(req.url).query).items()}
    assert q["response_type"] == "code" and q["client_id"] == "cid"
    assert q["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(req.code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert q["state"] == req.state and q["scope"] == "read write"


def test_exchange_then_refresh_before_expiry():
    clock = Clock()
    http, seen = token_server([tok("a1", "r1", 3600), tok("a2")])  # refresh omits refresh_token
    store = MemoryTokenStore()
    session = OAuth2Session(AUTH_CODE, "cid", "secret", store=store, http=http, clock=clock)
    req = session.authorization_url("http://127.0.0.1:1/callback")
    session.exchange_code("the-code", req)
    assert seen[0]["grant_type"] == "authorization_code"
    assert seen[0]["code_verifier"] == req.code_verifier and seen[0]["client_secret"] == "secret"
    assert session.access_token() == "a1"

    clock.t += 3600 - 30      # within the 60 s skew: refresh early
    assert session.access_token() == "a2"
    assert seen[1] == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "cid",
                       "client_secret": "secret", "_auth": None}
    assert store.load().refresh_token == "r1"   # kept when the provider omits it


def test_client_credentials_fetch_and_basic_auth():
    http, seen = token_server([tok("cc1", expires_in=60), tok("cc2")])
    clock = Clock()
    session = OAuth2Session(CLIENT_CREDS, "cid", "sec", client_auth="basic", http=http, clock=clock)
    assert session.access_token() == "cc1"          # fetched lazily
    assert seen[0]["_auth"] == "Basic " + base64.b64encode(b"cid:sec").decode()
    assert "client_secret" not in seen[0]
    clock.t += 61
    assert session.access_token() == "cc2"          # no refresh token: fetch again


def test_errors_are_clear():
    with pytest.raises(OAuthError, match="Not signed in"):
        OAuth2Session(AUTH_CODE, "cid").access_token()
    http, _ = token_server([httpx.Response(400, json={"error": "invalid_grant",
                                                      "error_description": "code expired"})])
    session = OAuth2Session(AUTH_CODE, "cid", http=http)
    with pytest.raises(OAuthError, match="invalid_grant code expired"):
        session.exchange_code("x", session.authorization_url("http://127.0.0.1:1/callback"))


def test_form_encoded_token_response():  # e.g. GitHub without Accept: application/json
    http, _ = token_server([httpx.Response(200, text="access_token=f1&token_type=bearer")])
    session = OAuth2Session(CLIENT_CREDS, "cid", http=http)
    assert session.fetch_client_credentials().access_token == "f1"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_file_store_is_owner_only_and_hides_secrets(tmp_path):
    path = tmp_path / "t" / "acme.json"
    FileTokenStore(path).save(TokenSet("secret-access", "secret-refresh", 123.0))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = FileTokenStore(path).load()
    assert loaded.access_token == "secret-access"
    assert "secret" not in repr(loaded)


@respx.mock
def test_client_refreshes_once_on_401():
    c = connector_from_openapi(SPEC)
    http, seen = token_server([tok("fresh")])
    session = OAuth2Session(AUTH_CODE, "cid", store=MemoryTokenStore(TokenSet("stale", "r1")),
                            http=http)
    route = respx.get("https://api.acme.test/me").mock(side_effect=lambda r: httpx.Response(
        200, json={"ok": True}) if r.headers["authorization"] == "Bearer fresh"
        else httpx.Response(401))
    with ConnectorClient(c, Credentials(oauth=session)) as client:
        assert client.execute("get_me") == {"ok": True}
    assert route.call_count == 2 and seen[0]["grant_type"] == "refresh_token"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("tamper", [False, True])
def test_loopback_flow_end_to_end(tamper):
    http, seen = token_server([tok("from-loopback", "r")])
    session = OAuth2Session(AUTH_CODE, "cid", http=http)
    port = _free_port()
    result, urls = {}, []

    def run():
        try:
            result["token"] = run_loopback_flow(session, port=port, open_browser=False,
                                                timeout=30, on_url=urls.append)
        except OAuthError as e:
            result["error"] = str(e)
        except Exception as e:  # surface anything else instead of a silent dead thread
            result["crash"] = f"{type(e).__name__}: {e}"

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(300):  # wait for the URL (server is listening by then)
        if urls or "crash" in result:
            break
        threading.Event().wait(0.05)
    assert urls, result.get("crash", "sign-in server didn't start within 15 s")
    state = parse_qs(urlparse(urls[0]).query)["state"][0]
    # play the browser: the provider redirects back with code + state
    resp = httpx.get(f"http://127.0.0.1:{port}/callback",
                     params={"code": "abc", "state": "forged" if tamper else state})
    thread.join(30)
    if tamper:
        assert resp.status_code == 400 and "state mismatch" in result["error"]
        assert seen == []   # never exchanged a code from a forged redirect
    else:
        assert resp.status_code == 200 and "Signed in" in resp.text
        assert result["token"].access_token == "from-loopback"
        assert seen[0]["code"] == "abc"
