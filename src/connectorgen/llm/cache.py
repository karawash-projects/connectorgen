"""On-disk cache in front of any LLM: identical requests are answered from disk, so
re-running enrichment after a tweak only pays for what changed.

Safety:
- The key covers the provider, endpoint and model as well as the request, so answers
  from one model or server are never served for another.
- Cached answers are re-checked against the request's schema before use.
- The default location is your per-user cache directory, not the working directory, so
  a cloned repository can't ship pre-planted "answers". Files are owner-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .base import LLM, Usage

CACHE_VERSION = 2


def default_cache_dir() -> Path:
    """$CONNECTORGEN_CACHE_DIR, else the platform's per-user cache directory."""
    override = os.environ.get("CONNECTORGEN_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "connectorgen" / "llm"


def llm_identity(llm: Any) -> str:
    """Which model on which server answers this LLM's calls."""
    ident = getattr(llm, "cache_identity", None)
    if ident:
        return str(ident)
    return f"{type(llm).__module__}.{type(llm).__name__}:{getattr(llm, 'model', '')}"


class CachedLLM:
    def __init__(self, inner: LLM, directory: str | Path | None = None):
        self.inner = inner
        self.model = inner.model
        self.directory = Path(directory) if directory is not None else default_cache_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:  # pragma: no cover - e.g. not the owner
            pass

    @property
    def usage(self) -> Usage:
        return self.inner.usage

    @property
    def cache_identity(self) -> str:
        return llm_identity(self.inner)

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]:
        key = hashlib.sha256(json.dumps(
            [CACHE_VERSION, llm_identity(self.inner), system, prompt, schema],
            sort_keys=True).encode()).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists():
            try:
                result = json.loads(path.read_text())
                if isinstance(result, dict) and Draft202012Validator(schema).is_valid(result):
                    self.inner.usage.cache_hits += 1
                    return result
            except (json.JSONDecodeError, OSError):
                pass
            path.unlink(missing_ok=True)  # corrupt or doesn't fit the schema: ask again
        result = self.inner.complete_json(system=system, prompt=prompt, schema=schema,
                                          max_tokens=max_tokens)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # pragma: no cover - non-POSIX
            pass
        with os.fdopen(fd, "w") as f:
            json.dump(result, f)
        os.replace(tmp, path)
        return result
