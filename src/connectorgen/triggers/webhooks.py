"""Webhook triggers: verify a delivery's signature, dedupe, parse and validate it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import warnings
from collections.abc import Mapping, MutableSet
from typing import Any, Literal


from ..errors import ConnectorError, ResponseSchemaWarning
from ..ir import Trigger, WebhookSignature
from ..limits import BoundedValidator, SchemaTooLarge

_ALGOS = {"sha256": hashlib.sha256, "sha1": hashlib.sha1, "sha512": hashlib.sha512}


class WebhookError(ConnectorError):
    """Delivery rejected: bad/missing signature, replayed, malformed, or duplicate."""


class DuplicateDeliveryError(WebhookError):
    """This delivery id was already processed (senders retry; this is normal)."""


def _digest(secret: str | bytes, message: bytes, sig: WebhookSignature) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    mac = hmac.new(key, message, _ALGOS[sig.algorithm]).digest()
    return mac.hex() if sig.encoding == "hex" else base64.b64encode(mac).decode()


def sign_payload(secret: str | bytes, body: bytes, sig: WebhookSignature,
                 timestamp: int | None = None) -> str:
    """The header value a sender would produce. Handy for testing your receiver."""
    if sig.format == "stripe":
        ts = int(time.time()) if timestamp is None else timestamp
        return f"t={ts},v1={_digest(secret, f'{ts}.'.encode() + body, sig)}"
    return sig.prefix + _digest(secret, body, sig)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lowered), None)


def verify_signature(secret: str | bytes, body: bytes, headers: Mapping[str, str],
                     sig: WebhookSignature, *, now: float | None = None) -> None:
    """Raise WebhookError unless `body` was signed with `secret`. Constant-time compare."""
    value = _header(headers, sig.header)
    if not value:
        raise WebhookError(f"Missing signature header '{sig.header}'.")
    if sig.format == "stripe":
        parts: dict[str, list[str]] = {}
        for piece in value.split(","):
            k, _, v = piece.strip().partition("=")
            parts.setdefault(k, []).append(v)
        try:
            ts = int(parts["t"][0])
        except (KeyError, ValueError):
            raise WebhookError("Signature header has no valid timestamp.") from None
        current = time.time() if now is None else now
        if abs(current - ts) > sig.tolerance_seconds:
            raise WebhookError("Signature timestamp outside the tolerance window (possible replay).")
        expected = _digest(secret, f"{ts}.".encode() + body, sig)
        # bytes, not str: compare_digest raises TypeError on non-ASCII strings
        if not any(hmac.compare_digest(expected.encode(), v.encode("utf-8", "replace"))
                   for v in parts.get("v1", [])):
            raise WebhookError("Signature mismatch.")
        return
    expected = sig.prefix + _digest(secret, body, sig)
    if not hmac.compare_digest(expected.encode(), value.strip().encode("utf-8", "replace")):
        raise WebhookError("Signature mismatch.")


def parse_webhook(
    trigger: Trigger,
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: str | bytes | None = None,
    schemas: dict[str, Any] | None = None,
    seen_ids: MutableSet[str] | None = None,
    validate: Literal["off", "warn", "raise"] = "warn",
    now: float | None = None,
) -> Any:
    """Verify and decode one delivery. Pass the raw request body bytes, exactly as
    received; re-serialized JSON will not match the signature.

    seen_ids: the delivery id is added only once the delivery has passed every check
    (signature, JSON, schema), so a sender's retry after a rejected delivery isn't
    mistaken for a duplicate. If your own processing then fails, `seen_ids.discard(id)`
    so the retry is accepted."""
    if trigger.kind != "webhook":
        raise WebhookError(f"Trigger '{trigger.name}' is not a webhook trigger.")
    if trigger.signature is not None:
        if not secret:
            raise WebhookError(f"Trigger '{trigger.name}' requires a signing secret.")
        verify_signature(secret, body, headers, trigger.signature, now=now)

    delivery = None
    if trigger.delivery_id_header and seen_ids is not None:
        delivery = _header(headers, trigger.delivery_id_header)
        if delivery and delivery in seen_ids:
            raise DuplicateDeliveryError(f"Delivery {delivery} already processed.")

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise WebhookError(f"Body is not valid JSON: {e}") from e

    if validate != "off" and trigger.payload_schema:
        schema = dict(trigger.payload_schema)
        if schemas:
            schema["$defs"] = schemas
        try:
            error = BoundedValidator(schema).best_error(payload)
        except SchemaTooLarge as e:
            raise WebhookError(f"Webhook '{trigger.name}' schema is too complex to check ({e}).") from None
        if error is not None:
            where = ".".join(map(str, error.path)) or "(root)"
            msg = f"Webhook '{trigger.name}' payload doesn't match its schema at {where}: {error.message}"
            if validate == "raise":
                raise WebhookError(msg)
            warnings.warn(msg, ResponseSchemaWarning, stacklevel=2)
    if delivery and seen_ids is not None:
        seen_ids.add(delivery)
    return payload
