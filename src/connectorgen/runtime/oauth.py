"""OAuth2: authorization code (with PKCE), client credentials, and refresh.

    session = OAuth2Session(flow, client_id="...", client_secret="...",
                            store=FileTokenStore(".connectorgen_tokens/acme.json"))
    run_loopback_flow(session)          # opens a browser once, saves the token
    ConnectorClient(c, Credentials(oauth=session))   # refreshes automatically

Tokens are refreshed shortly before they expire, and once more if the API
answers 401 (tokens can be revoked early). Token files are written atomically
with owner-only permissions; tokens are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..errors import ConnectorError
from ..ir import OAuthFlow


class OAuthError(ConnectorError):
    """Token endpoint refused, flow misconfigured, or no usable token."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    token_type: str = "Bearer"
    scope: str | None = None

    def expired(self, skew: float = 60.0, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) >= self.expires_at - skew

    @classmethod
    def from_response(cls, data: dict[str, Any], now: float,
                      previous: TokenSet | None = None) -> TokenSet:
        if not data.get("access_token"):
            raise OAuthError("Token response had no access_token.")
        expires_in = data.get("expires_in")
        return cls(
            access_token=data["access_token"],
            # Providers may omit refresh_token on refresh; keep the old one then.
            refresh_token=data.get("refresh_token") or (previous.refresh_token if previous else None),
            expires_at=now + float(expires_in) if expires_in else None,
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
        )

    def __repr__(self) -> str:  # never print secrets
        return f"TokenSet(expires_at={self.expires_at}, has_refresh={bool(self.refresh_token)})"


class TokenStore(Protocol):
    def load(self) -> TokenSet | None: ...
    def save(self, token: TokenSet) -> None: ...


class MemoryTokenStore:
    def __init__(self, token: TokenSet | None = None):
        self.token = token

    def load(self) -> TokenSet | None:
        return self.token

    def save(self, token: TokenSet) -> None:
        self.token = token


class FileTokenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        return TokenSet(**json.loads(self.path.read_text()))

    def save(self, token: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # pragma: no cover - non-POSIX
            pass
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(token), f)
        os.replace(tmp, self.path)


@dataclass(repr=False)
class AuthRequest:
    url: str
    state: str
    code_verifier: str
    redirect_uri: str

    def __repr__(self) -> str:  # the PKCE verifier and state are secrets until the flow ends
        return f"AuthRequest(redirect_uri={self.redirect_uri!r})"


class OAuth2Session:
    def __init__(
        self,
        flow: OAuthFlow,
        client_id: str,
        client_secret: str | None = None,
        *,
        store: TokenStore | None = None,
        scopes: list[str] | None = None,
        client_auth: Literal["body", "basic"] = "body",
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.flow = flow
        self.client_id = client_id
        self.client_secret = client_secret
        self.store = store or MemoryTokenStore()
        self.scopes = scopes if scopes is not None else list(flow.scopes)
        self.client_auth = client_auth
        self.http = http or httpx.Client(timeout=30)
        self.clock = clock
        self._lock = threading.Lock()
        self._token = self.store.load()

    @property
    def token(self) -> TokenSet | None:
        return self._token

    # -- authorization code ------------------------------------------------------

    def authorization_url(self, redirect_uri: str, *, scopes: list[str] | None = None,
                          extra: dict[str, str] | None = None) -> AuthRequest:
        if self.flow.flow != "authorization_code" or not self.flow.authorization_url:
            raise OAuthError("This flow has no authorization URL (not authorization_code).")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()) \
            .rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        chosen = scopes if scopes is not None else self.scopes
        if chosen:
            params["scope"] = " ".join(chosen)
        params.update(extra or {})
        sep = "&" if "?" in self.flow.authorization_url else "?"
        return AuthRequest(self.flow.authorization_url + sep + urlencode(params), state,
                           verifier, redirect_uri)

    def exchange_code(self, code: str, request: AuthRequest) -> TokenSet:
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": request.redirect_uri,
            "code_verifier": request.code_verifier,
        })

    # -- client credentials / refresh --------------------------------------------

    def fetch_client_credentials(self) -> TokenSet:
        data = {"grant_type": "client_credentials"}
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
        return self._token_request(data)

    def refresh(self) -> TokenSet:
        if not (self._token and self._token.refresh_token):
            if self.flow.flow == "client_credentials":
                return self.fetch_client_credentials()
            raise OAuthError("Token expired and there's no refresh token; sign in again "
                             "(connectorgen auth).")
        return self._token_request({"grant_type": "refresh_token",
                                    "refresh_token": self._token.refresh_token},
                                   url=self.flow.refresh_url or self.flow.token_url)

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if self._token is None:
                if self.flow.flow == "client_credentials":
                    self.fetch_client_credentials()
                else:
                    raise OAuthError("Not signed in yet: run the authorization flow first "
                                     "(connectorgen auth).")
            elif force_refresh or self._token.expired(now=self.clock()):
                self.refresh()
            assert self._token is not None
            return self._token.access_token

    def _token_request(self, data: dict[str, str], url: str | None = None) -> TokenSet:
        headers = {"Accept": "application/json"}
        payload = dict(data)
        if self.client_auth == "basic" and self.client_secret is not None:
            raw = f"{self.client_id}:{self.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            payload["client_id"] = self.client_id
            if self.client_secret is not None:
                payload["client_secret"] = self.client_secret
        try:
            resp = self.http.post(url or self.flow.token_url, data=payload, headers=headers)
        except httpx.HTTPError as e:
            raise OAuthError(f"Token endpoint unreachable: {type(e).__name__}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {k: v[0] for k, v in parse_qs(resp.text).items()}  # some return form-encoded
        if resp.is_error or "error" in body:
            err = body.get("error", f"HTTP {resp.status_code}")
            desc = body.get("error_description", "")
            raise OAuthError(f"Token request ({data['grant_type']}) failed: {err} {desc}".strip())
        token = TokenSet.from_response(body, self.clock(), self._token)
        self._token = token
        self.store.save(token)
        return token


# ------------------------------------------------------------------ loopback sign-in


class _LoopbackServer(HTTPServer):
    """HTTPServer without the reverse-DNS lookup (socket.getfqdn) it does on startup,
    which can stall for several seconds on macOS before the browser even opens."""

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def run_loopback_flow(
    session: OAuth2Session,
    *,
    port: int = 8765,
    scopes: list[str] | None = None,
    open_browser: bool = True,
    timeout: float = 300.0,
    on_url: Callable[[str], None] = print,
) -> TokenSet:
    """Interactive sign-in for CLIs: open the provider's consent page, catch the
    redirect on http://127.0.0.1:<port>/callback, exchange the code (with PKCE).
    Register that exact redirect URI with the provider first."""
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    request = session.authorization_url(redirect_uri, scopes=scopes)
    outcome: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if "error" in query:
                outcome["error"] = f"{query['error']} {query.get('error_description', '')}".strip()
            elif not hmac.compare_digest(query.get("state", ""), request.state):
                outcome["error"] = "state mismatch (possible CSRF); try again"
            elif not query.get("code"):
                outcome["error"] = "no authorization code in redirect"
            else:
                outcome["code"] = query["code"]
            ok = "code" in outcome
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if ok else "Sign-in failed. Check the terminal."
            self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

        def log_message(self, *args: Any) -> None:  # keep codes out of logs
            pass

    server = _LoopbackServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0
    try:
        on_url(request.url)
        if open_browser:
            webbrowser.open(request.url)
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in outcome:
        raise OAuthError(f"Sign-in failed: {outcome['error']}")
    if "code" not in outcome:
        raise OAuthError(f"Sign-in timed out after {timeout:.0f}s.")
    return session.exchange_code(outcome["code"], request)
