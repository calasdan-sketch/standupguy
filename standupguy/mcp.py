"""Stdio MCP server: let your coding agent ask what you shipped.

Tools:
  what_did_i_ship {since?, until?, author?, all?, root?} -> digest markdown

The wire is newline-delimited JSON-RPC in UTF-8 BYTES, whatever the
console code page (a cp1252 Windows console must not be able to kill the
server with an author called Łukasz).
"""

from __future__ import annotations

import io
import json
import os
import sys

from . import digest as digest_mod
from . import gitlog as gitlog_mod

SERVER_INFO = {"name": "standupguy", "version": "0.1.0"}

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
PARSE_ERROR = -32700


def _as_roots(roots) -> list[str]:
    if roots is None:
        return [os.getcwd()]
    if isinstance(roots, str):
        return [roots]
    return list(roots) or [os.getcwd()]


def _canon(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def resolve_root(requested: str | None, roots: list[str]) -> list[str]:
    """The repo path(s) a tool call may read.

    Without `root`: every configured root. With `root`: that path, but
    ONLY if it is one of the configured roots or inside one (realpath,
    so `..` and links can't escape) — the MCP client must not be able to
    point the server at any repo on the disk.
    """
    if not requested:
        return list(roots)
    want = _canon(requested)
    for r in roots:
        base = _canon(r)
        try:
            inside = os.path.commonpath([base, want]) == base
        except ValueError:  # different drives on Windows
            inside = False
        if want == base or inside:
            return [requested]
    raise ValueError(
        f"root {requested!r} is outside the configured root(s) "
        f"{', '.join(roots)}; start the server with --root for that repo")


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _tool_result(text: str, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _call_tool(params: dict, roots: list[str]) -> dict:
    if params.get("name") != "what_did_i_ship":
        raise LookupError(f"unknown tool: {params.get('name')}")
    args = params.get("arguments") or {}
    since = args.get("since") or "yesterday"
    until = args.get("until") or None
    label = f"{since} → {until}" if until else f"since {since}"
    try:
        for value in (since, until):
            if value:
                gitlog_mod.check_date(value)
        sections = []
        for root in resolve_root(args.get("root"), roots):
            if args.get("all"):
                author = None
            else:
                author = args.get("author") or \
                    gitlog_mod.default_author(root) or None
            sections.append(digest_mod.digest_markdown(
                root, since, label, author, until))
    except Exception as e:
        return _tool_result(json.dumps({"error": str(e)}), is_error=True)
    return _tool_result("\n".join(sections))


def handle(request, roots) -> dict | None:
    """Answer one JSON-RPC message. Returns None when nothing must be sent:
    notifications (no `id`, or a `notifications/*` method) never get a
    reply, not even an error."""
    roots = _as_roots(roots)
    if not isinstance(request, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request")
    method = request.get("method")
    if "id" not in request or (isinstance(method, str)
                               and method.startswith("notifications/")):
        return None
    req_id = request.get("id")
    if not isinstance(method, str):
        return _error(req_id, INVALID_REQUEST, "Invalid Request")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05",
                   "capabilities": {"tools": {}},
                   "serverInfo": SERVER_INFO})
    if method == "ping":
        return ok({})
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
                "until": {"type": "string",
                          "description": "end of the range, inclusive for a "
                                         "bare date (default: now)"},
                "author": {"type": "string",
                           "description": "filter by author name/email "
                                          "substring (default: the repo's "
                                          "git user.email)"},
                "all": {"type": "boolean",
                        "description": "include every author"},
                "root": {"type": "string",
                         "description": "repo path; must be one of the "
                                        "server's --root repos (default: "
                                        "all of them)"}}},
        }]})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            return ok(_call_tool(params, roots))
        except LookupError as e:
            return _error(req_id, INVALID_PARAMS, str(e))
    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def serve(roots=None, stdin=None, stdout=None) -> int:
    """Newline-delimited JSON-RPC over stdio, UTF-8 bytes on the wire."""
    roots = _as_roots(roots)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    reader = getattr(stdin, "buffer", stdin)
    writer = getattr(stdout, "buffer", stdout)
    while True:
        raw = reader.readline()
        if not raw:
            break
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            response = _error(None, PARSE_ERROR, "Parse error")
        else:
            try:
                response = handle(request, roots)
            except Exception as e:  # never let one bad request kill the server
                rid = request.get("id") if isinstance(request, dict) else None
                response = _error(rid, INTERNAL_ERROR, str(e))
        if response is None:
            continue
        data = (json.dumps(response) + "\n").encode("utf-8")
        if isinstance(writer, io.TextIOBase):
            writer.write(data.decode("utf-8"))
        else:
            writer.write(data)
        writer.flush()
    return 0
