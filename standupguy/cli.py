"""StandupGuy: kill the tracker trap without leaking data.

Reads local git history and drafts your standup notes, weekly summary,
and sprint-review bullets as Markdown. Deterministic digest works fully
offline; --draft uses YOUR local model endpoint (Ollama/freellmapi).
Your history never touches a cloud SaaS.

  standupguy                  # yesterday's digest
  standupguy --week           # last 7 days
  standupguy --draft          # LLM prose: Yesterday / Today / Blockers
  standupguy --sprint         # sprint-review bullets
  standupguy mcp              # MCP server: ask "what did I ship this week"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import digest as digest_mod
from . import draft as draft_mod
from . import gitlog as gitlog_mod
from . import mcp as mcp_mod
from . import push as push_mod
from . import serve as serve_mod

VERSION = "0.1.0"


def _resolve_author(args, root: str) -> str | None:
    if args.all:
        return None
    if args.author:
        return args.author
    return gitlog_mod.default_author(root) or None


def cmd_digest(args) -> int:
    root = args.root
    author = _resolve_author(args, root)
    if args.week or args.sprint:
        md = digest_mod.weekly_markdown(root, author)
        label = "week"
    else:
        since = args.since or "yesterday"
        label = f"since {since}"
        md = digest_mod.digest_markdown(root, since, label, author)
    if args.draft:
        try:
            md = draft_mod.draft(
                md, base_url=args.base_url, model=args.model) + "\n"
        except draft_mod.DraftError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if args.format == "json":
        commits = gitlog_mod.commits_since(
            root, "7 days ago" if (args.week or args.sprint) else
            (args.since or "yesterday"), author)
        print(json.dumps({
            "repo": gitlog_mod.repo_name(root), "label": label,
            "in_progress": gitlog_mod.uncommitted(root),
            "commits": [{"sha": c.sha, "author": c.author, "date": c.date,
                         "subject": c.subject, "files": c.files,
                         "pr": c.pr_number} for c in commits],
        }, indent=2))
        return 0
    if args.push_slack:
        webhook = args.push_slack if args.push_slack is not True else \
            os.environ.get("AUTOSTANDUP_SLACK_WEBHOOK", "")
        if not webhook:
            print("error: no webhook — pass --push-slack <url> or set "
                  "AUTOSTANDUP_SLACK_WEBHOOK", file=sys.stderr)
            return 2
        try:
            push_mod.push_slack(webhook, md)
            print("pushed to Slack ✓")
        except push_mod.PushError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    sys.stdout.write(md)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="standupguy", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--root", default=os.getcwd(), help="Repo path.")
    p.add_argument("--author", default=None, help="Filter by author.")
    p.add_argument("--all", action="store_true",
                   help="Include all authors (default: your git user).")
    p.add_argument("--since", default=None,
                   help="git date: 'yesterday' (default), '2026-10-01', …")
    p.add_argument("--week", action="store_true", help="Last 7 days.")
    p.add_argument("--sprint", action="store_true",
                   help="Sprint-review bullets (weekly + highlights).")
    p.add_argument("--draft", action="store_true",
                   help="Draft Yesterday/Today/Blockers prose via local model.")
    p.add_argument("--format", choices=["markdown", "json"],
                   default="markdown")
    p.add_argument("--push-slack", nargs="?", const=True, default=None,
                   metavar="WEBHOOK_URL",
                   help="POST the notes to a Slack webhook "
                        "(or AUTOSTANDUP_SLACK_WEBHOOK).")
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("cmd", nargs="?", choices=["mcp", "serve"],
                   help="Run the MCP server (mcp) or HTTP bridge (serve) "
                        "for n8n / workflow tools.")
    p.add_argument("--port", type=int, default=8765,
                   help="Port for `serve` (default 8765).")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind host for `serve` (default 127.0.0.1).")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        return mcp_mod.serve(args.root)
    if args.cmd == "serve":
        return serve_mod.serve(args.root, args.host, args.port)
    try:
        gitlog_mod._git(args.root, "rev-parse", "--git-dir")
    except RuntimeError:
        print("error: not a git repo", file=sys.stderr)
        return 2
    return cmd_digest(args)


if __name__ == "__main__":
    sys.exit(main())
