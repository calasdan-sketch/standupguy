"""Stdio MCP server: let your coding agent ask what you shipped.

Tools:
  what_did_i_ship {since?, author?} -> standup digest markdown
"""

from __future__ import annotations

import json
import os
import sys

from . import digest as digest_mod

SERVER_INFO = {"name": "standupguy", "version": "0.1.0"}


def handle(request: dict, root: str) -> dict | None:
    req_id = request.get("id")
    method = request.get("method")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05",
                   "capabilities": {"tools": {}},
                   "serverInfo": SERVER_INFO})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return ok({"tools": [{
            "name": "what_did_i_ship",
            "description": ("Get a Markdown digest of what was shipped in a "
                            "git repo since a date: commits grouped by type, "
                            "in-progress files. Local-only, no cloud."),
            "inputSchema": {"type": "object", "properties": {
                "since": {"type": "string",
                          "description": "git date: 'yesterday', '7 days ago', "
                                         "'2026-10-01' (default: yesterday)"},
                "author": {"type": "string",
                           "description": "filter by author (default: repo user)"},
                "root": {"type": "string",
                         "description": "repo path (default: server's root)"}}},
        }]})
    if method == "tools/call":
        params = request.get("params", {})
        if params.get("name") != "what_did_i_ship":
            return err(-32602, f"unknown tool: {params.get('name')}")
        args = params.get("arguments", {})
        since = args.get("since") or "yesterday"
        r = args.get("root") or root
        try:
            md = digest_mod.digest_markdown(
                r, since, f"since {since}", args.get("author"))
        except Exception as e:
            return ok({"content": [{"type": "text",
                                    "text": json.dumps({"error": str(e)})}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": md}]})
    return err(-32601, f"unknown method: {method}")


def serve(root: str | None = None) -> int:
    root = root or os.getcwd()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(request, root)
        except Exception as e:
            rid = request.get("id") if isinstance(request, dict) else None
            response = {"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32603, "message": str(e)}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0
