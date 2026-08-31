from copy import deepcopy

import pytest

from scripts.check_herdr_api import (
    ContractError,
    METHOD_PARAMS,
    RESPONSE_FIELDS,
    manifest_asset,
    validate_schema,
)


SUBSCRIPTIONS = {
    "pane.created",
    "pane.closed",
    "pane.exited",
    "pane.moved",
    "workspace.closed",
    "tab.closed",
    "pane.agent_status_changed",
}


def complete_schema():
    request_defs = {
        "Subscription": {
            "oneOf": [
                {"properties": {"type": {"const": event}}}
                for event in SUBSCRIPTIONS
            ]
        }
    }
    variants = []
    for index, (method, fields) in enumerate(METHOD_PARAMS.items()):
        name = f"Params{index}"
        request_defs[name] = {
            "properties": {field: {"type": "string"} for field in fields},
            "required": sorted(fields),
        }
        variants.append(
            {
                "properties": {
                    "method": {"const": method},
                    "params": {"$ref": f"#/schemas/request/$defs/{name}"},
                }
            }
        )
    response_defs = {
        name: {"properties": {field: {} for field in fields}}
        for name, fields in RESPONSE_FIELDS.items()
    }
    return {
        "protocol": 20,
        "schemas": {
            "request": {"oneOf": variants, "$defs": request_defs},
            "success_response": {"$defs": response_defs},
        },
    }


def test_complete_contract_passes():
    result = validate_schema(complete_schema(), SUBSCRIPTIONS, manifest_protocol=20)
    assert result["methods"] == len(METHOD_PARAMS)
    assert result["subscriptions"] == len(SUBSCRIPTIONS)


def test_missing_method_fails():
    schema = complete_schema()
    schema["schemas"]["request"]["oneOf"].pop()
    with pytest.raises(ContractError, match="required method missing"):
        validate_schema(schema, SUBSCRIPTIONS)


def test_new_required_request_field_fails():
    schema = complete_schema()
    params = schema["schemas"]["request"]["$defs"]["Params0"]
    params["properties"]["new_field"] = {"type": "string"}
    params["required"].append("new_field")
    with pytest.raises(ContractError, match="added required fields: new_field"):
        validate_schema(schema, SUBSCRIPTIONS)


def test_missing_subscription_fails():
    schema = complete_schema()
    schema["schemas"]["request"]["$defs"]["Subscription"]["oneOf"].pop()
    with pytest.raises(ContractError, match="subscriptions missing"):
        validate_schema(schema, SUBSCRIPTIONS)


def test_missing_response_field_fails():
    schema = complete_schema()
    del schema["schemas"]["success_response"]["$defs"]["AgentInfo"]["properties"]["tokens"]
    with pytest.raises(ContractError, match="AgentInfo fields missing: tokens"):
        validate_schema(schema, SUBSCRIPTIONS)


def test_manifest_asset_supports_stable_and_preview_shapes():
    stable = {"assets": {"linux-x86_64": "https://stable"}, "sha256": {"linux-x86_64": "abc"}}
    preview = {"assets": {"linux-x86_64": {"url": "https://preview", "sha256": "def"}}}
    assert manifest_asset(stable, "linux-x86_64") == ("https://stable", "abc")
    assert manifest_asset(preview, "linux-x86_64") == ("https://preview", "def")


def test_manifest_protocol_must_match_binary_schema():
    with pytest.raises(ContractError, match="manifest protocol 21"):
        validate_schema(deepcopy(complete_schema()), SUBSCRIPTIONS, manifest_protocol=21)
