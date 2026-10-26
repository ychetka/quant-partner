#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Universal quant-partner MCP stdio entrypoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.mcp import log, serve
from core.server import PartnerMCPServer


def main() -> None:
    partner_id = os.environ.get("PARTNER_ID", "")
    if not partner_id:
        log("WARN: PARTNER_ID is not set; partner_info and calls will report invalid_config")
    serve(PartnerMCPServer(partner_id))


if __name__ == "__main__":
    main()
