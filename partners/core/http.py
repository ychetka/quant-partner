#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Small JSON-over-HTTP helpers built on Python stdlib."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import PartnerError, sanitize


def _error_code(status: int, body: str) -> str:
    lower = body.lower()
    if status in (401, 403):
        return "unauthorized"
    if status == 408 or status == 429 or "quota" in lower or "rate limit" in lower:
        return "quota_exceeded"
    if "model_not_supported" in lower or "model not supported" in lower:
        return "model_not_supported"
    if "unsupported_api_for_model" in lower or "unsupported api" in lower:
        return "unsupported_api_for_model"
    if status == 404:
        return "not_found"
    if 500 <= status <= 599:
        return "partner_unreachable"
    return "backend_error"


def _extract_message(raw: bytes, status: int) -> str:
    body = raw.decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:600] or f"HTTP {status}"
    err = data.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("type") or body[:600]
        code = err.get("code") or err.get("type")
        return f"{code}: {message}" if code else str(message)
    return body[:600] or f"HTTP {status}"


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 240,
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        message = sanitize(_extract_message(raw, e.code), secrets)
        raise PartnerError(_error_code(e.code, message), f"HTTP {e.code}: {message}") from e
    except TimeoutError as e:
        raise PartnerError("partner_unreachable", f"request timed out after {timeout}s") from e
    except urllib.error.URLError as e:
        reason = sanitize(getattr(e, "reason", e), secrets)
        raise PartnerError("partner_unreachable", f"network error: {reason}") from e
    except json.JSONDecodeError as e:
        raise PartnerError("backend_error", "backend returned malformed JSON") from e


def get_json(url: str, **kwargs: Any) -> dict[str, Any]:
    return request_json("GET", url, **kwargs)


def post_json(url: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return request_json("POST", url, payload=payload, **kwargs)
