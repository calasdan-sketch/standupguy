"""Stdio MCP server: let your coding agent ask what you shipped.

Tools:
  what_did_i_ship {since?, until?, author?, all?, root?} -> digest markdown

The wire is newline-delimited JSON-RPC in UTF-8 BYTES, whatever the
console code page (a cp1252 Windows console must not be able to kill the
server with an author called Łukasz).

Errors: a request the client got wrong is a JSON-RPC error (-32700 parse,
-32600 invalid request, -32601 unknown method, -32602 invalid params:
params or arguments that are not objects, a missing or unknown tool name,
an argument of the wrong type); a tool call that was fine but failed (a
date git cannot read, a root outside the server's) is an `isError`
result. -32603 is for bugs only, and the server keeps running after any
of them. Notifications (no `id`) are never answered, not even with an error.
"""

from __future__ import annotations

import io
import json
import os
import sys

from . import digest as digest_mod
from . import gitlog as gitlog_mod
from . import stdio as stdio_mod

SERVER_INFO = {"name": "standupguy", "version": "0.2.0"}

TOOL_NAME = "what_did_i_ship"

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


class InvalidParams(Exception):
    """The params of a request are wrong: JSON-RPC -32602."""


def _check_call(params) -> dict:
    """Validate the `params` of a tools/call; return its arguments.

    Everything rejected here is the client's mistake. What comes after —
    a date git cannot read, a root outside the server's — is a tool call
    that ran and failed, reported as an `isError` result instead."""
    if not isinstance(params, dict):
        raise InvalidParams(
            'params must be an object like {"name": "what_did_i_ship", '
            '"arguments": {...}}')
    name = params.get("name")
    if name is None:
        raise InvalidParams("missing required parameter: name")
    if not isinstance(name, str):
        raise InvalidParams("name must be a string")
    if name != TOOL_NAME:
        raise InvalidParams(f"unknown tool: {name}")
    args = params.get("arguments")
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise InvalidParams("arguments must be an object")
    for key in ("since", "until", "author", "root"):
        if args.get(key) is not None and not isinstance(args[key], str):
            raise InvalidParams(f"argument {key} must be a string")
    if args.get("all") is not None and not isinstance(args["all"], bool):
        raise InvalidParams("argument all must be true or false")
    return args


def _call_tool(args: dict, roots: list[str]) -> dict:
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
    # JSON-RPC: an id is a string, a number or null — echoing anything else
    # back (a list nested a thousand deep) is how a client kills the server
    if not (req_id is None or (isinstance(req_id, (str, int, float))
                               and not isinstance(req_id, bool))):
        return _error(None, INVALID_REQUEST, "Invalid Request")
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
            "name": TOOL_NAME,
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
        try:
            args = _check_call(request.get("params"))
        except InvalidParams as e:
            return _error(req_id, INVALID_PARAMS, str(e))
        return ok(_call_tool(args, roots))
    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def serve(roots=None, stdin=None, stdout=None) -> int:
    """Newline-delimited JSON-RPC over stdio, UTF-8 bytes on the wire.

    Returns 2, with one line on stderr and before a request is read, when
    standard input is closed (standard output closed raises
    stdio.StdoutClosed first)."""
    roots = _as_roots(roots)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else stdio_mod.stdout()
    if stdin is None:
        # started with standard input shut (`standupguy mcp <&-`; pythonw on
        # Windows): sys.stdin is None, and the first read would end in an
        # AttributeError traceback. There is no client to talk to.
        print("error: standard input is closed, so there are no requests to "
              "read", file=sys.stderr)
        return 2
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
        except (ValueError, RecursionError):   # RecursionError: "[[[[..."
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
