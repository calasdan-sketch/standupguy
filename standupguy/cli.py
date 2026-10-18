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
  standupguy --root A --root B   # one section per repo
  standupguy mcp              # MCP server: ask "what did I ship this week"
  standupguy serve            # HTTP bridge for n8n / Activepieces (--root
                              # repeats here too)
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
from . import stdio as stdio_mod

VERSION = "0.1.3"


def _harden_streams() -> None:
    """Never crash on a console that cannot encode a commit subject or the
    digest's own marks (🌱, ✓, —) — Windows cp1252 consoles in particular.
    Python's stderr already replaces unencodable characters; make stdout
    do the same instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        # Both handlers raise UnicodeEncodeError on a character the code
        # page lacks: "strict" (the default) and "surrogateescape" (what a
        # piped stdout reports on Windows 11 / Python 3.11, with cp1252).
        if reconfigure is None or getattr(stream, "errors", None) not in (
                "strict", "surrogateescape"):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _resolve_author(args, root: str) -> str | None:
    if args.all:
        return None
    if args.author is not None:
        return args.author
    email = gitlog_mod.default_author(root)
    if not email:
        print(f"note: no git user.email configured in {root} — showing "
              "every author (set one with `git config user.email you@x`, "
              "or pass --author / --all)", file=sys.stderr)
        return None
    return email


def _window(args) -> tuple[str, str | None, str]:
    """(since, until, label) for the requested mode."""
    # "was it given" is `is not None`: an empty value is not "not given"
    # (_check_values has already refused it)
    given = args.since is not None
    if args.sprint is not None:
        since = args.since if given else f"{args.sprint} days ago"
        label = f"{since} → {args.until}" if args.until is not None \
            else f"since {since}"
        return since, args.until, f"sprint {label}"
    if args.week:
        since = args.since if given else "7 days ago"
        return since, args.until, "week"
    since = args.since if given else "yesterday"
    label = f"{since} → {args.until}" if args.until is not None \
        else f"since {since}"
    return since, args.until, label


def _check_dates(args) -> bool:
    """Refuse impossible ISO dates (exit 2); warn about words git will
    silently ignore. Returns False when the run must stop."""
    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value is None:
            continue
        try:
            warning = gitlog_mod.check_date(value)
        except ValueError as e:
            print(f"error: {flag} {e}", file=sys.stderr)
            return False
        if warning:
            print(f"warning: {flag} {warning}", file=sys.stderr)
    return True


def _repo_digest(args, root: str) -> tuple[str, dict]:
    """(markdown, json payload) for one repo — git runs once per repo."""
    author = _resolve_author(args, root)
    since, until, label = _window(args)
    commits = gitlog_mod.commits_since(root, since, author, until)
    in_progress = gitlog_mod.uncommitted(root)
    if args.sprint is not None:
        md = digest_mod.sprint_markdown(root, since, until, author,
                                        commits=commits,
                                        in_progress=in_progress)
    elif args.week:
        md = digest_mod.weekly_markdown(root, author, since, until,
                                        commits=commits)
    else:
        md = digest_mod.digest_markdown(root, since, label, author, until,
                                        commits=commits,
                                        in_progress=in_progress)
    return md, digest_mod.to_json(root, label, since, until, commits,
                                  in_progress)


def _show(text: str) -> None:
    """Write digest or draft text to the terminal. It is made of commit
    subjects and file names anyone can write: control characters are shown
    as visible escapes instead of being obeyed (JSON is left exact)."""
    stdio_mod.stdout().write(stdio_mod.printable(text))


def cmd_digest(args) -> int:
    results = [_repo_digest(args, root) for root in args.root]
    if args.format == "json":
        payloads = [p for _, p in results]
        data = payloads[0] if len(payloads) == 1 else {"repos": payloads}
        stdio_mod.stdout().write(json.dumps(data, indent=2) + "\n")
        return 0
    md = "\n".join(m for m, _ in results)
    if args.draft:
        try:
            md = draft_mod.draft(
                md, base_url=args.base_url, model=args.model) + "\n"
        except draft_mod.DraftError as e:
            print(f"error: {e}", file=sys.stderr)
            # the digest itself needs no model: show it anyway
            print("note: showing the plain digest instead"
                  + (" (nothing was posted to Slack)"
                     if args.push_slack is not None else ""), file=sys.stderr)
            _show(md)
            return 2
    if args.push_slack is not None:
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
    _show(md)
    return 0


def _port(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a port number, got {value!r}")
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError("expected a port from 0 to 65535")
    return n


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
    p.add_argument("--root", action="append", default=None, metavar="PATH",
                   help="Repo path (default: the current directory). Repeat "
                        "for a multi-repo digest (one section per repo); "
                        "`mcp` and `serve` take it repeated too.")
    p.add_argument("--author", default=None,
                   help="Filter by author: case-insensitive substring of the "
                        "author name or email (default: your git user.email).")
    p.add_argument("--all", action="store_true",
                   help="Include all authors (default: your git user). Every "
                        "author, not every branch: the digest reads the "
                        "branch that is checked out.")
    p.add_argument("--since", default=None,
                   help="git date: 'yesterday' (default), '2026-10-01', "
                        "'3 days ago', 'last monday' (a bare 'monday' means "
                        "today to git) … Values without a time "
                        "of day mean midnight. today, yesterday, N days ago "
                        "and N weeks ago are whole local calendar days; "
                        "other words are passed to git as typed.")
    p.add_argument("--until", default=None,
                   help="End of the range (git date). A bare date is "
                        "inclusive: --until 2026-10-15 means through 23:59:59.")
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
                   default="markdown",
                   help="markdown (default) or json. json is the finished "
                        "output: not with --draft or --push-slack.")
    p.add_argument("--push-slack", nargs="?", const=True, default=None,
                   metavar="WEBHOOK_URL",
                   help="POST the notes to a Slack webhook; a bare "
                        "--push-slack uses AUTOSTANDUP_SLACK_WEBHOOK. An "
                        "empty value is an error.")
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("cmd", nargs="?", choices=["mcp", "serve"],
                   help="Run the MCP server (mcp) or HTTP bridge (serve) "
                        "for n8n / workflow tools.")
    p.add_argument("--port", type=_port, default=8765,
                   help="Port for `serve` (default 8765).")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address for `serve` (default 127.0.0.1: this "
                        "computer only). A network address such as 0.0.0.0 "
                        "needs --token.")
    p.add_argument("--token", default=None, metavar="TOKEN",
                   help="`serve`: require this token (16+ printable "
                        "characters) as `Authorization: Bearer TOKEN` or "
                        "?token=TOKEN on every request except /health. Also "
                        "read from STANDUPGUY_TOKEN, which is safer: a "
                        "command-line argument shows in the process list.")
    p.add_argument("--allow-host", action="append", default=None,
                   metavar="NAME",
                   help="`serve`: also answer requests whose Host header is "
                        "NAME (e.g. host.docker.internal or devbox.lan; "
                        "NAME:PORT pins the port). Works for localhost, "
                        "127.0.0.1 and [::1] too: a forwarded port "
                        "(docker -p 9999:8765, ssh -L) arrives as "
                        "localhost:9999. Repeatable.")
    p.add_argument("--insecure-allow-lan", action="store_true",
                   help="`serve`: listen on a network address WITHOUT a "
                        "token. Anyone who can reach the port can read your "
                        "commit history.")
    return p


def _check_roots(roots: list[str]) -> bool:
    """Every --root must be a git repo. False (after saying which) if not."""
    for root in roots:
        try:
            gitlog_mod._git(root, "rev-parse", "--git-dir")
        except gitlog_mod.GitNotFound:
            raise
        except gitlog_mod.GitError as e:
            said = " ".join(e.stderr.split())
            if not said or "not a git repository" in said:
                print(f"error: not a git repo: {root}", file=sys.stderr)
            else:
                # git's own words: for a repo owned by another user they hold
                # the safe.directory command that fixes it
                print(stdio_mod.printable(
                    f"error: git cannot use {root}: {said[:600]}"),
                    file=sys.stderr)
            return False
    return True


# The options that take text, and what each one needs (for _check_values).
_TEXT_OPTIONS = (
    ("--author", "author", "a name or e-mail address"),
    ("--since", "since", "a git date"),
    ("--until", "until", "a git date"),
    ("--push-slack", "push_slack", "a webhook URL"),
    ("--base-url", "base_url", "a model endpoint URL"),
    ("--model", "model", "a model name"),
    ("--host", "host", "a bind address"),
)


def _check_values(args) -> bool:
    """An option given an empty value (`--push-slack "$HOOK"` with $HOOK
    never set) is a mistake, not a request for the default: say so (False,
    after printing it) instead of doing what nobody asked for. `""` is falsy,
    so it used to read as "not given": nothing was posted, nothing was said,
    exit code 0."""
    given = [("--root", "a repo path", value) for value in args.root or ()]
    given += [(flag, what, getattr(args, name))
              for flag, name, what in _TEXT_OPTIONS]
    for flag, what, value in given:
        if isinstance(value, str) and not value.strip():
            way_out = ("a bare --push-slack posts to "
                       "AUTOSTANDUP_SLACK_WEBHOOK" if flag == "--push-slack"
                       else "leave the option out to use the default")
            print(f"error: {flag} needs {what}, but the value is empty - is "
                  f"the variable it came from set? ({way_out})",
                  file=sys.stderr)
            return False
    return True


def _check_output_flags(args) -> bool:
    """`--format json` prints the digest as JSON and stops; nothing reaches
    --draft or --push-slack. Say so (False, after printing why) instead of
    quietly doing neither."""
    if args.cmd is not None or args.format != "json":
        return True
    named = [flag for flag, given in (("--draft", args.draft),
                                      ("--push-slack",
                                       args.push_slack is not None))
             if given]
    if not named:
        return True
    print(f"error: --format json cannot be combined with "
          f"{' or '.join(named)}: the JSON is printed as it is, so nothing "
          f"would be drafted or posted. Drop one or the other.",
          file=sys.stderr)
    return False


def main(argv=None) -> int:
    try:
        try:
            return _main(argv)
        finally:
            # a closed pipe is found out HERE, where it can be caught, not
            # at interpreter exit (also after argparse's --help/--version)
            if sys.stdout is not None:
                sys.stdout.flush()
    except stdio_mod.StdoutClosed as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        # `standupguy | head -1`: the reader is gone. POSIX says
        # BrokenPipeError, Windows a plain OSError (EINVAL) -- see stdio.
        if not stdio_mod.pipe_closed(e):
            raise
        stdio_mod.hush_stdout()
        return 1


def _main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    if not _check_values(args):
        return 2
    args.root = args.root or [os.getcwd()]
    if not _check_output_flags(args):
        return 2
    try:
        if not _check_roots(args.root):     # mcp, serve and the digest alike
            return 2
        if args.cmd == "mcp":
            return mcp_mod.serve(args.root)
        if args.cmd == "serve":
            token = args.token if args.token is not None else \
                (os.environ.get("STANDUPGUY_TOKEN") or None)
            return serve_mod.serve(
                args.root, args.host, args.port, token=token,
                allow_hosts=args.allow_host or (),
                allow_lan=args.insecure_allow_lan)
        if not _check_dates(args):
            return 2
        return cmd_digest(args)
    except (gitlog_mod.GitNotFound, gitlog_mod.GitTimeout) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
