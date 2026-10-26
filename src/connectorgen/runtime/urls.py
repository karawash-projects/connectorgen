"""URL safety helpers shared by the runtime, the docs crawler and the LLM layer.

- `same_origin`: scheme, host and port all match. Credentials are only ever sent to the
  origin they were configured for, so a hostile next-page link or redirect can't collect them.
- `redact_url`: hides secret-looking query values before a URL reaches a message or a log.
- `install_log_redaction`: httpx logs every request URL at INFO; this filter redacts those too.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Query parameter names whose values are treated as secrets in messages and logs.
_SECRET_EXACT = {"key", "code", "sig", "auth", "session", "sessionid", "pwd", "jwt", "pass"}
SECRET_NAME = re.compile(
    r"api[-_]?key|apikey|token|secret|passw|signature|credential|authorization|access[-_]?key",
    re.IGNORECASE,
)
_extra_secret_names: set[str] = set()


def origin(url: str | httpx.URL) -> tuple[str, str, int | None]:
    """(scheme, host, port). An unparseable URL gets a value no real origin equals."""
    try:
        u = httpx.URL(str(url))
        scheme = u.scheme.lower()
        return scheme, (u.host or "").lower().rstrip("."), u.port or _DEFAULT_PORTS.get(scheme)
    except Exception:  # noqa: BLE001 - invalid IDN, bad characters, ...
        return "invalid", f"<unparseable {len(str(url))}-char url>", -1


def same_origin(a: str | httpx.URL, b: str | httpx.URL) -> bool:
    return origin(a) == origin(b)


def register_secret_param(name: str | None) -> None:
    """Also redact this query parameter (e.g. an API's custom API-key parameter name)."""
    if name:
        _extra_secret_names.add(name.lower())


def is_secret_name(name: str) -> bool:
    low = name.lower()
    return low in _extra_secret_names or low in _SECRET_EXACT or bool(SECRET_NAME.search(name))


def redact_url(url: str | httpx.URL, extra: set[str] | None = None) -> str:
    """The URL with secret-looking query values (and any userinfo) replaced by ***."""
    try:
        u = httpx.URL(str(url))
    except Exception:  # noqa: BLE001 - never let redaction itself fail
        return "<unparseable url>"
    names = {n.lower() for n in (extra or set())}
    if u.userinfo:
        u = u.copy_with(username=None, password=None)
    if not u.query:
        return str(u)
    pairs = []
    for key, value in u.params.multi_items():
        hidden = key.lower() in names or is_secret_name(key)
        pairs.append((key, "***" if hidden and value else value))
    return str(u.copy_with(params=httpx.QueryParams(pairs)))


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")


def redact_text(text: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(redact_url(a) if isinstance(a, httpx.URL) else
                                redact_text(a) if isinstance(a, str) else a
                                for a in (record.args if isinstance(record.args, tuple)
                                          else (record.args,)))
        if isinstance(record.msg, str) and "://" in record.msg:
            record.msg = redact_text(record.msg)
        return True


_installed = False


def install_log_redaction() -> None:
    """Redact query secrets in httpx's own request logs (idempotent)."""
    global _installed
    if _installed:
        return
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).addFilter(_RedactFilter())
    _installed = True


def check_public_http_url(url: str) -> Any:
    """Raise ValueError unless url is http(s) with a host; returns the parsed URL."""
    u = httpx.URL(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise ValueError(f"not an http(s) URL: {redact_url(url)}")
    return u


# ------------------------------------------------------------------ secret detection
#
# Used to stop credentials being written into connector files, which are meant to be
# shareable. These are high-confidence rules: a false alarm blocks a legitimate import, so
# weak signals (a UUID, a word like "auth" or "session" in an option name) aren't used.

# Values that are credentials whatever they're called.
_SECRET_VALUE = re.compile(
    r"^(bearer|basic|token|digest)\s+\S+"                          # auth schemes
    r"|^(ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs]|sk|rk|sk-ant|sk-proj|npm|pypi|hf"
    r"|dop_v1|shpat|shpss|sq0atp|sq0csp|SG)[-_.][A-Za-z0-9_\-.]{8,}"   # well-known token prefixes
    r"|^AKIA[0-9A-Z]{16}$|^AIza[0-9A-Za-z_-]{30,}$"                # AWS / Google keys
    r"|^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."                # JWT
    r"|^\d{6,12}:[A-Za-z0-9_-]{30,}$",                             # Telegram bot token
    re.IGNORECASE)
# Random-looking strings: only used for header values, where they're nearly always secrets.
_HIGH_ENTROPY = re.compile(r"^(?=.*[0-9])(?=.*[a-z])(?=.*[A-Z])[A-Za-z0-9_\-+/=.]{24,}$")
_CONN_STRING_SECRET = re.compile(r"(^|[;&?\s])(password|pwd|passwd|secret|token|apikey|api_key)\s*=\s*[^;&\s]",
                                 re.IGNORECASE)
# Words that mark a name as holding a credential.
_STRONG_WORDS = {"token", "secret", "password", "passwd", "passphrase", "pwd", "apikey", "pat",
                 "jwt", "dsn", "bearer", "signature", "hmac"}
# Also strong in header and query names (where a bare "key"/"sig"/"session" means a credential).
_HEADER_WORDS = _STRONG_WORDS | {"key", "sig", "session", "sid", "credential", "credentials"}
_SECRET_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie", "x-auth", "auth",
                        "x-csrf-token"}
_SECRET_URL_HOSTS = ("hooks.slack.com", "discord.com", "discordapp.com", "webhook.office.com",
                     "outlook.office.com", "hooks.zapier.com", "maker.ifttt.com")
_SPLITTER = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _words(name: str) -> set[str]:
    words = {w.lower() for w in _SPLITTER.findall(name)}
    squashed = re.sub(r"[^a-z]", "", name.lower())
    return words | {w for w in ("apikey", "accesskey", "secretkey", "privatekey", "clientsecret")
                    if w in squashed}


def _strong_name(name: str, words: set[str] = _STRONG_WORDS) -> bool:
    found = _words(name)
    if found & words:
        return True
    return bool(found & {"accesskey", "secretkey", "privatekey", "clientsecret"}) or \
        "apikey" in found


def _trivial(value: str) -> bool:
    return value.lower() in ("true", "false", "yes", "no", "on", "off", "none", "") or \
        bool(re.fullmatch(r"[-+]?[0-9.]+[a-z]{0,2}", value, re.IGNORECASE))


_PLACEHOLDER = re.compile(
    r"^(?:(?:bearer|basic|token)\s+)?(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\{\{[^}]+\}\}|<[^<>]+>|\[[^\[\]]+\])$", re.IGNORECASE)
_PLACEHOLDER_WORDS = re.compile(r"your[-_ ]|[-_]here$|^(changeme|change[-_]me|replace[-_]?me|example|"
                                r"placeholder|redacted|dummy|todo|tbd|none|null|xxx+|password|pass|"
                                r"secret|token|api[-_]?key|key|user|username)$|x{6,}|\*{4,}",
                                re.IGNORECASE)


def _placeholder(value: str) -> bool:
    """A stand-in, not a credential: ${TOKEN}, ${env:KEY}, $TOKEN, %TOKEN%, {{token}},
    <YOUR_API_KEY>, YOUR_TOKEN_HERE, xoxb-your-bot-token, ghp_xxxxxxxx, "Bearer ${TOKEN}"."""
    v = value.strip()
    return bool(_PLACEHOLDER.match(v) or _PLACEHOLDER_WORDS.search(v))


def _path_like(value: str) -> bool:
    return value.startswith(("/", "./", "../", "~", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def looks_like_secret_value(value: str) -> bool:
    """A value that is a credential on its own: a token with a known shape, a URL or
    connection string carrying a password, or (recursively) a command or header in a string."""
    v = value.strip()
    if not v or _placeholder(v):
        return False
    if "-----BEGIN" in v and "PRIVATE KEY" in v:
        return True
    if _SECRET_VALUE.search(v) or _CONN_STRING_SECRET.search(v):
        return True
    if v.startswith(("{", "[")):                                      # a JSON config argument
        try:
            return bool(_secret_json(json.loads(v)))
        except ValueError:
            pass
    if "://" in v and not any(ch.isspace() for ch in v):
        return bool(secret_url_parts(v))
    header = re.match(r"^([A-Za-z][A-Za-z0-9-]*)\s*:\s*(\S.*)$", v)   # "Authorization: Bearer x"
    if header and secret_header_names({header.group(1): header.group(2)}):
        return True
    if any(ch.isspace() for ch in v):                                 # a nested command line
        import shlex
        try:
            return bool(secret_args(shlex.split(v)))
        except ValueError:
            return False
    return False


def secret_url_parts(url: str) -> list[str]:
    """What in a URL is a credential: userinfo, credential-named or token-shaped query or
    fragment values, token-shaped path segments, or a secret-bearing webhook URL."""
    try:
        u = httpx.URL(url)
    except Exception:  # noqa: BLE001
        return ["unparseable URL"]
    found = []
    if u.password and not _placeholder(u.password):
        found.append("credentials in the URL (user:password@)")
    elif u.username and not u.password and (_SECRET_VALUE.search(u.username)
                                            or _HIGH_ENTROPY.match(u.username)):
        found.append("a token in the URL (token@host)")
    pairs = list(u.params.multi_items())
    if u.fragment and "=" in u.fragment:
        pairs += list(httpx.QueryParams(u.fragment).multi_items())
    for k, v in pairs:
        if v and not _trivial(v) and (_strong_name(k, _HEADER_WORDS) or _SECRET_VALUE.search(v)):
            found.append(f"query '{k}'")
    if any((u.host or "").endswith(h) for h in _SECRET_URL_HOSTS) and len(u.path) > 12:
        found.append("a webhook URL (the path is the secret)")
    if any(seg and _SECRET_VALUE.search(seg) for seg in u.path.split("/")):
        found.append("a token in the URL path")
    return found


def secret_header_names(headers: dict[str, str]) -> list[str]:
    """Headers whose name or value marks them as carrying a credential."""
    out = []
    for k, v in headers.items():
        value = str(v).strip()
        if _placeholder(value):
            continue
        if _trivial(value) or _path_like(value):
            named = False
        else:
            named = k.lower() in _SECRET_HEADER_NAMES or _strong_name(k, _HEADER_WORDS)
        if named or _SECRET_VALUE.search(value) or _HIGH_ENTROPY.match(value) \
                or _CONN_STRING_SECRET.search(value):
            out.append(k)
    return out


def _secret_json(data: Any, depth: int = 0) -> list[str]:
    """Credential-named keys with real-looking values in a JSON config (Smithery-style
    --config '{"apiKey": "..."}')."""
    found: list[str] = []
    if depth > 20:
        return found
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and (_strong_name(str(k), _HEADER_WORDS) and _credential_like(v)
                                       or looks_like_secret_value(v)):
                found.append(str(k))
            elif isinstance(v, (dict, list)):
                found += _secret_json(v, depth + 1)
    elif isinstance(data, list):
        for v in data:
            found += _secret_json(v, depth + 1)
    return found


def _credential_like(value: str, name: str = "") -> bool:
    """A value given to a credential-named option that could be the credential itself
    (not a path to a file holding it, a plain URL, a placeholder, or a word like 'keychain')."""
    v = value.strip()
    if _trivial(v) or v.startswith("-") or _path_like(v) or _placeholder(v):
        return False
    if re.fullmatch(r"[a-z]+([-_][a-z]+)*", v) and len(v) <= 24 and \
            not _words(name) & {"password", "passwd", "passphrase", "pwd", "secret", "pass", "token"}:
        return False  # a mode or source name (keychain, oauth, env), not a secret
    if "://" in v:
        return bool(secret_url_parts(v))
    return True


def secret_args(args: list[str]) -> list[str]:
    """Command-line arguments that pass a credential: --token X, --password=X, TOKEN=X,
    URLs or connection strings with passwords, token-shaped values, and the same inside
    nested command strings (sh -c "...") or header arguments ("Authorization: Bearer x")."""
    found = []
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        name = flag.lstrip("-")
        if flag.startswith("-") and name and (_strong_name(name) or name.lower() in ("key", "k", "p")):
            nxt = value if eq else (args[i + 1] if i + 1 < len(args) else "")
            if _credential_like(nxt, name):
                found.append(flag)
                continue
        if eq and not flag.startswith("-") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", flag) \
                and _strong_name(flag) and _credential_like(value, flag):
            found.append(flag + "=...")  # NAME=value, e.g. docker -e GITHUB_TOKEN=...
            continue
        if eq and flag.startswith("-") and value and secret_args([value]):
            found.append(flag + "=...")   # --env=GITHUB_TOKEN=ghp_...
            continue
        if looks_like_secret_value(arg) or (eq and flag.startswith("-") and looks_like_secret_value(value)):
            found.append(arg[:6] + "...")
    return found


def secret_env(env: dict[str, str]) -> list[str]:
    return [k for k, v in env.items()
            if (_strong_name(k) and _credential_like(str(v), k)) or looks_like_secret_value(str(v))
            or (not _path_like(str(v)) and not _placeholder(str(v)) and _HIGH_ENTROPY.match(str(v)))]


def secret_query_names(url: str) -> list[str]:
    return secret_url_parts(url)
