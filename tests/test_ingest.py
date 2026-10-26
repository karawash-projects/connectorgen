import pytest

from connectorgen import Connector, connector_from_openapi
from connectorgen.ingest.openapi import snake_case


def test_basic_metadata(petstore):
    assert petstore.name == "pet_store"
    assert petstore.api_version == "2.1.0"
    assert petstore.base_url == "https://us.api.petstore.test/v1"  # server variable filled
    assert petstore.spec_hash.startswith("sha256:")


def test_action_names(petstore):
    names = [a.name for a in petstore.actions]
    assert names == [
        "list_pets", "create_pet", "get_pet", "delete_pet",
        "get_orders", "list_customers", "list_categories", "list_events",
    ]


def test_auth_schemes(petstore):
    by_id = {s.id: s for s in petstore.auth}
    assert by_id["ApiKeyAuth"].type == "api_key"
    assert by_id["ApiKeyAuth"].param_name == "X-API-Key"
    assert by_id["ApiKeyAuth"].location == "header"
    assert by_id["BearerAuth"].type == "bearer"


def test_path_level_ref_param_inherited(petstore):
    get_pet = petstore.action("get_pet")
    (param,) = get_pet.params
    assert (param.name, param.location, param.required) == ("petId", "path", True)
    assert param.json_schema == {"type": "integer"}


def test_body_and_nullable(petstore):
    create = petstore.action("create_pet")
    assert create.body_required
    assert create.body_schema == {"$ref": "#/$defs/Pet"}
    pet = petstore.schemas["Pet"]
    assert pet["required"] == ["name"]
    assert pet["properties"]["tag"]["type"] == ["string", "null"]
    assert create.input_schema()["required"] == ["body"]
    assert petstore.input_schema("create_pet")["$defs"]["Pet"] == pet


def test_only_used_schemas_collected(petstore):
    assert set(petstore.schemas) == {"Pet", "Category"}


def test_property_names_are_not_treated_as_keywords():
    spec = {
        "openapi": "3.0.0", "info": {"title": "X", "version": "1"},
        "paths": {"/x": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"example": {"type": "string"}, "nullable": {"type": "boolean"}},
            }}}},
            "responses": {"204": {"description": "ok"}},
        }}},
    }
    body = connector_from_openapi(spec).action("post_x").body_schema
    assert set(body["properties"]) == {"example", "nullable"}


def test_nullable_ref_becomes_anyof():
    spec = {
        "openapi": "3.0.0", "info": {"title": "X", "version": "1"},
        "components": {"schemas": {"A": {"type": "string"}}},
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/A", "nullable": True}}}}}}}},
    }
    out = connector_from_openapi(spec).action("get_x").output_schema
    assert out == {"anyOf": [{"$ref": "#/$defs/A"}, {"type": "null"}]}


def test_destructive_flag(petstore):
    assert petstore.action("delete_pet").destructive
    assert not petstore.action("delete_pet").safe
    assert petstore.action("list_pets").safe


def test_cursor_pagination_nested(petstore):
    p = petstore.action("list_pets").pagination
    assert p.style == "cursor"
    assert p.request_param == "cursor"
    assert p.limit_param == "limit"
    assert p.items_path == "data"
    assert p.response_cursor_path == "meta.next_cursor"


def test_offset_pagination_bare_array(petstore):
    p = petstore.action("get_orders").pagination
    assert (p.style, p.request_param, p.limit_param, p.items_path) == ("offset", "offset", "limit", None)


def test_item_cursor_pagination(petstore):
    p = petstore.action("list_customers").pagination
    assert p.style == "cursor"
    assert p.cursor_from_item_field == "id"
    assert p.has_more_path == "has_more"


def test_url_cursor_pagination(petstore):
    p = petstore.action("list_events").pagination
    assert (p.style, p.items_path, p.response_cursor_path) == ("cursor", "events", "next")


def test_recursive_schema_kept_as_ref(petstore):
    schema = petstore.action("list_categories").output_schema
    assert schema == {"type": "array", "items": {"$ref": "#/$defs/Category"}}
    children = petstore.schemas["Category"]["properties"]["children"]["items"]
    assert children == {"$ref": "#/$defs/Category"}


def test_save_load_roundtrip(petstore, tmp_path):
    out = tmp_path / "c.json"
    petstore.save(out)
    assert Connector.load(out) == petstore


def test_spec_hash_is_stable(petstore_path):
    assert connector_from_openapi(petstore_path).spec_hash == connector_from_openapi(petstore_path).spec_hash


def test_notes_when_pagination_unresolvable():
    spec = {
        "openapi": "3.0.0", "info": {"title": "X", "version": "1"},
        "paths": {"/things": {"get": {
            "parameters": [{"name": "cursor", "in": "query", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object", "properties": {"things": {"type": "array"}}}}}}},
        }}},
    }
    action = connector_from_openapi(spec).action("get_things")
    assert action.pagination.style == "link_header"
    assert action.pagination.items_path == "things"
    assert action.notes and "Link-header" in action.notes[0]


def test_swagger2_is_converted(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{"swagger": "2.0", "info": {"title": "Old", "version": "1"}, "host": "old.test", '
                 '"paths": {"/x": {"get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}}}}')
    c = connector_from_openapi(f)
    assert c.base_url == "https://old.test" and c.action("get_x")
    assert "Converted from Swagger 2.0." in c.notes


@pytest.mark.parametrize("raw,expected", [
    ("listPets", "list_pets"),
    ("getHTTPResponse", "get_http_response"),
    ("pets.find-by-tag", "pets_find_by_tag"),
])
def test_snake_case(raw, expected):
    assert snake_case(raw) == expected
