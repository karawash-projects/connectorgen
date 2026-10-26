import json

import pytest
import respx
from typer.testing import CliRunner

from connectorgen import ConnectorError, Credentials
from connectorgen.cli import app
from connectorgen.validate.smoke import run_smoke_tests

BASE = "https://us.api.petstore.test/v1"


def no_sleep(_):
    pass


def run(connector, **kw):
    return run_smoke_tests(connector, Credentials(api_key="k"), sleep=no_sleep, **kw)


@respx.mock
def test_only_get_actions_are_called_and_ids_are_chained(petstore):
    respx.get(f"{BASE}/pets").respond(json={"data": [{"id": 7, "name": "Rex"}],
                                            "meta": {"next_cursor": None}})
    pet = respx.get(f"{BASE}/pets/7").respond(json={"id": 7, "name": "Rex"})
    respx.get(url__regex=rf"{BASE}/(orders|customers|categories|events).*").respond(json=[])
    report = run(petstore)
    methods = {c.request.method for c in respx.calls}
    assert methods == {"GET"}                                      # never POST/DELETE
    assert pet.called                                              # petId came from list_pets
    statuses = {r.action: r.status for r in report.results}
    assert "create_pet" not in statuses and "delete_pet" not in statuses
    assert statuses["get_pet"] == "ok"
    assert respx.calls[0].request.url.params["limit"] == "2"       # small page


def test_refuses_unsafe_actions_even_when_named(petstore):
    with pytest.raises(ConnectorError, match="only run read-only GET"):
        run(petstore, actions=["delete_pet"])


@respx.mock
def test_skips_when_param_unknown(petstore):
    respx.get(f"{BASE}/pets").respond(json={"data": [], "meta": {}})
    report = run(petstore, actions=["list_pets", "get_pet"])
    get_pet = next(r for r in report.results if r.action == "get_pet")
    assert get_pet.status == "skipped" and "--param petId=" in get_pet.detail


@respx.mock
def test_global_params_fill_required(petstore):
    route = respx.get(f"{BASE}/pets/42").respond(json={"id": 42, "name": "Max"})
    report = run(petstore, actions=["get_pet"], global_params={"petId": 42})
    assert route.called and report.results[0].status == "ok"


@respx.mock
def test_drift_reported(petstore):
    respx.get(f"{BASE}/pets/1").respond(json={"id": "one", "name": "Rex"})
    report = run(petstore, actions=["get_pet"], global_params={"petId": 1})
    assert report.results[0].status == "drift"
    assert "at id" in report.results[0].detail
    assert report.ok  # drift is a finding, not a failure


@respx.mock
def test_stops_on_401(petstore):
    respx.get(f"{BASE}/pets").respond(401, json={"error": "bad key"})
    route = respx.get(url__regex=rf"{BASE}/(orders|customers|categories|events).*").respond(json=[])
    report = run(petstore)
    assert "credentials rejected" in report.aborted
    assert not route.called and not report.ok


@respx.mock
def test_stops_when_rate_limited(petstore):
    respx.get(f"{BASE}/pets").respond(429, headers={"Retry-After": "120"})
    route = respx.get(url__regex=rf"{BASE}/(orders|customers|categories|events).*").respond(json=[])
    report = run(petstore)
    assert "rate limited" in report.aborted and not route.called


@respx.mock
def test_cli_smoke_json_report(petstore, tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTORGEN_API_KEY", "k")
    path = tmp_path / "pets.json"
    petstore.save(path)
    respx.get(f"{BASE}/pets/3").respond(json={"id": 3, "name": "Rex"})
    out = tmp_path / "smoke.json"
    result = CliRunner().invoke(app, ["smoke", str(path), "-a", "get_pet", "--param", "petId=3",
                                      "--json", str(out), "--delay", "0"])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["results"][0]["status"] == "ok"
