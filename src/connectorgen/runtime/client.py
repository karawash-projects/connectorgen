"""Generic runtime: executes any Action described by a Connector."""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Iterator
from typing import Any, Literal
from urllib.parse import parse_qsl, quote

import httpx

from ..errors import (
    ActionHTTPError,
    ConnectionFailedError,
    ConnectorError,
    InputValidationError,
    RateLimitError,
    ResponseSchemaWarning,
    ResponseValidationError,
)
from ..ir import Action, Connector, pagination_problem
from ..limits import BoundedValidator, SchemaTooLarge
from .auth import Credentials, apply_auth, pick_scheme
from .retry import RetryPolicy, log
from .urls import install_log_redaction, redact_url, register_secret_param, same_origin

USER_AGENT = "connectorgen/0.9"
MAX_REDIRECTS = 10
# Headers kept when a redirect leaves the API's origin; everything else (Authorization,
# Cookie, X-API-Key and any other header that could carry a secret) is dropped.
_CROSS_ORIGIN_HEADERS = ("accept", "accept-encoding", "user-agent")


def get_path(data: Any, path: str | None) -> Any:
    """Read a dotted path like 'meta.next' from parsed JSON. None path returns data."""
    if not path:
        return data
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


class ConnectorClient:
    """Call a connector's actions.

    >>> client = ConnectorClient(connector, Credentials(api_key="..."))
    >>> pet = client.execute("get_pet", petId=42)
    >>> for pet in client.paginate("list_pets", limit=50): ...
    """

    def __init__(
        self,
        connector: Connector,
        credentials: Credentials | None = None,
        *,
        base_url: str | None = None,
        auth_scheme: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        validate: bool = True,
        retry: RetryPolicy | None = None,
        validate_responses: Literal["off", "warn", "raise"] = "off",
    ):
        """
        retry: defaults to RetryPolicy(); pass RetryPolicy.disabled() to turn retries off.
        validate_responses: check responses against each action's output schema.
            "warn" emits ResponseSchemaWarning, "raise" raises ResponseValidationError.
            Useful for spotting API drift; off by default because real-world specs
            are often looser than the APIs they describe.
        """
        self.connector = connector
        self.retry = retry if retry is not None else RetryPolicy()
        self.validate_responses = validate_responses
        self._output_validators: dict[str, BoundedValidator] = {}
        self.credentials = credentials
        self.base_url = (base_url or connector.base_url).rstrip("/")
        if not self.base_url:
            raise ConnectorError("No base URL: the spec has no servers; pass base_url=...")
        if re.search(r"\{[^}]+\}", self.base_url):
            raise ConnectorError(f"Base URL '{self.base_url}' has unfilled placeholders; pass "
                                 "base_url=... with the real values.")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConnectorError(f"Base URL '{self.base_url}' is relative (the spec didn't say "
                                 "which host); pass base_url='https://your-host"
                                 f"{self.base_url}'.")
        self.scheme = pick_scheme(connector.auth, credentials, auth_scheme)
        for s in connector.auth:
            if s.type == "api_key" and s.location == "query":
                register_secret_param(s.param_name)
        install_log_redaction()
        self.validate = validate
        self._owns_http = http is None
        # Redirects are followed by _send_following, which never carries credentials
        # to another origin (httpx only strips Authorization, not X-API-Key and friends).
        self.http = http or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self._validators: dict[str, BoundedValidator] = {}

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> ConnectorClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ----------------------------------------------------------------

    def execute(self, action: str | Action, inputs: dict[str, Any] | None = None,
                **kwargs: Any) -> Any:
        """Run one action and return the parsed response body."""
        act = self._resolve(action)
        merged = {**(inputs or {}), **kwargs}
        request = self.build_request(act, merged)
        return self._send(act, request)

    def paginate(self, action: str | Action, inputs: dict[str, Any] | None = None, *,
                 page_size: int | None = None, max_pages: int | None = None,
                 stats: dict[str, Any] | None = None, start_url: str | None = None,
                 **kwargs: Any) -> Iterator[Any]:
        """Yield items across all pages, following the action's pagination strategy.

        stats: if given, kept up to date with "pages" read, "params" (the inputs that
        fetched the current page, for resuming there later; None when the page came from
        a server-provided URL), "url" (the current page's URL) and "exhausted" (True once
        the last page was reached).
        start_url: begin at this page URL (e.g. a "url" saved earlier); must be on the API's
        origin, like any next-page link."""
        stats = stats if stats is not None else {}
        stats.update(pages=0, exhausted=False, params=None, url=None)
        act = self._resolve(action)
        p = act.pagination
        params = {**(inputs or {}), **kwargs}
        if page_size is not None:
            if not p.limit_param:
                raise ConnectorError(f"'{act.name}' has no page-size parameter.")
            params[p.limit_param] = page_size
        limit = params.get(p.limit_param) if p.limit_param else None
        broken = pagination_problem(p)
        if broken:
            log.warning("%s: pagination misconfigured (%s); returning the first page only",
                        act.name, broken)

        request = self.build_request(act, params)
        stats["params"] = dict(params)
        pages = 0
        seen_cursors: set[str] = set()
        using_links = False
        if start_url:
            request = self._follow_url(request, start_url, self.schemes_for(act))
            stats["params"] = None
            using_links = p.style != "cursor"
        while True:
            stats["url"] = str(request.url)
            data, response = self._send_raw(act, request)
            stats["pages"] = pages + 1
            items = get_path(data, p.items_path)
            if items is None:
                items = []
            if not isinstance(items, list):
                raise ConnectorError(
                    f"'{act.name}': expected a list at '{p.items_path or '(body)'}', "
                    f"got {type(items).__name__}."
                )
            stats["page_items"] = len(items)
            yield from items
            pages += 1

            if p.style == "none" or not items or broken:
                stats["exhausted"] = True
                return
            if p.has_more_path and get_path(data, p.has_more_path) is False:
                stats["exhausted"] = True
                return
            if max_pages is not None and pages >= max_pages:
                return

            # An RFC 8288 Link header beats any guessed strategy when present.
            try:
                link_next = response.links.get("next", {}).get("url")
                if link_next:
                    link_next = str(request.url.join(link_next))
            except (httpx.InvalidURL, UnicodeError, ValueError) as e:
                raise ConnectorError(f"'{act.name}': malformed next-page link "
                                     f"({type(e).__name__}).") from None
            if link_next:
                if link_next in seen_cursors:
                    stats["exhausted"] = True
                    return
                seen_cursors.add(link_next)
                using_links = True
                request = self._follow_url(request, link_next, self.schemes_for(act))
                stats["params"] = None
                continue
            if p.style == "link_header" or using_links:
                stats["exhausted"] = True
                return  # the API paginates via Link headers and there's no next page

            if p.style == "cursor":
                if p.cursor_from_item_field:
                    last = items[-1]
                    nxt = last.get(p.cursor_from_item_field) if isinstance(last, dict) else None
                else:
                    nxt = get_path(data, p.response_cursor_path)
                if not nxt or str(nxt) in seen_cursors:
                    stats["exhausted"] = True
                    return
                seen_cursors.add(str(nxt))
                if isinstance(nxt, str) and nxt.startswith(("http://", "https://")):
                    request = self._follow_url(request, nxt, self.schemes_for(act))  # "next" is a full URL
                    stats["params"] = None
                    continue
                params[p.request_param] = nxt  # type: ignore[index]
            else:
                if isinstance(limit, int) and len(items) < limit:
                    stats["exhausted"] = True
                    return  # short page = last page
                if p.style == "offset":
                    params[p.request_param] = int(params.get(p.request_param) or 0) + len(items)  # type: ignore[index]
                else:  # page
                    params[p.request_param] = int(params.get(p.request_param) or 1) + 1  # type: ignore[index]
            request = self.build_request(act, params)
            stats["params"] = dict(params)

    def build_request(self, action: Action, inputs: dict[str, Any]) -> httpx.Request:
        """Validate inputs and build (but don't send) the HTTP request."""
        if self.validate:
            self._validate(action, inputs)

        path, _, fixed_query = action.path.partition("?")
        # Fixed pairs written into the path template (/export?mode=csv) are sent as given.
        query: dict[str, Any] = dict(parse_qsl(fixed_query, keep_blank_values=True))
        headers: dict[str, str] = {"Accept": "application/json"}
        cookies: dict[str, str] = {}

        for p in action.params:
            if p.name not in inputs or inputs[p.name] is None:
                if p.location == "path":
                    raise InputValidationError(action.name, f"missing path parameter '{p.name}'")
                continue
            value = inputs[p.name]
            if p.location == "path":
                path = path.replace("{" + p.name + "}", _path_value(action, p, value))
            elif p.location == "query":
                query.update(_query_pairs(action, p, value))
            else:
                headers[p.sent_name] = _simple(action, p, value)

        schemes = self.schemes_for(action)
        if schemes is None:
            ids = [s for s in (action.security or [])] if action.security_sets is None else \
                [" + ".join(x) for x in action.security_sets if x]
            allowed = [s for s in self.connector.auth if s.id in (action.security or [])]
            if allowed and all(s.type == "unsupported" for s in allowed):
                raise ConnectorError(
                    f"'{action.name}' uses auth connectorgen can't perform yet "
                    f"({'; '.join(s.description for s in allowed)}).")
            raise ConnectorError(
                f"'{action.name}' requires {' or '.join(ids) or 'some'} credentials, "
                "which weren't provided.")
        for scheme in schemes:
            apply_auth(scheme, self.credentials, headers, query, cookies)

        kwargs: dict[str, Any] = {"params": query, "headers": headers}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = inputs.get("body")
        if body is not None and action.body_schema is not None:
            if action.body_encoding == "form":
                if isinstance(body, (str, bytes)):  # already form-encoded
                    kwargs["content"] = body
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                elif isinstance(body, list):
                    kwargs["data"] = _flatten_form({str(i): v for i, v in enumerate(body)})
                else:
                    kwargs["data"] = _flatten_form(body) if isinstance(body, dict) else body
            else:
                kwargs["json"] = body
        url = self.base_url + path
        self._check_target(action, url)
        try:
            return self.http.build_request(action.method, url, **kwargs)
        except (httpx.InvalidURL, UnicodeError) as e:  # e.g. a lone surrogate in a cursor value
            raise ConnectorError(f"'{action.name}': can't build the request "
                                 f"({type(e).__name__}).") from None

    def _check_target(self, action: Action, url: str) -> None:
        """The request must stay on the base URL's origin and under its path, whatever the
        action's path says (a path like "@evil.test/x" or ".evil.test/x" would otherwise
        move the request, and its credentials, to another host)."""
        base = self.base_url
        try:
            target = httpx.URL(url)
            ok = (action.path.startswith("/") and same_origin(target, base)
                  and target.raw_path.decode("ascii", "replace").startswith(
                      httpx.URL(base).raw_path.decode("ascii", "replace").rstrip("/") + "/"))
        except Exception:  # noqa: BLE001 - malformed URL
            ok = False
        if not ok:
            raise ConnectorError(f"'{action.name}' has path {action.path!r}, which doesn't stay "
                                 f"under the base URL {base}; refusing to send the request.")

    # -- internals -----------------------------------------------------------------

    def _resolve(self, action: str | Action) -> Action:
        return action if isinstance(action, Action) else self.connector.action(action)

    def _validate(self, action: Action, inputs: dict[str, Any]) -> None:
        validator = self._validators.get(action.name)
        if validator is None:
            validator = BoundedValidator(self.connector.input_schema(action))
            self._validators[action.name] = validator
        try:
            error = validator.best_error(inputs)
        except SchemaTooLarge as e:
            raise ConnectorError(f"'{action.name}': its input schema is too complex to validate "
                                 f"({e}); pass validate=False only if you trust the spec.") from None
        if error is not None:
            raise InputValidationError(action.name, error.message, list(error.path))

    def schemes_for(self, action: Action) -> list[Any] | None:
        """All auth schemes to apply for this action: the first requirement set whose
        schemes the credentials all satisfy ([] for anonymous). None = none satisfied."""
        if action.security_sets is not None:
            for want in sorted(action.security_sets, key=lambda x: not x):  # authenticated first
                schemes = [s for s in self.connector.auth if s.id in want]
                if len(schemes) == len(want) and all(
                        self.credentials is not None and self.credentials.satisfies(s) for s in schemes):
                    return schemes
            return None
        one = self.scheme_for(action)
        if one is not None:
            return [one]
        return [] if (action.security == [] or action.security is None) else None

    def scheme_for(self, action: Action) -> Any:
        """The (first) auth scheme to use for this action (per-operation security), or None."""
        if action.security_sets is not None:
            schemes = self.schemes_for(action)
            return schemes[0] if schemes else None
        if action.security is None:
            return self.scheme
        if not action.security:
            return None
        if self.scheme is not None and self.scheme.id in action.security:
            return self.scheme
        allowed = [s for s in self.connector.auth if s.id in action.security]
        return pick_scheme(allowed, self.credentials)

    def _follow_url(self, previous: httpx.Request, url: str, scheme: Any = None) -> httpx.Request:
        """Request a server-provided next-page URL the way the previous page was requested
        (same method and body: a POST search keeps its filters), keeping headers and
        query-string auth.
        Only URLs on the API's own origin (scheme, host and port) are followed: a hostile or
        compromised API must not be able to send our credentials somewhere else."""
        try:
            target = httpx.URL(url)
            target.host.encode("ascii")  # IDNA-encoded hosts only; raw Unicode can't be sent
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Refusing a malformed next-page URL ({type(e).__name__}).") from None
        if target.userinfo:
            raise ConnectorError("Refusing a next-page URL with user:password@ in it.")
        if not same_origin(target, self.base_url):
            raise ConnectorError(
                f"Refusing to follow a next-page URL on another origin ({redact_url(target)}); "
                f"credentials are only sent to {self.base_url}.")
        for sc in (scheme if isinstance(scheme, list) else [scheme] if scheme else []):
            if (sc.type == "api_key" and sc.location == "query"
                    and sc.param_name not in target.params and self.credentials):
                target = target.copy_merge_params({sc.param_name: self.credentials.key_for(sc)})
        headers = httpx.Headers(previous.headers)
        headers.pop("content-length", None)  # recomputed for the body below
        content = previous.read() if previous.method != "GET" else b""
        if not content:
            headers.pop("content-type", None)
        return self.http.build_request(previous.method, target, headers=headers, content=content or None)

    def _send_raw(self, action: Action, request: httpx.Request) -> tuple[Any, httpx.Response]:
        policy = self.retry
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            can_retry = attempt < policy.max_attempts
            try:
                response = self._send_following(request)
            except httpx.TransportError as exc:
                if can_retry and policy.should_retry_error(request, exc):
                    wait = policy.backoff(attempt)
                    log.warning("%s: %s; retrying in %.1fs (attempt %d/%d)", action.name,
                                type(exc).__name__, wait, attempt + 1, policy.max_attempts)
                    policy.sleep(wait)
                    continue
                raise ConnectionFailedError(action.name, redact_url(request.url), exc) from exc

            scheme = self.scheme_for(action)
            if (response.status_code == 401 and not refreshed and scheme is not None
                    and scheme.type == "oauth2" and self.credentials is not None
                    and self.credentials.oauth is not None):
                # Token revoked or expired early: refresh once and resend.
                refreshed = True
                response.close()
                token = self.credentials.oauth.access_token(force_refresh=True)
                request.headers["Authorization"] = f"Bearer {token}"
                attempt -= 1  # doesn't count against the retry budget
                continue

            if response.is_error and can_retry and policy.should_retry_response(request, response):
                wait = policy.server_wait(response)
                if wait is None:
                    wait = policy.backoff(attempt)
                if wait <= policy.max_wait:
                    log.warning("%s: HTTP %d; retrying in %.1fs (attempt %d/%d)", action.name,
                                response.status_code, wait, attempt + 1, policy.max_attempts)
                    response.close()
                    policy.sleep(wait)
                    continue
            break

        body = _parse_body(response)
        if response.is_error:
            if policy.is_rate_limited(response):
                raise RateLimitError(action.name, response.status_code, body,
                                     redact_url(response.request.url), policy.server_wait(response))
            raise ActionHTTPError(action.name, response.status_code, body,
                                  redact_url(response.request.url))
        self._check_response(action, body)
        return body, response

    def _send_following(self, request: httpx.Request) -> httpx.Response:
        """Send, following redirects ourselves. Same-origin hops keep every header; a hop to
        another origin keeps only harmless headers and must be a GET/HEAD; a downgrade from
        https to http is refused outright."""
        origin_url = request.url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = self.http.send(request, follow_redirects=False)
            except (httpx.InvalidURL, UnicodeError) as e:  # e.g. an invalid IDN host to connect to
                raise ConnectorError(f"Refusing a malformed URL ({type(e).__name__}).") from None
            location = response.headers.get("location")
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                return response
            try:
                target = request.url.join(location)
                target.host.encode("ascii")
            except Exception as e:  # noqa: BLE001
                response.close()
                raise ConnectorError(f"Refusing a malformed redirect ({type(e).__name__}).") from None
            if request.url.scheme == "https" and target.scheme != "https":
                response.close()
                raise ConnectorError(f"Refusing a redirect from https to {target.scheme} "
                                     f"({redact_url(target)}).")
            method, content = request.method, request.read()
            if (response.status_code == 303 and method != "HEAD"
                    or response.status_code in (301, 302) and method == "POST"):
                method, content = "GET", b""
            headers = httpx.Headers(request.headers)
            if not content:
                headers.pop("content-type", None)
            headers.pop("content-length", None)
            if not same_origin(target, origin_url):
                if method not in ("GET", "HEAD"):
                    response.close()
                    raise ConnectorError(f"Refusing to replay a {method} to another origin "
                                         f"({redact_url(target)}) after a redirect.")
                headers = httpx.Headers({k: v for k, v in headers.items()
                                         if k.lower() in _CROSS_ORIGIN_HEADERS})
                origin_url = target  # credentials stay dropped for the rest of the chain
            response.close()
            request = httpx.Request(method, target, headers=headers, content=content or None)
        raise ConnectorError(f"Too many redirects (more than {MAX_REDIRECTS}).")

    def _check_response(self, action: Action, body: Any) -> None:
        if self.validate_responses == "off" or action.output_schema is None or body is None:
            return
        validator = self._output_validators.get(action.name)
        if validator is None:
            schema = dict(action.output_schema)
            if self.connector.schemas:
                schema["$defs"] = self.connector.schemas
            validator = BoundedValidator(schema)
            self._output_validators[action.name] = validator
        try:
            error = validator.best_error(body)
        except SchemaTooLarge as e:
            warnings.warn(f"'{action.name}': response schema too complex to check ({e})",
                          ResponseSchemaWarning, stacklevel=4)
            return
        if error is None:
            return
        exc = ResponseValidationError(action.name, error.message, list(error.path))
        if self.validate_responses == "raise":
            raise exc
        warnings.warn(str(exc), ResponseSchemaWarning, stacklevel=4)

    def _send(self, action: Action, request: httpx.Request) -> Any:
        return self._send_raw(action, request)[0]


def _path_segment(value: str) -> str:
    """Percent-encode a path parameter so it stays inside its segment: '/' is encoded, and
    '.' / '..' are encoded too, since URL normalization would otherwise turn
    /items/{id} with id='..' into a different endpoint."""
    encoded = quote(value, safe=",")
    if encoded in (".", ".."):
        encoded = encoded.replace(".", "%2E")
    return encoded


def _unsupported_style(action: Action, p: Any, value: Any) -> ConnectorError:
    kind = "an object" if isinstance(value, dict) else "a list" if isinstance(value, (list, tuple)) else "a value"
    return ConnectorError(f"'{action.name}': can't send {kind} for {p.location} param '{p.name}' "
                          f"with style '{p.style}'.")


def _query_pairs(action: Action, p: Any, value: Any) -> dict[str, Any]:
    """OpenAPI query serialization. Objects: form+explode -> ?a=1&b=2, form -> ?name=a,1,b,2,
    deepObject -> ?name[a]=1 (nested: name[a][b]); arrays: repeated keys, or joined."""
    style = p.style or "form"
    explode = p.explode if p.explode is not None else style == "form" or style == "deepObject"
    if style == "deepObject":
        if not isinstance(value, dict):
            raise _unsupported_style(action, p, value)
        return {k: _query_value(v) for k, v in _flatten_form({p.sent_name: value}).items()}
    if style in ("spaceDelimited", "pipeDelimited"):
        sep = " " if style == "spaceDelimited" else "|"
        if isinstance(value, dict):
            return {p.sent_name: sep.join(f"{k}{sep}{_scalar(v)}" for k, v in value.items())}
        return {p.sent_name: _joined(value, sep)}
    if style != "form":
        raise _unsupported_style(action, p, value)
    if isinstance(value, dict):
        if explode:
            return {str(k): _query_value(v) if not isinstance(v, (dict, list, tuple)) else _scalar(v)
                    for k, v in value.items()}
        return {p.sent_name: ",".join(f"{k},{_scalar(v)}" for k, v in value.items())}
    if isinstance(value, (list, tuple)) and not explode:
        return {p.sent_name: _joined(value, ",")}
    return {p.sent_name: _query_value(value)}


def _simple(action: Action, p: Any, value: Any) -> str:
    """Header/path `simple` style: lists a,b; objects a,1,b,2 (explode: a=1,b=2)."""
    if p.style not in (None, "simple"):
        raise _unsupported_style(action, p, value)
    if isinstance(value, dict):
        sep = "=" if p.explode else ","
        return ",".join(f"{k}{sep}{_scalar(v)}" for k, v in value.items())
    return _joined(value, ",")


def _path_value(action: Action, p: Any, value: Any) -> str:
    """Path params: simple (default), label (.a.b / .a,b) or matrix (;id=a;id=b / ;id=a,b);
    always percent-encoded so the value stays inside its segment."""
    style = p.style or "simple"
    if style == "simple":
        return _path_segment(_simple(action, p, value))
    if style not in ("label", "matrix"):
        raise _unsupported_style(action, p, value)
    enc = _path_segment
    if isinstance(value, dict):
        items = [(str(k), _scalar(v)) for k, v in value.items()]
        if p.explode:
            parts = [f"{enc(k)}={enc(v)}" for k, v in items]
            return ("." if style == "label" else ";") + ("." if style == "label" else ";").join(parts)
        flat = ",".join(f"{enc(k)},{enc(v)}" for k, v in items)
        return "." + flat if style == "label" else f";{enc(p.name)}={flat}"
    values = [_scalar(v) for v in value] if isinstance(value, (list, tuple)) else [_scalar(value)]
    if style == "label":
        return "." + ("." if p.explode else ",").join(enc(v) for v in values)
    if p.explode:
        return "".join(f";{enc(p.name)}={enc(v)}" for v in values)
    return f";{enc(p.name)}=" + ",".join(enc(v) for v in values)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"))  # nested beyond what the style can say
    return str(value)


def _joined(value: Any, sep: str) -> str:
    if isinstance(value, (list, tuple)):
        return sep.join(str(_query_value(v)) for v in value)
    return str(_query_value(value))


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return [_query_value(v) for v in value]
    return value


def _flatten_form(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dicts/lists -> bracket notation: {"metadata": {"plan": "pro"}} ->
    {"metadata[plan]": "pro"}; lists -> key[0], key[1] (Stripe/Rails convention)."""
    out: dict[str, Any] = {}

    def add(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (dict, list, tuple)) and not value:
            out[key] = ""  # keep the key: empty containers are sent as `key=` (Stripe/Rails)
        elif isinstance(value, dict):
            for k, v in value.items():
                add(f"{key}[{k}]", v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                add(f"{key}[{i}]", v)
        else:
            out[key] = _query_value(value)

    for k, v in data.items():
        add(f"{prefix}[{k}]" if prefix else k, v)
    return out


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json" or ctype.endswith("+json"):
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text
