#!/usr/bin/env python3
"""Verify herdwatch's API usage against a released Herdr schema."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MANIFESTS = {
    "stable": "https://herdr.dev/latest.json",
    "preview": "https://herdr.dev/preview.json",
}
METHOD_PARAMS = {
    "session.snapshot": set(),
    "agent.get": {"target"},
    "pane.process_info": {"pane_id"},
    "pane.report_metadata": {"pane_id", "source", "tokens", "ttl_ms"},
    # Used only by the explicit lifecycle compatibility mode.
    "pane.report_agent": {"pane_id", "source", "agent", "state"},
    "pane.release_agent": {"pane_id", "source", "agent"},
    "events.subscribe": {"subscriptions"},
}
RESPONSE_FIELDS = {
    "SessionSnapshot": {"agents", "panes", "protocol", "version"},
    "AgentInfo": {
        "pane_id",
        "terminal_id",
        "agent",
        "agent_status",
        "cwd",
        "foreground_cwd",
        "tokens",
        "agent_session",
    },
    "PaneProcessInfo": {"shell_pid", "foreground_process_group_id"},
}


class ContractError(RuntimeError):
    pass


def _request_variants(schema: dict) -> dict[str, dict]:
    variants = {}
    for variant in schema["schemas"]["request"]["oneOf"]:
        method = variant.get("properties", {}).get("method", {}).get("const")
        if isinstance(method, str):
            variants[method] = variant
    return variants


def _resolve_request_ref(schema: dict, variant: dict) -> dict:
    params = variant.get("properties", {}).get("params", {})
    ref = params.get("$ref")
    if not isinstance(ref, str):
        return params
    prefix = "#/schemas/request/$defs/"
    if not ref.startswith(prefix):
        raise ContractError(f"unsupported request schema reference: {ref}")
    return schema["schemas"]["request"]["$defs"][ref.removeprefix(prefix)]


def source_subscriptions(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(), filename=str(source))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "GLOBAL_SUBSCRIPTIONS"
            for target in node.targets
        ):
            continue
        rows = ast.literal_eval(node.value)
        return {row["type"] for row in rows} | {"pane.agent_status_changed"}
    raise ContractError(f"GLOBAL_SUBSCRIPTIONS not found in {source}")


def validate_schema(
    schema: dict,
    subscriptions: set[str],
    *,
    manifest_protocol: int | None = None,
) -> dict[str, int]:
    protocol = schema.get("protocol")
    if not isinstance(protocol, int) or protocol < 1:
        raise ContractError("schema has no valid protocol number")
    if manifest_protocol is not None and protocol != manifest_protocol:
        raise ContractError(
            f"manifest protocol {manifest_protocol} != schema protocol {protocol}"
        )

    variants = _request_variants(schema)
    for method, sent_fields in METHOD_PARAMS.items():
        variant = variants.get(method)
        if variant is None:
            raise ContractError(f"required method missing: {method}")
        params = _resolve_request_ref(schema, variant)
        properties = set(params.get("properties", {}))
        missing = sent_fields - properties
        if missing:
            raise ContractError(
                f"{method} no longer accepts fields: {', '.join(sorted(missing))}"
            )
        newly_required = set(params.get("required", [])) - sent_fields
        if newly_required:
            raise ContractError(
                f"{method} added required fields: "
                f"{', '.join(sorted(newly_required))}"
            )

    request_defs = schema["schemas"]["request"]["$defs"]
    supported_subscriptions = {
        row.get("properties", {}).get("type", {}).get("const")
        for row in request_defs["Subscription"]["oneOf"]
    }
    missing_subscriptions = subscriptions - supported_subscriptions
    if missing_subscriptions:
        raise ContractError(
            "subscriptions missing: "
            f"{', '.join(sorted(missing_subscriptions))}"
        )

    response_defs = schema["schemas"]["success_response"]["$defs"]
    for type_name, required_fields in RESPONSE_FIELDS.items():
        definition = response_defs.get(type_name)
        if definition is None:
            raise ContractError(f"response type missing: {type_name}")
        missing_fields = required_fields - set(definition.get("properties", {}))
        if missing_fields:
            raise ContractError(
                f"{type_name} fields missing: {', '.join(sorted(missing_fields))}"
            )

    return {
        "protocol": protocol,
        "methods": len(METHOD_PARAMS),
        "subscriptions": len(subscriptions),
        "response_fields": sum(map(len, RESPONSE_FIELDS.values())),
    }


def platform_key() -> str:
    systems = {"Linux": "linux", "Darwin": "macos"}
    machines = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
    try:
        return f"{systems[platform.system()]}-{machines[platform.machine()]}"
    except KeyError as exc:
        raise ContractError(
            f"unsupported platform: {platform.system()} {platform.machine()}"
        ) from exc


def manifest_asset(manifest: dict, key: str) -> tuple[str, str]:
    asset = manifest.get("assets", {}).get(key)
    if isinstance(asset, dict):
        url, digest = asset.get("url"), asset.get("sha256")
    else:
        url = asset
        digest = manifest.get("sha256", {}).get(key)
    if not isinstance(url, str) or not isinstance(digest, str):
        raise ContractError(f"manifest has no verified asset for {key}")
    return url, digest


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "herdwatch-contract-ci"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download_binary(manifest: dict, destination: Path) -> None:
    url, expected = manifest_asset(manifest, platform_key())
    request = urllib.request.Request(url, headers={"User-Agent": "herdwatch-contract-ci"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ContractError("downloaded Herdr binary failed SHA-256 verification")
    destination.chmod(0o755)


def binary_schema(binary: Path) -> dict:
    result = subprocess.run(
        [str(binary), "api", "schema", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=sorted(MANIFESTS), default="stable")
    parser.add_argument("--schema", type=Path, help="validate an existing schema")
    parser.add_argument("--herdr", type=Path, help="validate an existing binary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest = None
    if args.schema:
        schema = json.loads(args.schema.read_text())
        label = str(args.schema)
    elif args.herdr:
        schema = binary_schema(args.herdr)
        label = str(args.herdr)
    else:
        manifest = fetch_json(MANIFESTS[args.channel])
        with tempfile.TemporaryDirectory(prefix="herdwatch-herdr-") as tmp:
            binary = Path(tmp) / "herdr"
            download_binary(manifest, binary)
            schema = binary_schema(binary)
        label = f"{args.channel} {manifest.get('version') or manifest.get('build_id')}"

    result = validate_schema(
        schema,
        source_subscriptions(root / "src/herdwatch/daemon.py"),
        manifest_protocol=manifest.get("protocol") if manifest else None,
    )
    print(
        f"OK {label}: protocol {result['protocol']}, "
        f"{result['methods']} methods, {result['subscriptions']} subscriptions, "
        f"{result['response_fields']} response fields"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
