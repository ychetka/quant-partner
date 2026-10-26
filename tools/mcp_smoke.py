#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Local JSON-RPC smoke client for quant-partner MCP servers."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "partners" / "mcp_partner.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="run initialize and tools/list")
    parser.add_argument("--call", help="tool name to call")
    parser.add_argument("--question", help="question argument for ask_* tools")
    parser.add_argument("--skill", help="skill override")
    parser.add_argument("--model", help="model override")
    parser.add_argument("--role", help="model role override")
    args = parser.parse_args()

    partner_id = os.environ.get("PARTNER_ID", "")
    if not partner_id:
        sys.exit("PARTNER_ID is required")
    env = dict(os.environ)
    env.setdefault("PARTNER_CONFIG", str(ROOT / "partners" / "config.json"))

    requests = [
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    ]
    if args.list or not args.call:
        requests.append({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})
    if args.call:
        arguments = {}
        if args.question:
            arguments["question"] = args.question
        if args.skill:
            arguments["skill"] = args.skill
        if args.model:
            arguments["model"] = args.model
        if args.role:
            arguments["role"] = args.role
        requests.append({
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {"name": args.call, "arguments": arguments},
        })

    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(json.dumps(x) for x in requests) + "\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=600,
    )
    failed = False
    for line in proc.stdout.splitlines():
        message = json.loads(line)
        print(json.dumps(message, ensure_ascii=False, indent=2))
        result = message.get("result")
        if message.get("error") or (isinstance(result, dict) and result.get("isError")):
            failed = True
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return 1 if failed else proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
