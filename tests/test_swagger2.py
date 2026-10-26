import json

import pytest
import respx
import yaml
from typer.testing import CliRunner

from connectorgen import ConnectorClient, Credentials, UnsupportedSpecError, connector_from_openapi
from connectorgen.cli import app
from connectorgen.ingest.swagger2 import convert_swagger2
from connectorgen.validate import run_contract_tests

SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "Zoo", "version": "1.4"},
    "host": "api.zoo.test",
    "basePath": "/v1",
    "schemes": ["http", "https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "securityDefinitions": {
        "key": {"type": "apiKey", "in": "header", "name": "X-Zoo-Key"},
        "basicAuth": {"type": "basic"},
        "oauthCode": {"type": "oauth2", "flow": "accessCode", "authorizationUrl": "https://zoo.test/auth",
                      "tokenUrl": "https://zoo.test/token", "scopes": {"read": "Read"}},
        "oauthApp": {"type": "oauth2", "flow": "application", "tokenUrl": "https://zoo.test/token",
                     "scopes": {}},
    },
    "security": [{"key": []}],
    "parameters": {
        "limitParam": {"name": "limit", "in": "query", "type": "integer", "maximum": 100},
        "animalBody": {"name": "animal", "in": "body", "required": True,
                       "schema": {"$ref": "#/definitions/Animal"}},
    },
    "responses": {"NotFound": {"description": "Not found", "schema": {"$ref": "#/definitions/Error"}}},
    "definitions": {
        "Animal": {"type": "object", "required": ["name"], "discriminator": "kind", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}, "kind": {"type": "string"},
            "nickname": {"type": "string", "x-nullable": True},
            "tags": {"type": "array", "items": {"type": "string"}}}},
        "Error": {"type": "object", "properties": {"message": {"type": "string"}}},
    },
    "paths": {
        "/animals": {
            "get": {"operationId": "listAnimals", "summary": "List animals", "parameters": [
                {"$ref": "#/parameters/limitParam"},
                {"name": "offset", "in": "query", "type": "integer"},
                {"name": "ids", "in": "query", "type": "array", "items": {"type": "integer"},
                 "collectionFormat": "csv"},
                {"name": "tag", "in": "query", "type": "array", "items": {"type": "string"},
                 "collectionFormat": "multi"}],
                "responses": {"200": {"description": "ok", "schema": {
                    "type": "array", "items": {"$ref": "#/definitions/Animal"}},
                    "headers": {"X-Total": {"type": "integer"}}}}},
            "post": {"operationId": "createAnimal", "parameters": [{"$ref": "#/parameters/animalBody"}],
                     "responses": {"201": {"description": "created", "schema": {"$ref": "#/definitions/Animal"}},
                                   "404": {"$ref": "#/responses/NotFound"}}},
        },
        "/animals/{animalId}": {
            "parameters": [{"name": "animalId", "in": "path", "required": True, "type": "integer"}],
            "get": {"operationId": "getAnimal", "responses": {
                "200": {"description": "ok", "schema": {"$ref": "#/definitions/Animal"}}}},
        },
        "/animals/{animalId}/photo": {"post": {
            "operationId": "uploadPhoto", "consumes": ["multipart/form-data"],
            "parameters": [{"name": "animalId", "in": "path", "required": True, "type": "integer"},
                           {"name": "file", "in": "formData", "type": "file", "required": True},
                           {"name": "caption", "in": "formData", "type": "string"}],
            "responses": {"200": {"description": "ok"}}}},
        "/feedings": {"post": {
            "operationId": "logFeeding", "consumes": ["application/x-www-form-urlencoded"],
            "parameters": [{"name": "food", "in": "formData", "type": "string", "required": True,
                            "enum": ["fish", "hay"]},
                           {"name": "grams", "in": "formData", "type": "integer"}],
            "responses": {"204": {"description": "logged"}}}},
    },
}


@pytest.fixture
def converted():
    return convert_swagger2(SWAGGER)


def test_servers_prefer_https(converted):
    assert [s["url"] for s in converted["servers"]] == ["https://api.zoo.test/v1", "http://api.zoo.test/v1"]


def test_definitions_and_refs_rewritten(converted):
    text = json.dumps(converted)
    assert "#/definitions/" not in text and '"#/parameters/' not in text and '"#/responses/' not in text
    animal = converted["components"]["schemas"]["Animal"]
    assert animal["discriminator"] == {"propertyName": "kind"}
    assert animal["properties"]["nickname"]["nullable"] is True


def test_body_and_global_params(converted):
    post = converted["paths"]["/animals"]["post"]
    assert post["requestBody"] == {"$ref": "#/components/requestBodies/animalBody"}
    body = converted["components"]["requestBodies"]["animalBody"]
    assert body["required"] and body["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/Animal"
    assert converted["components"]["parameters"]["limitParam"]["schema"] == {"type": "integer", "maximum": 100}


def test_collection_formats(converted):
    params = {p.get("name"): p for p in converted["paths"]["/animals"]["get"]["parameters"] if "name" in p}
    assert (params["ids"]["style"], params["ids"]["explode"]) == ("form", False)
    assert (params["tag"]["style"], params["tag"]["explode"]) == ("form", True)


def test_form_data_bodies(converted):
    upload = converted["paths"]["/animals/{animalId}/photo"]["post"]["requestBody"]
    schema = upload["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}
    assert schema["required"] == ["file"]
    feed = converted["paths"]["/feedings"]["post"]["requestBody"]
    assert feed["content"]["application/x-www-form-urlencoded"]["schema"]["properties"]["food"]["enum"] == ["fish", "hay"]


def test_responses_and_headers(converted):
    ok = converted["paths"]["/animals"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["items"]["$ref"] == "#/components/schemas/Animal"
    assert ok["headers"]["X-Total"]["schema"] == {"type": "integer"}
    assert converted["paths"]["/animals"]["post"]["responses"]["404"] == {"$ref": "#/components/responses/NotFound"}


def test_security_definitions(converted):
    schemes = converted["components"]["securitySchemes"]
    assert schemes["basicAuth"] == {"type": "http", "scheme": "basic"}
    assert schemes["oauthCode"]["flows"]["authorizationCode"]["tokenUrl"] == "https://zoo.test/token"
    assert schemes["oauthApp"]["flows"]["clientCredentials"]["tokenUrl"] == "https://zoo.test/token"


def test_connector_end_to_end():
    c = connector_from_openapi(SWAGGER)
    assert c.base_url == "https://api.zoo.test/v1"
    assert {s.type for s in c.auth} == {"api_key", "basic", "oauth2"}
    assert [f.flow for s in c.auth for f in s.oauth_flows] == ["authorization_code", "client_credentials"]
    assert c.action("list_animals").pagination.style == "offset"
    assert any(t.name == "new_animals" for t in c.triggers)
    assert c.action("upload_photo").notes  # multipart bodies aren't modeled yet; noted
    report = run_contract_tests(c)
    assert report.ok, report.to_text()


@respx.mock
def test_requests_are_correct():
    c = connector_from_openapi(SWAGGER)
    lst = respx.get("https://api.zoo.test/v1/animals").respond(json=[])
    feed = respx.post("https://api.zoo.test/v1/feedings").respond(204)
    with ConnectorClient(c, Credentials(api_key="k")) as client:
        client.execute("list_animals", ids=[1, 2, 3], tag=["a", "b"], limit=10)
        client.execute("log_feeding", body={"food": "fish", "grams": 200})
    req = lst.calls.last.request
    assert req.headers["X-Zoo-Key"] == "k"
    assert req.url.params["ids"] == "1,2,3"                       # csv
    assert req.url.params.get_list("tag") == ["a", "b"]          # multi
    assert feed.calls.last.request.content == b"food=fish&grams=200"


def test_relative_base_path_without_host():
    spec = {**SWAGGER}
    spec.pop("host")
    assert convert_swagger2(spec)["servers"] == [{"url": "/v1"}]


def test_not_swagger():
    with pytest.raises(UnsupportedSpecError, match="Not a Swagger 2.0"):
        convert_swagger2({"openapi": "3.0.0"})


def test_cli_convert_and_generate(tmp_path):
    src = tmp_path / "swagger.yaml"
    src.write_text(yaml.safe_dump(SWAGGER))
    runner = CliRunner()
    r = runner.invoke(app, ["convert", str(src), "-o", str(tmp_path / "openapi.yaml")])
    assert r.exit_code == 0 and "OpenAPI 3.0.3, 4 paths" in r.output
    assert yaml.safe_load((tmp_path / "openapi.yaml").read_text())["openapi"] == "3.0.3"
    r = runner.invoke(app, ["generate", str(src), "-o", str(tmp_path / "zoo.json")])
    assert r.exit_code == 0 and "Converted from Swagger 2.0" in r.output


def test_refs_into_swagger_response_structure_are_rewritten():
    spec = {"swagger": "2.0", "info": {"title": "W", "version": "1"}, "host": "w.test", "paths": {
        "/word/{id}": {"get": {"operationId": "getWord", "parameters": [
            {"name": "id", "in": "path", "required": True, "type": "string"}],
            "responses": {"200": {"description": "ok", "schema": {"type": "object", "properties": {
                "glosses": {"type": "array", "items": {"type": "string"}}}}}}}},
        "/glosses": {"get": {"operationId": "glosses", "responses": {"200": {"description": "ok", "schema": {
            "$ref": "#/paths/~1word~1%7Bid%7D/get/responses/200/schema/properties/glosses"}}}}}}}
    c = connector_from_openapi(spec)
    assert c.action("glosses").output_schema == {"type": "array", "items": {"type": "string"}}
