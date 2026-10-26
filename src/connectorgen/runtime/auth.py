from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..errors import ConnectorError
from ..ir import AuthScheme

if TYPE_CHECKING:
    from .oauth import OAuth2Session


@dataclass
class Credentials:
    """Secrets for calling an API. Supply whichever the API's auth scheme needs.

    `token` is a static bearer/OAuth access token. For OAuth with automatic
    refresh, pass `oauth=OAuth2Session(...)` instead.
    """

    api_key: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    oauth: OAuth2Session | None = None

    @classmethod
    def from_env(cls, prefix: str = "CONNECTORGEN_") -> Credentials:
        return cls(
            api_key=os.environ.get(f"{prefix}API_KEY"),
            token=os.environ.get(f"{prefix}TOKEN"),
            username=os.environ.get(f"{prefix}USERNAME"),
            password=os.environ.get(f"{prefix}PASSWORD"),
        )

    def __repr__(self) -> str:  # never print secrets
        present = [n for n in ("api_key", "token", "username", "password", "oauth")
                   if getattr(self, n) is not None]
        return f"Credentials(<set: {', '.join(present) or 'none'}>)"

    __str__ = __repr__

    def satisfies(self, scheme: AuthScheme) -> bool:
        if scheme.type == "api_key":
            return bool(self.api_key and scheme.param_name)
        if scheme.type == "oauth2":
            return bool(self.token or self.oauth)
        if scheme.type == "bearer":
            return bool(self.token)
        if scheme.type == "basic":
            return self.username is not None and self.password is not None
        return False


def pick_scheme(schemes: list[AuthScheme], creds: Credentials | None,
                preferred: str | None = None) -> AuthScheme | None:
    if preferred:
        for s in schemes:
            if s.id == preferred:
                if creds is None or not creds.satisfies(s):
                    raise ConnectorError(f"Credentials don't satisfy auth scheme '{preferred}'.")
                return s
        raise ConnectorError(f"No auth scheme with id '{preferred}'.")
    if creds is None:
        return None
    return next((s for s in schemes if creds.satisfies(s)), None)


def apply_auth(scheme: AuthScheme | None, creds: Credentials | None,
               headers: dict[str, str], query: dict[str, object],
               cookies: dict[str, str]) -> None:
    if scheme is None or creds is None:
        return
    if scheme.type == "api_key":
        assert scheme.param_name and creds.api_key
        if scheme.location == "query":
            query[scheme.param_name] = creds.api_key
        elif scheme.location == "cookie":
            cookies[scheme.param_name] = creds.api_key
        else:
            headers[scheme.param_name] = creds.api_key
    elif scheme.type == "oauth2" and creds.oauth is not None:
        headers["Authorization"] = f"Bearer {creds.oauth.access_token()}"
    elif scheme.type in ("bearer", "oauth2"):
        headers["Authorization"] = f"Bearer {creds.token}"
    elif scheme.type == "basic":
        raw = f"{creds.username}:{creds.password}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
