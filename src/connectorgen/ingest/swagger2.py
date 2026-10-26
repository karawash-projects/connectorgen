"""Swagger 2.0 -> OpenAPI 3.0.3, so the rest of the pipeline only deals with OpenAPI 3.

Covers what real Swagger specs use:
- host / basePath / schemes            -> servers
- definitions, parameters, responses   -> components (with every $ref rewritten)
- body and formData parameters         -> requestBody (JSON / urlencoded / multipart)
- consumes / produces                  -> media types on bodies and responses
- type/format/items/enum/... on params -> parameter schemas; collectionFormat -> style/explode
- type: file                           -> string/binary; x-nullable -> nullable
- securityDefinitions (basic, apiKey, oauth2 implicit/password/application/accessCode)
Unknown x- extensions are carried over unchanged.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..errors import UnsupportedSpecError

METHODS = ("get", "put", "post", "delete", "options", "head", "patch")
_SCHEMA_KEYS = ("type", "format", "items", "enum", "default", "maximum", "exclusiveMaximum",
                "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern", "maxItems",
                "minItems", "uniqueItems", "multipleOf")
_JSONISH = ("application/json", "text/json")


def _is_json(media: str) -> bool:
    base = media.split(";")[0].strip().lower()
    return base in _JSONISH or base.endswith("+json")


def _escape(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


class _Converter:
    def __init__(self, spec: dict[str, Any]):
        self.src = spec
        self.consumes: list[str] = spec.get("consumes") or ["application/json"]
        self.produces: list[str] = spec.get("produces") or ["application/json"]
        self.global_params: dict[str, Any] = spec.get("parameters") or {}

    def _json_type(self) -> str:
        return next((c for c in self.produces if _is_json(c)), self.produces[0] if self.produces
                    else "application/json")

    # ---------------------------------------------------------------- refs and schemas

    def ref(self, ref: str) -> str:
        if ref.startswith("#/paths/"):
            # Swagger: .../responses/200/schema/...  ->  OpenAPI 3: .../responses/200/content/<type>/schema/...
            ref = re.sub(r"(/responses/[^/]+)/schema(?=/|$)",
                         lambda m: f"{m.group(1)}/content/{_escape(self._json_type())}/schema", ref)
            return ref
        if ref.startswith("#/definitions/"):
            return "#/components/schemas/" + ref[len("#/definitions/"):]
        if ref.startswith("#/responses/"):
            return "#/components/responses/" + ref[len("#/responses/"):]
        if ref.startswith("#/parameters/"):
            name = ref[len("#/parameters/"):]
            param = self.global_params.get(name.replace("~1", "/").replace("~0", "~"), {})
            if param.get("in") == "body":
                return "#/components/requestBodies/" + name
            return "#/components/parameters/" + name
        return ref

    def schema(self, node: Any) -> Any:
        if isinstance(node, list):
            return [self.schema(x) for x in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out[k] = self.ref(v)
            elif k == "x-nullable":
                if v:
                    out["nullable"] = True
            elif k == "discriminator" and isinstance(v, str):
                out[k] = {"propertyName": v}
            elif k in ("properties", "patternProperties", "definitions") and isinstance(v, dict):
                out[k] = {name: self.schema(sub) for name, sub in v.items()}
            elif k in ("enum", "required", "example", "default"):
                out[k] = copy.deepcopy(v)
            else:
                out[k] = self.schema(v)
        if out.get("type") == "file":
            out["type"], out["format"] = "string", "binary"
        return out

    # ---------------------------------------------------------------- parameters

    def resolve_param(self, p: dict[str, Any]) -> dict[str, Any]:
        ref = p.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/parameters/"):
            name = ref[len("#/parameters/"):].replace("~1", "/").replace("~0", "~")
            if name not in self.global_params:
                raise UnsupportedSpecError(f"Unresolvable $ref: {ref}")
            return self.global_params[name]
        return p

    def param_schema(self, p: dict[str, Any]) -> dict[str, Any]:
        schema = {k: copy.deepcopy(p[k]) for k in _SCHEMA_KEYS if k in p}
        if "items" in schema:
            schema["items"] = self.schema(schema["items"])
        if p.get("x-nullable"):
            schema["nullable"] = True
        if schema.get("type") == "file":
            schema["type"], schema["format"] = "string", "binary"
        return schema

    def parameter(self, p: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {k: copy.deepcopy(v) for k, v in p.items()
                               if k in ("name", "in", "description", "required", "deprecated",
                                        "allowEmptyValue") or k.startswith("x-")}
        if p.get("in") == "path":
            out["required"] = True
        out["schema"] = self.param_schema(p)
        fmt = p.get("collectionFormat")
        if p.get("type") == "array" and fmt:
            if fmt == "multi":
                out["style"], out["explode"] = "form", True
            elif fmt == "ssv":
                out["style"], out["explode"] = "spaceDelimited", False
            elif fmt == "pipes":
                out["style"], out["explode"] = "pipeDelimited", False
            else:  # csv (default), tsv has no OpenAPI 3 equivalent
                out["style"] = "form" if p.get("in") in ("query", "cookie") else "simple"
                out["explode"] = False
        return out

    def body_from_params(self, params: list[dict[str, Any]], consumes: list[str]) -> dict[str, Any] | None:
        body = next((p for p in params if p.get("in") == "body"), None)
        form = [p for p in params if p.get("in") == "formData"]
        if body is not None:
            schema = self.schema(body.get("schema") or {})
            types = [c for c in consumes if _is_json(c)] or consumes or ["application/json"]
            out = {"content": {ct: {"schema": schema} for ct in types}}
            if body.get("description"):
                out["description"] = body["description"]
            if body.get("required"):
                out["required"] = True
            return out
        if form:
            props, required = {}, []
            for p in form:
                props[p["name"]] = {**self.param_schema(p),
                                    **({"description": p["description"]} if p.get("description") else {})}
                if p.get("required"):
                    required.append(p["name"])
            schema: dict[str, Any] = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            has_file = any(p.get("type") == "file" for p in form)
            multipart = has_file or any(c.startswith("multipart/") for c in consumes)
            urlencoded = any(c.startswith("application/x-www-form-urlencoded") for c in consumes)
            media = "multipart/form-data" if multipart and (has_file or not urlencoded) \
                else "application/x-www-form-urlencoded"
            out = {"content": {media: {"schema": schema}}}
            if required:
                out["required"] = True
            return out
        return None

    # ---------------------------------------------------------------- responses

    def response(self, r: dict[str, Any], produces: list[str]) -> dict[str, Any]:
        if "$ref" in r:
            return {"$ref": self.ref(r["$ref"])}
        out: dict[str, Any] = {"description": r.get("description") or ""}
        if isinstance(r.get("schema"), dict):
            schema = self.schema(r["schema"])
            types = [c for c in produces if _is_json(c)] or produces or ["application/json"]
            out["content"] = {ct: {"schema": schema} for ct in types}
            examples = r.get("examples") or {}
            for ct, example in examples.items():
                if ct in out["content"]:
                    out["content"][ct]["example"] = example
        if isinstance(r.get("headers"), dict):
            out["headers"] = {name: {k: v for k, v in {
                "description": h.get("description"), "schema": self.param_schema(h)}.items() if v}
                for name, h in r["headers"].items() if isinstance(h, dict)}
        out.update({k: v for k, v in r.items() if k.startswith("x-")})
        return out

    # ---------------------------------------------------------------- top level

    def servers(self) -> list[dict[str, Any]]:
        host = self.src.get("host")
        base_path = self.src.get("basePath") or ""
        if base_path and not base_path.startswith("/"):
            base_path = "/" + base_path
        if not host:
            return [{"url": base_path or "/"}]
        schemes = self.src.get("schemes") or ["https"]
        ordered = sorted(schemes, key=lambda s: {"https": 0, "http": 1}.get(s, 2))
        return [{"url": f"{s}://{host}{base_path}"} for s in ordered if s in ("http", "https")] \
            or [{"url": f"https://{host}{base_path}"}]

    def security_schemes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, d in (self.src.get("securityDefinitions") or {}).items():
            t = d.get("type")
            desc = {"description": d["description"]} if d.get("description") else {}
            if t == "basic":
                out[name] = {"type": "http", "scheme": "basic", **desc}
            elif t == "apiKey":
                out[name] = {"type": "apiKey", "name": d.get("name"), "in": d.get("in", "header"), **desc}
            elif t == "oauth2":
                scopes = d.get("scopes") or {}
                flow = d.get("flow")
                flows: dict[str, Any] = {}
                if flow == "implicit":
                    flows["implicit"] = {"authorizationUrl": d.get("authorizationUrl"), "scopes": scopes}
                elif flow == "password":
                    flows["password"] = {"tokenUrl": d.get("tokenUrl"), "scopes": scopes}
                elif flow == "application":
                    flows["clientCredentials"] = {"tokenUrl": d.get("tokenUrl"), "scopes": scopes}
                elif flow == "accessCode":
                    flows["authorizationCode"] = {"authorizationUrl": d.get("authorizationUrl"),
                                                  "tokenUrl": d.get("tokenUrl"), "scopes": scopes}
                out[name] = {"type": "oauth2", "flows": flows, **desc}
            else:
                out[name] = copy.deepcopy(d)
        return out

    def convert(self) -> dict[str, Any]:
        src = self.src
        out: dict[str, Any] = {
            "openapi": "3.0.3",
            "info": copy.deepcopy(src.get("info") or {"title": "API", "version": ""}),
            "servers": self.servers(),
            "paths": {},
            "components": {},
            "x-converted-from": "swagger 2.0",
        }
        for key in ("tags", "externalDocs", "security"):
            if key in src:
                out[key] = copy.deepcopy(src[key])
        out.update({k: copy.deepcopy(v) for k, v in src.items() if k.startswith("x-")})

        components = out["components"]
        if src.get("definitions"):
            components["schemas"] = {k: self.schema(v) for k, v in src["definitions"].items()}
        params, bodies = {}, {}
        for name, p in self.global_params.items():
            if p.get("in") == "body":
                bodies[name] = self.body_from_params([p], self.consumes)
            elif p.get("in") != "formData":  # formData params are inlined where used
                params[name] = self.parameter(p)
        if params:
            components["parameters"] = params
        if bodies:
            components["requestBodies"] = bodies
        if src.get("responses"):
            components["responses"] = {k: self.response(v, self.produces)
                                       for k, v in src["responses"].items()}
        schemes = self.security_schemes()
        if schemes:
            components["securitySchemes"] = schemes
        if not components:
            del out["components"]

        for path, item in (src.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            new_item: dict[str, Any] = {k: copy.deepcopy(v) for k, v in item.items()
                                        if k.startswith("x-") or k in ("summary", "description")}
            shared = [self.resolve_param(p) if self._is_global_body_or_form(p) else p
                      for p in item.get("parameters") or []]
            shared_simple = [p for p in shared if self._resolved_in(p) not in ("body", "formData")]
            if shared_simple:
                new_item["parameters"] = [self._param_or_ref(p) for p in shared_simple]
            for method in METHODS:
                op = item.get(method)
                if isinstance(op, dict):
                    new_item[method] = self.operation(op, [p for p in shared
                                                           if self._resolved_in(p) in ("body", "formData")])
            out["paths"][path] = new_item
        return out

    def _resolved_in(self, p: dict[str, Any]) -> str | None:
        return self.resolve_param(p).get("in")

    def _is_global_body_or_form(self, p: dict[str, Any]) -> bool:
        return "$ref" in p and self._resolved_in(p) in ("body", "formData")

    def _param_or_ref(self, p: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in p:
            return {"$ref": self.ref(p["$ref"])}
        return self.parameter(p)

    def operation(self, op: dict[str, Any], inherited_bodies: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {k: copy.deepcopy(v) for k, v in op.items()
                               if k in ("tags", "summary", "description", "externalDocs", "operationId",
                                        "deprecated", "security") or k.startswith("x-")}
        consumes = op.get("consumes") or self.consumes
        produces = op.get("produces") or self.produces
        raw = op.get("parameters") or []
        resolved = [self.resolve_param(p) for p in raw]
        body_params = [p for p in inherited_bodies] + \
            [r for r, p in zip(resolved, raw) if r.get("in") in ("body", "formData")]
        simple = [p for r, p in zip(resolved, raw) if r.get("in") not in ("body", "formData")]
        if simple:
            out["parameters"] = [self._param_or_ref(p) for p in simple]
        body_ref = next((p["$ref"] for p in raw if "$ref" in p
                         and self._resolved_in(p) == "body"), None)
        if body_ref and not any(p.get("in") == "formData" for p in body_params):
            out["requestBody"] = {"$ref": self.ref(body_ref)}
        else:
            body = self.body_from_params([self.resolve_param(p) for p in body_params], consumes)
            if body is not None:
                out["requestBody"] = body
        out["responses"] = {str(code): self.response(r, produces)
                            for code, r in (op.get("responses") or {"default": {"description": ""}}).items()
                            if isinstance(r, dict)}
        return out


def convert_swagger2(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert a Swagger 2.0 document (dict) to an OpenAPI 3.0.3 document (dict)."""
    if str(spec.get("swagger", "")) != "2.0":
        raise UnsupportedSpecError("Not a Swagger 2.0 document ('swagger: \"2.0\"').")
    return _Converter(spec).convert()
