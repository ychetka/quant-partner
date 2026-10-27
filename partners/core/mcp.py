#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Newline-delimited JSON-RPC transport for MCP stdio servers."""

from __future__ import annotations

import json
import sys
from typing import Any, Protocol

from .errors import PartnerError, sanitize


class ToolServer(Protocol):
    def server_info(self) -> dict[str, str]: ...
    def tools(self) -> list[dict[str, Any]]: ...
    def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...
    def secrets(self) -> list[str]: ...


def log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(rid: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle_message(server: ToolServer, msg: dict[str, Any]) -> None:
    """Handle one decoded JSON-RPC message; malformed frames are ignored safely."""
    if not isinstance(msg, dict):
        return
    rid = msg.get("id")
    method = str(msg.get("method", ""))
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        _result(
            rid,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": server.server_info(),
            },
        )
    elif method in ("notifications/initialized", "initialized"):
        return
    elif method == "ping":
        _result(rid, {})
    elif method == "tools/list":
        _result(rid, {"tools": server.tools()})
    elif method == "tools/call":
        name = str(params.get("name", ""))
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            text = server.call_tool(name, arguments)
            _result(rid, {"content": [{"type": "text", "text": text}]})
        except PartnerError as e:
            text = sanitize(e.to_text(), server.secrets())
            _result(rid, {"isError": True, "content": [{"type": "text", "text": text}]})
        except Exception as e:
            log(f"internal error in {name}: {type(e).__name__}")
            text = "ERROR internal_error: partner backend failed; check server logs"
            _result(rid, {"isError": True, "content": [{"type": "text", "text": text}]})
    elif rid is not None:
        _error(rid, -32601, f"unsupported method {method}")


def serve(server: ToolServer) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle_message(server, msg)
