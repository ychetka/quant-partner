#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Local JSON-RPC smoke client for quant-partner MCP servers.

Beyond basic initialize/tools/list/tools-call plumbing, the client can
assert on the advertised tool surface (``--expect-tool`` /
``--forbid-tool``) and validate a tools/call text payload as the
advisory review artifact (``--expect-artifact-status`` /
``--expect-error-code``). Every check fails closed and failure output
never echoes server lines or artifact content.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "partners" / "mcp_partner.py"
SCHEMA = ROOT / "partners" / "review" / "schema.py"


def _load_schema() -> Any:
    """Import ``partners/review/schema.py`` directly; stdlib-only, no deps."""
    spec = importlib.util.spec_from_file_location("review_schema_for_smoke", SCHEMA)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load review schema module")
    module = importlib.util.module_from_spec(spec)
    # Registration before exec lets dataclass fields resolve the module.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _first_text(result: Any) -> str | None:
    """Extract ``result.content[0].text`` from a tools/call result."""
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict):
        return None
    text = first.get("text")
    return text if isinstance(text, str) else None


def _issue_codes(issues: Any) -> str:
    """Render stable issue codes only; paths may echo attacker-controlled keys."""
    codes = sorted({issue.code for issue in issues})
    return ", ".join(codes) if codes else "schema_invalid"


def _check_artifact(text: str, args: argparse.Namespace) -> bool:
    """Validate call text as an advisory artifact; True when a check fails.

    Error output reports stable issue codes only - never issue paths,
    exception text or the artifact content itself, all of which may
    embed attacker-controlled key names.
    """
    try:
        schema = _load_schema()
    except Exception:
        print("smoke: review schema module failed to load", file=sys.stderr)
        return True
    try:
        artifact = schema.extract_json(text)
    except schema.SchemaError as e:
        print(
            f"smoke: tools/call text is not a parseable artifact: {_issue_codes(e.issues)}",
            file=sys.stderr,
        )
        return True
    except Exception:
        print("smoke: tools/call text is not a parseable artifact", file=sys.stderr)
        return True
    issues = schema.validate(artifact)
    if issues:
        print(
            f"smoke: artifact failed schema validation: {_issue_codes(issues)}",
            file=sys.stderr,
        )
        return True
    if args.expect_artifact_status and artifact["status"] != args.expect_artifact_status:
        print(
            f"smoke: artifact status {artifact['status']!r} "
            f"!= expected {args.expect_artifact_status!r}",
            file=sys.stderr,
        )
        return True
    if args.expect_error_code:
        codes = {
            entry.get("code") for entry in artifact.get("errors", []) if isinstance(entry, dict)
        }
        if args.expect_error_code not in codes:
            print(
                f"smoke: expected error code {args.expect_error_code!r} "
                "not present in artifact errors",
                file=sys.stderr,
            )
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="run initialize and tools/list")
    parser.add_argument("--call", help="tool name to call")
    parser.add_argument("--question", help="question argument for ask_* tools")
    parser.add_argument("--skill", help="skill override")
    parser.add_argument("--model", help="model override")
    parser.add_argument("--role", help="model role override")
    parser.add_argument(
        "--arguments-json",
        help="JSON object used as tools/call arguments; --question/--skill/"
        "--model/--role override individual keys",
    )
    parser.add_argument(
        "--expect-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="require tool NAME in tools/list output; repeatable",
    )
    parser.add_argument(
        "--forbid-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="fail when tool NAME appears in tools/list output; repeatable",
    )
    parser.add_argument(
        "--expect-artifact-status",
        choices=("ok", "rejected", "error"),
        help="validate the call result as an advisory artifact with this status",
    )
    parser.add_argument(
        "--expect-error-code",
        metavar="CODE",
        help="require CODE among the artifact's errors entries",
    )
    args = parser.parse_args()

    if args.arguments_json is not None and not args.call:
        sys.exit("smoke: --arguments-json requires --call")
    if (args.expect_artifact_status or args.expect_error_code) and not args.call:
        sys.exit("smoke: artifact expectations require --call")

    partner_id = os.environ.get("PARTNER_ID", "")
    if not partner_id:
        sys.exit("PARTNER_ID is required")
    env = dict(os.environ)
    env.setdefault("PARTNER_CONFIG", str(ROOT / "partners" / "config.json"))

    requests = [
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    ]
    tool_checks = bool(args.expect_tool or args.forbid_tool)
    if args.list or not args.call or tool_checks:
        requests.append({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})
    if args.call:
        arguments: dict[str, Any] = {}
        if args.arguments_json is not None:
            try:
                parsed = json.loads(args.arguments_json)
            except json.JSONDecodeError:
                sys.exit("smoke: --arguments-json is not valid JSON")
            if not isinstance(parsed, dict):
                sys.exit("smoke: --arguments-json must decode to a JSON object")
            arguments = parsed
        if args.question:
            arguments["question"] = args.question
        if args.skill:
            arguments["skill"] = args.skill
        if args.model:
            arguments["model"] = args.model
        if args.role:
            arguments["role"] = args.role
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": "call",
                "method": "tools/call",
                "params": {"name": args.call, "arguments": arguments},
            }
        )

    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(json.dumps(x) for x in requests) + "\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=600,
    )
    expected_ids = [request["id"] for request in requests]
    seen_ids: set[Any] = set()
    results: dict[Any, dict[str, Any]] = {}
    failed = False
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print("smoke: suppressed non-JSON line on server stdout", file=sys.stderr)
            failed = True
            continue
        if not isinstance(message, dict):
            print("smoke: suppressed non-object JSON line on server stdout", file=sys.stderr)
            failed = True
            continue
        print(json.dumps(message, ensure_ascii=False, indent=2))
        if "id" in message:
            seen_ids.add(message["id"])
            result = message.get("result")
            if isinstance(result, dict):
                results[message["id"]] = result
        result = message.get("result")
        if message.get("error") or (isinstance(result, dict) and result.get("isError")):
            failed = True
    missing_ids = [rid for rid in expected_ids if rid not in seen_ids]
    if missing_ids:
        print(f"smoke: missing responses for request ids: {missing_ids}", file=sys.stderr)
        failed = True
    if tool_checks:
        tools_result = results.get("tools")
        tool_entries = tools_result.get("tools") if isinstance(tools_result, dict) else None
        if not isinstance(tool_entries, list):
            print("smoke: tools/list response missing or malformed", file=sys.stderr)
            failed = True
        else:
            names = {entry.get("name") for entry in tool_entries if isinstance(entry, dict)}
            for name in args.expect_tool:
                if name not in names:
                    print(f"smoke: expected tool not advertised: {name}", file=sys.stderr)
                    failed = True
            for name in args.forbid_tool:
                if name in names:
                    print(f"smoke: forbidden tool advertised: {name}", file=sys.stderr)
                    failed = True
    if args.expect_artifact_status or args.expect_error_code:
        text = _first_text(results.get("call"))
        if text is None:
            print(
                "smoke: tools/call response carries no text content to validate",
                file=sys.stderr,
            )
            failed = True
        elif _check_artifact(text, args):
            failed = True
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return 1 if failed else proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
