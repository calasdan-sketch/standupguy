"""StandupGuy: kill the tracker trap without leaking data.

Reads local git history and drafts your standup notes, weekly summary,
and sprint-review bullets as Markdown. Deterministic digest works fully
offline; --draft uses YOUR local model endpoint (Ollama/freellmapi).
Your history never touches a cloud SaaS.

  standupguy                  # yesterday's digest
  standupguy --week           # last 7 days
  standupguy --draft          # LLM prose: Yesterday / Today / Blockers
  standupguy --sprint [DAYS]  # sprint-review bullets, grouped by day,
                              # with a "Carried over" section
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


def _harden_streams() -> None:
    """Never crash on a console that cannot encode a commit subject or the
    digest's own marks (🌱, ✓, —) — Windows cp1252 consoles in particular.
    Python's stderr already replaces unencodable characters; make stdout
    do the same instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or getattr(stream, "errors", None) != "strict":
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _resolve_author(args, root: str) -> str | None:
    if args.all:
        return None
    if args.author:
        return args.author
    return gitlog_mod.default_author(root) or None


def _window(args) -> tuple[str, str | None, str]:
    """(since, until, label) for the requested mode."""
    if args.sprint is not None:
        since = args.since or f"{args.sprint} days ago"
        label = f"{since} → {args.until}" if args.until else f"since {since}"
        return since, args.until, f"sprint {label}"
    if args.week:
        since = args.since or "7 days ago"
        return since, args.until, "week"
    since = args.since or "yesterday"
    label = f"{since} → {args.until}" if args.until else f"since {since}"
    return since, args.until, label


def cmd_digest(args) -> int:
    root = args.root
    author = _resolve_author(args, root)
    since, until, label = _window(args)
    if args.sprint is not None:
        md = digest_mod.sprint_markdown(root, since, until, author)
    elif args.week:
        md = digest_mod.weekly_markdown(root, author, since)
    else:
        md = digest_mod.digest_markdown(root, since, label, author, until)
    if args.draft:
        try:
            md = draft_mod.draft(
                md, base_url=args.base_url, model=args.model) + "\n"
        except draft_mod.DraftError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if args.format == "json":
        commits = gitlog_mod.commits_since(root, since, author, until)
        print(json.dumps({
            "repo": gitlog_mod.repo_name(root), "label": label,
            "since": since, "until": until,
            "in_progress": gitlog_mod.uncommitted(root),
            "commits": [{"sha": c.sha, "author": c.author, "email": c.email,
                         "date": c.date, "subject": c.subject,
                         "files": c.files, "pr": c.pr_number,
                         "wip": digest_mod.is_wip(c.subject)}
                        for c in commits],
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


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of days, got {value!r}")
    if n <= 0:
        raise argparse.ArgumentTypeError("--sprint DAYS must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="standupguy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--root", default=os.getcwd(), help="Repo path.")
    p.add_argument("--author", default=None,
                   help="Filter by author: case-insensitive substring of the "
                        "author name or email (default: your git user.email).")
    p.add_argument("--all", action="store_true",
                   help="Include all authors (default: your git user).")
    p.add_argument("--since", default=None,
                   help="git date: 'yesterday' (default), '2026-10-01', "
                        "'3 days ago', 'last monday' … Values without a time "
                        "of day mean midnight; the words are passed to git.")
    p.add_argument("--until", default=None,
                   help="End of the range (git date, same rules as --since).")
    p.add_argument("--week", action="store_true", help="Last 7 days.")
    p.add_argument("--sprint", nargs="?", const=digest_mod.DEFAULT_SPRINT_DAYS,
                   type=_positive_int, default=None, metavar="DAYS",
                   help="Sprint-review bullets for the last DAYS days "
                        f"(default {digest_mod.DEFAULT_SPRINT_DAYS}), or for "
                        "--since/--until, grouped by local day with a "
                        "'Carried over' section (WIP commits + uncommitted work).")
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
    _harden_streams()
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
