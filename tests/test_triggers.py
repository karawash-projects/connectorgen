import json
import time

import httpx
import pytest
import respx

from connectorgen import ConnectorClient, connector_from_openapi
from connectorgen.ir import WebhookSignature
from connectorgen.triggers import (
    FileStateStore,
    MemoryStateStore,
    Poller,
    WebhookError,
    parse_webhook,
    sign_payload,
    verify_signature,
)
from connectorgen.triggers.webhooks import DuplicateDeliveryError

ISSUE = {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}}}

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Tracker", "version": "1"},
    "servers": [{"url": "https://api.tracker.test"}],
    "components": {"schemas": {"Issue": ISSUE}},
    "paths": {
        "/issues": {"get": {
            "operationId": "listIssues",
            "summary": "List issues",
            "parameters": [
                {"name": "sort", "in": "query", "schema": {"type": "string",
                                                           "enum": ["created", "updated"]}},
                {"name": "direction", "in": "query", "schema": {"type": "string",
                                                                "enum": ["asc", "desc"]}},
                {"name": "page", "in": "query", "schema": {"type": "integer"}},
                {"name": "per_page", "in": "query", "schema": {"type": "integer"}},
            ],
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Issue"}}}}}},
        }},
        "/labels": {"get": {
            "operationId": "listLabels",
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "array", "items": {"type": "object", "properties": {
                    "color": {"type": "string"}}}}}}}},
        }},
        "/subscriptions": {"post": {
            "operationId": "createSubscription",
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "callbacks": {"issueClosed": {"{$request.body#/url}": {"post": {
                "summary": "An issue was closed",
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Issue"}}}},
                "responses": {"200": {"description": "ok"}}}}}},
            "responses": {"201": {"description": "ok"}},
        }},
    },
    "webhooks": {"issue.created": {"post": {
        "summary": "An issue was created",
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "required": ["action", "issue"],
            "properties": {"action": {"type": "string"},
                           "issue": {"$ref": "#/components/schemas/Issue"}}}}}},
        "responses": {"200": {"description": "ok"}}}}},
}
BASE = "https://api.tracker.test"


@pytest.fixture
def tracker():
    return connector_from_openapi(SPEC)


# ------------------------------------------------------------------ derivation


def test_polling_trigger_derived_with_newest_first_order(tracker):
    t = next(t for t in tracker.triggers if t.kind == "polling")
    assert t.name == "new_issues"
    assert t.source_action == "list_issues"
    assert t.dedup_key == "id"
    assert t.order == "newest_first"
    assert t.inputs == {"sort": "created", "direction": "desc"}


def test_no_trigger_without_stable_id(tracker):
    assert all(t.source_action != "list_labels" for t in tracker.triggers)


def test_webhooks_and_callbacks_become_triggers(tracker):
    hooks = {t.name: t for t in tracker.triggers if t.kind == "webhook"}
    assert set(hooks) == {"on_issue_closed", "on_issue_created"}
    assert hooks["on_issue_closed"].source_action == "create_subscription"  # registers it
    assert hooks["on_issue_created"].payload_schema["required"] == ["action", "issue"]


def test_triggers_can_be_skipped():
    assert not [t for t in connector_from_openapi(SPEC, derive_triggers=False).triggers
                if t.kind == "polling"]


# ------------------------------------------------------------------ polling


def page(*ids):
    return [{"id": i, "title": f"issue {i}"} for i in ids]


def pages(*first_pages):
    """side_effect: each poll gets the next first page; any page>=2 request is empty."""
    queue = list(first_pages)

    def handler(request):
        if request.url.params.get("page", "1") != "1":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=queue.pop(0))
    return handler


def polling(connector):
    return next(t for t in connector.triggers if t.kind == "polling")


@pytest.fixture
def client(tracker):
    with ConnectorClient(tracker) as c:
        yield c


@respx.mock
def test_first_poll_is_baseline_then_only_new_items(client):
    route = respx.get(f"{BASE}/issues").mock(side_effect=pages(page(3, 2, 1), page(5, 4, 3, 2, 1)))
    poller = Poller(client, "new_issues", MemoryStateStore())
    first = poller.poll()
    assert first.baseline and first.items == []
    second = poller.poll()
    assert [i["id"] for i in second.items] == [4, 5]   # oldest first
    assert second.stopped_early                         # stopped at first seen id
    assert route.calls[0].request.url.params["sort"] == "created"
    assert route.calls[0].request.url.params["direction"] == "desc"


@respx.mock
def test_emit_on_first_run(client):
    respx.get(f"{BASE}/issues").respond(json=page(2, 1))
    result = Poller(client, "new_issues", emit_on_first_run=True).poll()
    assert [i["id"] for i in result.items] == [1, 2]


@respx.mock
def test_at_least_once_commit(client):
    respx.get(f"{BASE}/issues").mock(side_effect=pages(page(1), page(2, 1), page(2, 1)))
    poller = Poller(client, "new_issues")
    poller.poll()
    pending = poller.poll(commit=False)
    assert [i["id"] for i in pending.items] == [2]
    # processing crashed before commit: next poll delivers it again
    again = poller.poll(commit=False)
    assert [i["id"] for i in again.items] == [2]
    poller.commit(again)


@respx.mock
def test_unknown_order_pages_through_and_dedups(tracker):
    polling(tracker).order = "unknown"
    polling(tracker).inputs = {}
    respx.get(f"{BASE}/issues").mock(side_effect=lambda r: httpx.Response(
        200, json={"1": page(1, 2), "2": page(3), "3": []}.get(r.url.params.get("page", "1"))))
    with ConnectorClient(tracker) as c:
        poller = Poller(c, "new_issues", emit_on_first_run=True, page_size=2)
        assert [i["id"] for i in poller.poll().items] == [1, 2, 3]
        assert poller.poll().items == []


@respx.mock
def test_file_state_survives_restart(client, tmp_path):
    respx.get(f"{BASE}/issues").mock(side_effect=pages(page(1), page(2, 1)))
    Poller(client, "new_issues", FileStateStore(tmp_path)).poll()
    result = Poller(client, "new_issues", FileStateStore(tmp_path)).poll()  # "restarted"
    assert [i["id"] for i in result.items] == [2]


@respx.mock
def test_different_inputs_have_separate_state(client):
    store = MemoryStateStore()
    a = Poller(client, "new_issues", store, inputs={"per_page": 10})
    b = Poller(client, "new_issues", store, inputs={"per_page": 20})
    assert a.key != b.key


def test_seen_set_is_bounded(client):
    poller = Poller(client, "new_issues", seen_limit=3)
    with respx.mock:
        respx.get(f"{BASE}/issues").mock(side_effect=pages(page(5, 4, 3, 2, 1)))
        poller.poll()
    assert len(poller.store.load(poller.key)["seen"]) == 3


def test_poller_rejects_webhook_trigger(client):
    with pytest.raises(Exception, match="not a polling trigger"):
        Poller(client, "on_issue_created")


# ------------------------------------------------------------------ webhooks

BODY = json.dumps({"action": "created", "issue": {"id": 7, "title": "Bug"}}).encode()


def test_github_style_signature_roundtrip():
    sig = WebhookSignature.github()
    header = sign_payload("s3cret", BODY, sig)
    assert header.startswith("sha256=")
    verify_signature("s3cret", BODY, {"X-Hub-Signature-256": header}, sig)
    with pytest.raises(WebhookError, match="mismatch"):
        verify_signature("wrong", BODY, {"x-hub-signature-256": header}, sig)
    with pytest.raises(WebhookError, match="mismatch"):
        verify_signature("s3cret", BODY + b" ", {"X-Hub-Signature-256": header}, sig)
    with pytest.raises(WebhookError, match="Missing signature"):
        verify_signature("s3cret", BODY, {}, sig)


def test_stripe_style_signature_and_replay_window():
    sig = WebhookSignature.stripe()
    now = time.time()
    header = sign_payload("whsec", BODY, sig, timestamp=int(now))
    verify_signature("whsec", BODY, {"Stripe-Signature": header}, sig, now=now)
    with pytest.raises(WebhookError, match="tolerance"):
        verify_signature("whsec", BODY, {"Stripe-Signature": header}, sig, now=now + 3600)


def test_parse_webhook_full_path(tracker):
    trigger = next(t for t in tracker.triggers if t.name == "on_issue_created")
    trigger.signature = WebhookSignature.github()
    trigger.delivery_id_header = "X-Delivery"
    headers = {"X-Hub-Signature-256": sign_payload("k", BODY, trigger.signature),
               "X-Delivery": "d-1"}
    seen: set[str] = set()
    payload = parse_webhook(trigger, headers, BODY, secret="k", schemas=tracker.schemas,
                            seen_ids=seen, validate="raise")
    assert payload["issue"]["id"] == 7
    with pytest.raises(DuplicateDeliveryError):
        parse_webhook(trigger, headers, BODY, secret="k", seen_ids=seen)
    with pytest.raises(WebhookError, match="requires a signing secret"):
        parse_webhook(trigger, headers, BODY)


def test_webhook_payload_validation(tracker):
    trigger = next(t for t in tracker.triggers if t.name == "on_issue_created")
    bad = json.dumps({"action": "created"}).encode()
    with pytest.raises(WebhookError, match="'issue' is a required property"):
        parse_webhook(trigger, {}, bad, schemas=tracker.schemas, validate="raise")
    with pytest.warns(UserWarning):
        assert parse_webhook(trigger, {}, bad, schemas=tracker.schemas) == {"action": "created"}


def test_trigger_roundtrips_through_json(tracker, tmp_path):
    tracker.triggers[0].signature = WebhookSignature.stripe()
    path = tmp_path / "c.json"
    tracker.save(path)
    from connectorgen import Connector
    assert Connector.load(path).triggers == tracker.triggers
