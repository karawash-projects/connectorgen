"""API docs (HTML / Markdown / text) -> Connector IR, via a model.

Guardrails, because docs are untrusted and models can invent things:
- Grounding: an endpoint is kept only if every literal segment of its path
  appears in the docs text the model was shown.
- Base URL: kept only if it's on the same site as the docs you supplied (same registrable
  domain per the Public Suffix List, https, default port; exact host on shared docs hosts
  and for IP addresses). Otherwise credentials could be sent to a host a malicious page chose.
- Fetching: only the origins you supplied are fetched, redirects included, so a docs page
  can't steer the crawler at internal addresses; bodies and page counts are capped.
- Schemas the model writes must be valid JSON Schema, or they're dropped.
- The result goes through the contract tests like any other connector.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..enrich.context import UNTRUSTED_NOTICE
from ..enrich.passes import (
    AUTH_SCHEME_SCHEMA,
    NAME_RE,
    PAGINATION_FIELDS,
    auth_from_proposal,
    pagination_from_proposal,
)
from ..errors import UnsupportedSpecError
from ..ir import Action, Connector, Pagination, Param, hash_spec, pagination_problem
from ..llm.base import LLM, LLMError, Usage, enum, strict_object, text
from ..runtime.urls import origin, redact_url
from .openapi import DESTRUCTIVE_WORDS, _action_name, normalize_schema, snake_case

HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
log = logging.getLogger("connectorgen")
MAX_PAGE_BYTES = 5_000_000
MAX_REDIRECTS = 5

# ------------------------------------------------------------------ fetching


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "noscript", "svg", "head", "button", "form"}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "br", "table", "ul", "ol",
             "dt", "dd", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "pre":
            self._pre += 1
            self.parts.append("\n```\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag[0] == "h" and tag[1:].isdigit():
                self.parts.append("#" * int(tag[1:]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.parts.append("\n```\n")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t]+\n", "\n", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> tuple[str, list[str]]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text(), parser.links


@dataclass
class DocPage:
    source: str
    text: str


def _looks_like_openapi(body: str) -> bool:
    head = body.lstrip()[:2000]
    return bool(re.search(r'^\s*["\']?(openapi|swagger)["\']?\s*:', head, re.M))


def fetch_docs(sources: list[str], *, crawl: bool = False, max_pages: int = 15,
               http: httpx.Client | None = None, max_bytes: int = MAX_PAGE_BYTES) -> list[DocPage]:
    """Fetch URLs or read local files. With crawl=True, follow links that stay under
    each start URL's directory (same host), breadth-first.

    At most max_pages documents are fetched in total (empty pages count), each body is
    capped at max_bytes, and only the origins of the URLs you passed are ever contacted:
    a redirect or link to another origin (an internal address, say) is skipped."""
    client = http or httpx.Client(timeout=30, headers={"User-Agent": "connectorgen-docs/0.9"})
    pages: list[DocPage] = []
    allowed = {origin(s) for s in sources if s.startswith(("http://", "https://"))}
    try:
        queue: deque[str] = deque(sources)
        seen: set[str] = set()
        roots = [_root(s) for s in sources if s.startswith("http")]
        fetched = 0
        while queue and fetched < max_pages:
            src = urldefrag(queue.popleft())[0]
            if src in seen:
                continue
            seen.add(src)
            fetched += 1
            if src.startswith(("http://", "https://")):
                got = _get_capped(client, src, allowed, max_bytes)
                if got is None:
                    continue
                body, ctype, src = got
                seen.add(src)
            else:
                body = Path(src).read_text()
                ctype = "text/html" if src.endswith((".html", ".htm")) else "text/plain"
            if _looks_like_openapi(body):
                raise UnsupportedSpecError(
                    f"{src} is an OpenAPI/Swagger spec; use `connectorgen generate` instead.")
            if "html" in ctype or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                page_text, links = html_to_text(body)
            else:
                page_text, links = body, []
            if page_text.strip():
                pages.append(DocPage(src, page_text))
            if crawl and src.startswith("http"):
                for href in links:
                    url = urldefrag(urljoin(src, href))[0]
                    if url not in seen and any(url.startswith(r) for r in roots):
                        queue.append(url)
    finally:
        if http is None:
            client.close()
    return pages


def _fetch_allowed(url: str, allowed: set[Any]) -> bool:
    """A URL whose origin you supplied, or an https URL on the same site as one of them
    (docs.acme.com -> www.acme.com). Never an IP address or host you didn't supply."""
    if origin(url) in allowed:
        return True
    scheme, host, port = origin(url)
    return scheme == "https" and port == 443 and any(
        o[0] == "https" and _same_site(host, o[1], fetching=True) for o in allowed)


def _get_capped(client: httpx.Client, url: str, allowed: set[Any],
                max_bytes: int) -> tuple[str, str, str] | None:
    """(text, content type, final URL), following redirects only within `allowed` origins."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _fetch_allowed(url, allowed):
            log.warning("docs: skipped %s (outside the docs origins you supplied)", redact_url(url))
            return None
        try:
            got = _stream_once(client, url, max_bytes)
        except (httpx.InvalidURL, UnicodeError, ValueError) as e:
            log.warning("docs: skipped a malformed URL (%s)", type(e).__name__)
            return None
        if isinstance(got, str):   # a redirect: follow it (if allowed) on the next loop
            url = got
            continue
        return got
    log.warning("docs: skipped %s (too many redirects)", redact_url(url))
    return None


def _stream_once(client: httpx.Client, url: str, max_bytes: int) -> tuple[str, str, str] | str | None:
    """One GET: (text, content type, url), the next URL for a redirect, or None to skip."""
    with client.stream("GET", url, follow_redirects=False) as resp:
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            return urldefrag(urljoin(url, location))[0]
        if resp.is_error:
            return None
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                log.warning("docs: skipped %s (larger than %d bytes)", redact_url(url), max_bytes)
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        text_ = raw.decode(resp.encoding or "utf-8", errors="replace")
        return text_, resp.headers.get("content-type", ""), url


def _root(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path if parsed.path.endswith("/") else parsed.path.rsplit("/", 1)[0] + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def chunk_pages(pages: list[DocPage], max_chars: int = 40000) -> list[str]:
    """Split on headings into chunks under max_chars, keeping the page source as context."""
    chunks: list[str] = []
    for page in pages:
        sections = re.split(r"\n(?=#{1,3} )", page.text)
        current = ""
        for sec in sections:
            while len(sec) > max_chars:
                chunks.append(f"[source: {page.source}]\n{current}{sec[:max_chars]}".strip())
                current, sec = "", sec[max_chars:]
            if len(current) + len(sec) > max_chars and current:
                chunks.append(f"[source: {page.source}]\n{current}".strip())
                current = ""
            current += sec + "\n"
        if current.strip():
            chunks.append(f"[source: {page.source}]\n{current}".strip())
    return chunks


# ------------------------------------------------------------------ extraction

_EXTRACT_SYSTEM = f"""You turn API documentation into a precise machine-readable
description of its HTTP endpoints, for building an API connector.
Rules:
- Only include endpoints the docs actually document. Copy paths exactly, using
  {{param}} placeholders (convert :id or <id> to {{id}}). Paths are relative to the base URL.
- base_url: the API's root URL if stated, else "".
- params: path, query and header parameters (not body fields). Every {{placeholder}}
  in the path must have a path param.
- body_schema_json / response_schema_json: a JSON Schema serialized as a JSON string,
  describing the request body / success response; "" if not documented.
- name: snake_case verb_object (list_pets, get_pet, create_pet).
- evidence: a short verbatim snippet from the docs that shows this endpoint.
- auth: schemes the docs describe (may be empty).
{UNTRUSTED_NOTICE}"""

_PARAM_SCHEMA = strict_object({
    "name": text(),
    "location": enum(["path", "query", "header"]),
    "required": {"type": "boolean"},
    "type": enum(["string", "integer", "number", "boolean", "array", "object"]),
    "description": text(),
    "enum_values": {"type": "array", "items": {"type": "string"}},
})

_ENDPOINT_SCHEMA = strict_object({
    "name": text(),
    "method": enum(HTTP_METHODS),
    "path": text(),
    "summary": text(),
    "description": text(),
    "params": {"type": "array", "items": _PARAM_SCHEMA},
    "body_encoding": enum(["none", "json", "form"]),
    "body_required": {"type": "boolean"},
    "body_schema_json": text(),
    "response_schema_json": text(),
    "pagination": strict_object(PAGINATION_FIELDS),
    "evidence": text(),
})

_EXTRACT_SCHEMA = strict_object({
    "api_title": text("the API's name, or ''"),
    "base_url": text(),
    "auth": {"type": "array", "items": AUTH_SCHEME_SCHEMA},
    "endpoints": {"type": "array", "items": _ENDPOINT_SCHEMA},
})


@dataclass
class DocsReport:
    pages: list[str] = field(default_factory=list)
    chunks: int = 0
    extracted: int = 0
    kept: int = 0
    dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def to_text(self) -> str:
        lines = [f"Read {len(self.pages)} page(s) in {self.chunks} chunk(s); model proposed "
                 f"{self.extracted} endpoint(s), kept {self.kept}."]
        lines += [f"  x dropped {d}" for d in self.dropped]
        lines += [f"  ! {n}" for n in self.notes]
        lines.append(str(self.usage))
        return "\n".join(lines)


def _norm_path(path: str) -> str:
    path = "/" + path.strip().lstrip("/")
    path = re.sub(r"/:([A-Za-z_][A-Za-z0-9_]*)", r"/{\1}", path)
    path = re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", r"{\1}", path)
    return path.split("?")[0].rstrip("/") or "/"


def _grounded(path: str, chunk: str) -> bool:
    """Every literal path segment appears in the docs text itself, as a whole token. The
    chunk's own "[source: <url>]" header is excluded: otherwise the docs URL's path would
    ground any endpoint that reuses its segments."""
    haystack = re.sub(r"^\[source: [^\]\n]*\]\n?", "", chunk).lower()
    literal = [seg for seg in path.strip("/").split("/") if seg and not seg.startswith("{")]
    return all(re.search(r"(?<![a-z0-9_-])" + re.escape(seg.lower()) + r"(?![a-z0-9_-])", haystack)
               for seg in literal)


# Platforms that host many unrelated customers' docs or apps under one domain. Two hosts
# here are only "the same site" if they're identical. (Many, like github.io, are in the
# Public Suffix List already; these are the docs platforms it doesn't cover.)
SHARED_HOSTS = ("readme.io", "readme.com", "mintlify.app", "mintlify.dev", "stoplight.io",
                "apiary.io", "redoc.ly", "redocly.app", "bump.sh", "getpostman.com",
                "postman.co", "postman.com", "notion.site", "gitbook.io", "github.io",
                "gitlab.io", "netlify.app", "vercel.app", "pages.dev", "web.app",
                "firebaseapp.com", "herokuapp.com", "azurewebsites.net", "cloudfront.net",
                "amazonaws.com", "onrender.com", "fly.dev", "workers.dev", "ngrok.io",
                "ngrok-free.app", "trycloudflare.com", "localhost")


@functools.lru_cache(maxsize=1)
def _psl() -> Any:
    from publicsuffixlist import PublicSuffixList

    return PublicSuffixList()


# Subdomains companies conventionally use for their own docs. Docs served from one of these
# (or from the bare registrable domain) are taken to belong to whoever owns that domain, so
# another host on the same domain is the same site. Docs on any other subdomain -- most often
# a tenant on a hosting platform, like acme.zendesk.com -- only vouch for their exact host.
DOCS_LABELS = {"docs", "doc", "developer", "developers", "dev", "devs", "api", "apis", "api-docs",
               "apidocs", "apiref", "reference", "ref", "www", "help", "support", "learn",
               "platform", "portal", "devportal", "developer-docs", "documentation", "guides",
               "kb", "open", "manual", "wiki"}


def _site(host: str) -> str | None:
    """The domain whose hosts docs on `host` can vouch for, or None when only `host` itself
    is safe: IP addresses, single-label names, shared hosting platforms, and docs hosts that
    aren't the domain's own (see DOCS_LABELS)."""
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    if "." not in host or any(host == d or host.endswith("." + d) for d in SHARED_HOSTS):
        return None
    registrable = _psl().privatesuffix(host)
    if not registrable:
        return None
    if host == registrable or host[: -len(registrable) - 1] in DOCS_LABELS:
        return registrable   # the bare domain, or exactly one conventional docs label on it
    return None


# The base URL itself must be the docs host, the bare domain, or exactly "api." on it. Any
# other label might be a customer-chosen subdomain on a multi-tenant platform (rest.zendesk.com,
# api7.zendesk.com), so it's never accepted automatically: pass --base-url instead.
_API_LABEL = re.compile(r"^api$")


def _same_site(candidate_host: str, docs_host: str, *, fetching: bool = False) -> bool:
    """Is candidate_host the docs owner's own? For a base URL (credentials go there) it must
    be the docs host, the bare domain, or an API label under it; for fetching more docs
    pages (www., docs., help., ...) a docs-like label is fine too."""
    c, d = candidate_host.lower().rstrip("."), docs_host.lower().rstrip(".")
    if c == d:
        return True
    site = _site(d)
    if site is None:
        return False
    try:
        ipaddress.ip_address(c.strip("[]"))
        return False
    except ValueError:
        pass
    if c == site:
        return True
    if not c.endswith("." + site):
        return False
    label = c[: -len(site) - 1]
    if _API_LABEL.match(label):
        return True
    return fetching and label in DOCS_LABELS


def _safe_base_url(candidate: str, sources: list[str]) -> bool:
    """Same site as one of the docs URLs, over https on the default port (or exactly the
    docs' own scheme, host and port, e.g. a local http docs server). Malformed: False."""
    try:
        return _safe_base_url_unchecked(candidate, sources)
    except Exception:  # noqa: BLE001 - a model-written URL can be anything
        return False


_PLAIN_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.?$")


def _safe_base_url_unchecked(candidate: str, sources: list[str]) -> bool:
    if any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in candidate):
        return False  # whitespace, control or non-ASCII look-alike characters
    # Parse with httpx (what will actually send the requests) and accept only a plain
    # hostname with no user:password@ part, so parsers can't disagree about the host.
    parsed = httpx.URL(candidate)
    if parsed.userinfo or not _PLAIN_HOST.match(parsed.host or "") or "\\" in candidate:
        return False
    c = urlparse(candidate)
    if (c.hostname or "").lower() != parsed.host.lower():
        return False
    if not c.hostname or c.scheme not in ("http", "https"):
        return False
    try:
        c_port = c.port or (443 if c.scheme == "https" else 80)
    except ValueError:
        return False
    for s in sources:
        if not s.startswith(("http://", "https://")):
            continue
        d = urlparse(s)
        if not d.hostname:
            continue
        if origin(candidate) == origin(s):
            return True
        if c.scheme == "https" and c_port == 443 and _same_site(c.hostname, d.hostname):
            return True
    return False


def _parse_schema(raw: str, what: str, notes: list[str]) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return None
    try:
        schema = json.loads(raw)
        if not isinstance(schema, dict):
            raise ValueError("not an object")
        Draft202012Validator.check_schema(schema)
        return normalize_schema(schema)
    except (ValueError, SchemaError) as e:
        notes.append(f"Model-written {what} schema was invalid and was dropped ({e}).")
        return None


def _build_action(ep: dict[str, Any]) -> Action:
    notes: list[str] = []
    path = _norm_path(ep["path"])
    placeholders = re.findall(r"\{([^}]+)\}", path)
    params: list[Param] = []
    seen: set[tuple[str, str]] = set()
    for p in ep["params"]:
        key = (p["name"], p["location"])
        if key in seen or not p["name"]:
            continue
        seen.add(key)
        if p["location"] == "path" and p["name"] not in placeholders:
            notes.append(f"Path param '{p['name']}' isn't in the path; dropped.")
            continue
        schema: dict[str, Any] = {"type": p["type"]}
        if p["type"] == "array":
            schema["items"] = {"type": "string"}
        if p["enum_values"] and p["type"] == "string":
            schema["enum"] = p["enum_values"]
        params.append(Param(name=p["name"], location=p["location"],
                            required=p["required"] or p["location"] == "path",
                            json_schema=schema, description=p["description"]))
    for name in placeholders:
        if (name, "path") not in seen:
            params.append(Param(name=name, location="path", required=True,
                                json_schema={"type": "string"}))
            notes.append(f"Path placeholder '{{{name}}}' had no documented param; added as string.")

    body_schema = None
    if ep["body_encoding"] != "none":
        body_schema = _parse_schema(ep["body_schema_json"], "request body", notes) \
            or {"type": "object"}
    output_schema = _parse_schema(ep["response_schema_json"], "response", notes)
    pagination = pagination_from_proposal(ep["pagination"])
    problem = pagination_problem(pagination, {p.name for p in params if p.location == "query"})
    if problem:
        notes.append(f"Docs pagination ignored ({problem}); needs review.")
        pagination = Pagination()
    if pagination.style != "none" and output_schema is None:
        notes.append("Docs describe pagination but no response schema; pagination unverified.")
    method = ep["method"]
    return Action(
        name=ep["name"].strip(),
        method=method,
        path=path,
        summary=ep["summary"].strip(),
        description=ep["description"].strip(),
        params=params,
        body_schema=body_schema,
        body_required=bool(ep["body_required"]) and body_schema is not None,
        body_encoding="form" if ep["body_encoding"] == "form" else "json",
        output_schema=output_schema,
        pagination=pagination,
        destructive=method == "DELETE",
        notes=notes,
    )


# Verbs that destroy, disable, revoke, move away or pay out. Matched as whole words in the
# action's name and path (split on camelCase and punctuation) and as the leading verb of its
# summary, so "dropdown", "dropbox" or "Deleted items are excluded" don't count.
DESTRUCTIVE_VERBS = {
    "delete", "remove", "destroy", "purge", "erase", "wipe", "truncate", "terminate", "flush",
    "reset", "deactivate", "disable", "close", "drop", "kill", "clear", "uninstall", "suspend",
    "revoke", "cancel", "void", "ban", "expire", "overwrite", "unpublish", "unsubscribe",
    "detach", "unlink", "prune", "stop", "kick", "invalidate", "disconnect", "rotate",
    "regenerate", "transfer", "rollback", "anonymize", "anonymise", "empty", "abort", "discard",
    "deprovision", "refund", "evict", "replace", "trash", "undeploy", "logout", "signout",
    "retire", "offboard", "demote", "capture", "payout", "merge", "force", "commit", "password", "block", "deny", "decommission", "archive",
    "lock", "freeze", "shutdown", "restart", "reboot", "reinstall", "downgrade", "leave",
    "unassign", "uninvite", "unshare", "unfollow", "unenroll", "dismiss", "withdraw",
    # generic executors: whatever they run (SQL, a mutation, any method) could destroy data
    "execute", "exec", "eval", "sql", "rpc", "graphql", "mutation", "mutate", "command",
    "shell", "script",
}
# Anywhere in a summary or description, these mean "runs arbitrary statements".
_EXECUTOR_WORDS = {"sql", "mutation", "mutations", "shell"}
_IRREVERSIBLE = re.compile(r"\bpermanent(ly)?\b|irreversib|can(not|'t| not) be (undone|recovered|reversed)",
                           re.IGNORECASE)
# An endpoint ending in one of these reads or checks something, even when an earlier path
# segment names a destructive thing (/hr/leave/balance, /sql/validate, /locks/status).
READ_ONLY_WORDS = {"status", "list", "search", "find", "get", "validate", "validation", "check",
                   "preview", "estimate", "count", "analyze", "analyse", "lookup", "eligibility",
                   "balance", "history", "stats", "info", "describe", "verify", "simulate",
                   "dry", "render", "quote", "report", "export", "download", "policy", "policies",
                   "analyze", "query", "extract", "screen", "read", "fetch", "compute", "calculate"}
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _verb_forms(word: str) -> set[str]:
    w = word.lower()
    forms = {w}
    for suffix in ("es", "s"):
        if w.endswith(suffix) and len(w) > len(suffix) + 2:
            forms.add(w[: -len(suffix)])
    return forms


def docs_action_destructive(act: Action) -> bool:
    """Docs-made actions are named by a model, so be conservative: PUT/PATCH/DELETE replace
    or remove data, and a POST is destructive when its name or path contains a destructive
    verb, or its summary starts with one."""
    if act.method in ("PUT", "PATCH", "DELETE"):
        return True
    if act.method == "GET":
        return False
    summary_words = _WORD.findall(act.summary or "")
    if summary_words and _verb_forms(summary_words[0]) & DESTRUCTIVE_VERBS:
        return True
    text = f"{act.summary} {act.description}"
    if _IRREVERSIBLE.search(text):
        return True
    if summary_words and _verb_forms(summary_words[0]) & READ_ONLY_WORDS:
        return False   # "Get the cancellation policy", "Analyze stop words"
    described = {w.lower() for w in _WORD.findall(text)}
    if described & _EXECUTOR_WORDS:
        return True    # "Runs a SQL statement": whatever it runs could destroy data
    literal = [seg for seg in act.path.strip("/").split("/") if seg and not seg.startswith("{")]
    last_words = {w.lower() for w in _WORD.findall(literal[-1])} if literal else set()
    name_words = [w.lower() for w in _WORD.findall(act.name)]
    if last_words & READ_ONLY_WORDS or (name_words and name_words[0] in READ_ONLY_WORDS):
        return False
    words = _WORD.findall(act.name) + _WORD.findall(act.path)
    return any(_verb_forms(w) & DESTRUCTIVE_VERBS for w in words) or \
        bool(DESTRUCTIVE_WORDS.search(snake_case(act.name)))


def _merge(existing: Action, new: Action) -> Action:
    """Same endpoint documented in two chunks: keep the richer of each part."""
    have = {(p.name, p.location) for p in existing.params}
    existing.params += [p for p in new.params if (p.name, p.location) not in have]
    for attr in ("summary", "description"):
        if len(getattr(new, attr)) > len(getattr(existing, attr)):
            setattr(existing, attr, getattr(new, attr))
    for attr in ("body_schema", "output_schema"):
        if getattr(existing, attr) in (None, {"type": "object"}) and getattr(new, attr):
            setattr(existing, attr, getattr(new, attr))
    if existing.pagination.style == "none":
        existing.pagination = new.pagination
    existing.notes = list(dict.fromkeys(existing.notes + new.notes))
    return existing


def connector_from_docs(
    sources: list[str],
    llm: LLM,
    *,
    name: str | None = None,
    base_url: str | None = None,
    crawl: bool = False,
    max_pages: int = 15,
    chunk_chars: int = 40000,
    http: httpx.Client | None = None,
) -> tuple[Connector, DocsReport]:
    """Read API docs and build a Connector. Every action is marked as model-derived."""
    report = DocsReport(usage=llm.usage)
    pages = fetch_docs(sources, crawl=crawl, max_pages=max_pages, http=http)
    if not pages:
        raise UnsupportedSpecError("No readable documentation text found.")
    report.pages = [p.source for p in pages]
    chunks = chunk_pages(pages, chunk_chars)
    report.chunks = len(chunks)

    titles: list[str] = []
    base_urls: list[str] = []
    auth_items: list[dict[str, Any]] = []
    actions: dict[tuple[str, str], Action] = {}
    source_tag = f"llm:{llm.model}"

    for i, chunk in enumerate(chunks, 1):
        try:
            out = llm.complete_json(system=_EXTRACT_SYSTEM,
                                    prompt=f"<docs>\n{chunk}\n</docs>",
                                    schema=_EXTRACT_SCHEMA, max_tokens=32000)
        except LLMError as e:
            report.notes.append(f"chunk {i}/{len(chunks)} failed: {e}")
            continue
        if out["api_title"].strip():
            titles.append(out["api_title"].strip())
        if out["base_url"].strip():
            base_urls.append(out["base_url"].strip().rstrip("/"))
        auth_items += out["auth"]
        for ep in out["endpoints"]:
            report.extracted += 1
            label = f"{ep['method']} {ep['path']}"
            if not _grounded(_norm_path(ep["path"]), chunk):
                report.dropped.append(f"{label}: path not found in the docs text (likely invented)")
                continue
            act = _build_action(ep)
            key = (act.method, re.sub(r"\{[^}]+\}", "{}", act.path))
            actions[key] = _merge(actions[key], act) if key in actions else act

    # names: model's choice if valid and unique, else derived from method + path
    used: set[str] = set()
    for act in actions.values():
        candidate = snake_case(act.name) if act.name else ""
        if not NAME_RE.match(candidate) or candidate in used:
            candidate = _action_name(act.method.lower(), act.path, {})
        base, n = candidate, 2
        while candidate in used:
            candidate, n = f"{base}_{n}", n + 1
        used.add(candidate)
        act.name = candidate
        act.provenance = {"action": source_tag}
        act.destructive = docs_action_destructive(act)

    connector_notes: list[str] = []
    final_base = (base_url or "").rstrip("/")
    if not final_base and base_urls:
        candidate = max(set(base_urls), key=base_urls.count)
        if _safe_base_url(candidate, sources):
            final_base = candidate
        else:
            connector_notes.append(
                f"Docs gave base URL {candidate!r}, which isn't on the docs' own domain; "
                "not used. Pass base_url explicitly if it's correct.")
    if not final_base:
        connector_notes.append("No usable base URL; pass one when creating the client.")

    seen_auth: set[tuple[str, str | None]] = set()
    unique_auth = []
    for item in auth_items:
        key = (item["type"], item.get("param_name") or None)
        if key not in seen_auth:
            seen_auth.add(key)
            unique_auth.append(item)
    auth, problems = auth_from_proposal(unique_auth, prefix="docs")
    connector_notes += problems

    title = max(set(titles), key=titles.count) if titles else (name or "api")
    report.kept = len(actions)
    report.notes += connector_notes
    connector = Connector(
        name=name or snake_case(title),
        title=title,
        base_url=final_base,
        auth=auth,
        actions=list(actions.values()),
        source=", ".join(report.pages),
        spec_hash=hash_spec([p.text for p in pages]),
        provenance={"connector": source_tag, **({"auth": source_tag} if auth else {})},
        notes=connector_notes,
    )
    from ..triggers.derive import derive_polling_triggers

    connector.triggers += derive_polling_triggers(connector)
    return connector, report
