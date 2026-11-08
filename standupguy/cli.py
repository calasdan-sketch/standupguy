"""StandupGuy: kill the tracker trap without leaking data.

Reads local git history and drafts your standup notes, weekly summary,
and sprint-review bullets as Markdown. Deterministic digest works fully
offline; --draft uses YOUR local model endpoint (Ollama/freellmapi).
Your history never touches a cloud SaaS.

  standupguy                  # since the last workday (Friday, on a Monday)
  standupguy --week           # last 7 days
  standupguy --draft          # LLM prose: Yesterday / Today / Blockers
  standupguy --sprint [DAYS]  # sprint-review bullets, grouped by day,
                              # with a "Carried over" section
  standupguy --root A --root B   # one section per repo
  standupguy mcp              # MCP server: ask "what did I ship this week"
  standupguy serve            # HTTP bridge for n8n / Activepieces (--root
                              # repeats here too)
  standupguy doctor           # check this install: git, Python, encoding
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import __author__, __version__
from . import digest as digest_mod
from . import draft as draft_mod
from . import formats as formats_mod
from . import gitlog as gitlog_mod
from . import inflight as inflight_mod
from . import mcp as mcp_mod
from . import push as push_mod
from . import redact as redact_mod
from . import scan as scan_mod
from . import serve as serve_mod
from . import stdio as stdio_mod

VERSION = __version__


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


# English and fixed: strftime("%a") follows the locale
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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
    if not given:
        since = gitlog_mod.LAST_WORKDAY
        day = gitlog_mod.calendar_day(since)
        return since, args.until, (
            f"since {since} ({_DAY_NAMES[day.weekday()]} {day.isoformat()})"
            + (f" → {args.until}" if args.until is not None else ""))
    since = args.since
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
    if _is_plain_day(args.since) and _is_plain_day(args.until) \
            and args.since.strip() > args.until.strip():
        print(f"error: --since {args.since.strip()} is after --until "
              f"{args.until.strip()}, so no day can match; swap them",
              file=sys.stderr)
        return False
    return True


def _is_plain_day(value) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", value.strip()) is not None


def _author_hint(args, root, since, until, author, commits) -> None:
    """A stranger whose git e-mail differs from the one on their commits
    sees an empty digest and no reason. When the digest is empty ONLY
    because of the author filter, say so (stderr; the output is unchanged)."""
    if commits or not author or args.scan:   # --scan hides quiet repos itself
        return
    try:
        others = gitlog_mod.commits_since(root, since, None, until)
    except (gitlog_mod.GitNotFound, gitlog_mod.GitTimeout):
        raise
    except RuntimeError:
        return
    if others:
        n = len(others)
        where = f" in {gitlog_mod.repo_name(root)}" \
            if len(args.root or ()) > 1 else ""
        print(stdio_mod.printable(
            f"note: nothing by '{author}'{where} in this window, but {n} "
            f"commit{'s' if n != 1 else ''} by other authors - "
            "use --all to see them, or --author to name the right person"),
            file=sys.stderr)


def _shallow_hint(args, root: str, commits) -> None:
    """A shallow clone cuts history at a depth: the oldest commit kept
    lists every file of the project as its change, so its file count (and
    --stat) is wrong, and older work is simply missing. Say so on stderr."""
    if not commits or not gitlog_mod.is_shallow(root):
        return
    where = f" ({gitlog_mod.repo_name(root)})" if len(args.root or ()) > 1 \
        else ""
    print(stdio_mod.printable(
        f"note: this is a shallow clone{where}: older history is missing, "
        "and the oldest commit shown may list every file as changed "
        "(git fetch --unshallow gets the rest)"), file=sys.stderr)


def _options(args) -> digest_mod.Options:
    profile = args.redact or (os.environ.get("STANDUPGUY_REDACT") or "").strip() \
        or redact_mod.DEFAULT_PROFILE
    return digest_mod.Options(
        redactor=redact_mod.make(profile, args.redact_term or ()),
        group=args.group, fold=not args.no_fold,
        inflight=not args.no_inflight, style=args.style,
        agent_sessions=args.agent_sessions, stat=args.stat,
        by_author=args.by_author)


def _repo_digest(args, root: str, opts=None) -> tuple[str, dict]:
    """(markdown, json payload) for one repo — git runs once per repo."""
    opts = opts or _options(args)
    author = _resolve_author(args, root)
    since, until, label = _window(args)
    commits = gitlog_mod.commits_since(root, since, author, until,
                                       stat=args.stat)
    in_progress = gitlog_mod.uncommitted(root)
    _author_hint(args, root, since, until, author, commits)
    _shallow_hint(args, root, commits)
    found = inflight_mod.find(root, since, until, author) \
        if opts.inflight and not args.week else []
    if args.sprint is not None:
        md = digest_mod.sprint_markdown(root, since, until, author,
                                        commits=commits,
                                        in_progress=in_progress, opts=opts,
                                        in_flight=found)
    elif args.week:
        md = digest_mod.weekly_markdown(root, author, since, until,
                                        commits=commits, opts=opts)
    else:
        md = digest_mod.digest_markdown(root, since, label, author, until,
                                        commits=commits,
                                        in_progress=in_progress, opts=opts,
                                        in_flight=found)
    return md, digest_mod.to_json(root, label, since, until, commits,
                                  in_progress, in_flight=found)


def _show(text: str) -> None:
    """Write digest or draft text to the terminal. It is made of commit
    subjects and file names anyone can write: control characters are shown
    as visible escapes instead of being obeyed (JSON is left exact)."""
    stdio_mod.stdout().write(stdio_mod.printable(text))


def _emit(args, text: str) -> None:
    """Show `text`; copy it too when asked."""
    _show(text)
    if args.copy:
        try:
            # the clipboard gets the same visible escapes the terminal does
            tool = formats_mod.copy(stdio_mod.printable(text))
        except formats_mod.ClipboardError as e:
            print(f"warning: not copied: {e}", file=sys.stderr)
        else:
            print(f"copied to the clipboard ({tool})", file=sys.stderr)


def cmd_digest(args) -> int:
    opts = _options(args)
    results = [_repo_digest(args, root, opts) for root in args.root]
    if args.scan:
        # a scan finds every project you have: show the ones with news
        total = len(results)
        results = [(m, p) for m, p in results
                   if p["commits"] or p["in_progress"] or p.get("in_flight")]
        if not results:
            msg = f"No activity in the {total} repo(s) found by --scan."
            if args.format == "json":
                stdio_mod.stdout().write(json.dumps({"repos": []}) + "\n")
            else:
                _emit(args, msg + "\n")
            return 0
    if args.format == "json":
        payloads = [p for _, p in results]
        if args.redact is not None:
            payloads = [digest_mod.redact_payload(p, opts.redactor) for p in payloads]
        data = payloads[0] if len(payloads) == 1 else {"repos": payloads}
        stdio_mod.stdout().write(json.dumps(data, indent=2) + "\n")
        return 0
    md = "\n".join(m for m, _ in results)
    if args.dry_run:
        return _dry_run(args, md)
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
            _emit(args, formats_mod.convert(md, args.format))
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
    _emit(args, formats_mod.convert(md, args.format))
    return 0


def _dry_run(args, md: str) -> int:
    """--dry-run: print what --draft would send and what --push-slack would
    post, then stop. Nothing leaves this computer."""
    out = []
    if args.draft:
        out.append(draft_mod.dry_run(md, base_url=args.base_url,
                                     model=args.model))
    if args.push_slack is not None:
        if args.draft:
            out.append("# --push-slack: the model's reply would be posted to "
                       "Slack, sanitized the same way as below\n")
        out.append("# dry run: this is what would be posted to Slack "
                   "(nothing was sent)\n" + push_mod.sanitize_slack_text(md)
                   + "\n")
    _show("\n".join(out))
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
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION} (by {__author__})")
    p.add_argument("--root", action="append", default=None, metavar="PATH",
                   help="Repo path (default: the current directory). Repeat "
                        "for a multi-repo digest (one section per repo); "
                        "`mcp` and `serve` take it repeated too.")
    p.add_argument("--scan", action="append", default=None, metavar="DIR",
                   help="Also digest every git repo found under DIR (repeat "
                        "for more). Quiet repos are left out. Never follows "
                        "symbolic links; skips node_modules, .venv and the "
                        "like.")
    p.add_argument("--scan-depth", type=int, default=scan_mod.DEFAULT_DEPTH,
                   metavar="N",
                   help=f"How many folders down --scan looks (default "
                        f"{scan_mod.DEFAULT_DEPTH}, at most "
                        f"{scan_mod.MAX_DEPTH}).")
    p.add_argument("--stat", action="store_true",
                   help="Show lines added and removed on each commit line "
                        "(and in --format json).")
    p.add_argument("--holidays", default=None, metavar="DATES",
                   help="Days off, comma-separated ISO dates "
                        "(2026-12-25,2026-12-28): 'last workday' skips "
                        "them. Also read from STANDUPGUY_HOLIDAYS.")
    p.add_argument("--author", default=None,
                   help="Filter by author: case-insensitive substring of the "
                        "author name or email (default: your git user.email).")
    p.add_argument("--all", action="store_true",
                   help="Include all authors (default: your git user). Every "
                        "author, not every branch: the digest reads the "
                        "branch that is checked out.")
    p.add_argument("--since", default=None,
                   help="git date: 'last workday' (default: Friday on a "
                        "Monday, see --workdays), 'yesterday', '2026-10-01', "
                        "'3 days ago', 'last monday' (a bare 'monday' means "
                        "today to git) … Values without a time "
                        "of day mean midnight. today, yesterday, N days ago "
                        "N weeks ago and last workday are whole local "
                        "calendar days; "
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
    p.add_argument("--format", choices=["markdown", "json", "plain", "slack",
                                        "teams"],
                   default="markdown",
                   help="markdown (default), plain, slack (mrkdwn), teams, "
                        "or json. json is the finished output: not with "
                        "--draft or --push-slack. plain/slack/teams are for "
                        "what is shown or copied: not with --push-slack.")
    p.add_argument("--style", choices=["standup", "ytb"], default="standup",
                   help="standup (default): Shipped by kind. ytb: "
                        "Yesterday / Today / Blockers by theme, no model.")
    p.add_argument("--group", choices=["type", "ticket"], default="type",
                   help="Group Shipped by kind of change (default) or by "
                        "ticket key (ABC-123).")
    p.add_argument("--redact", choices=list(redact_mod.PROFILES), default=None,
                   help="What to hide before text is shown, copied, posted or "
                        "sent to a model: standard (default: secrets, URL "
                        "queries and internal links, e-mails, private IPs), "
                        "strict (also every link and repo names), off. Also "
                        "STANDUPGUY_REDACT. With --format json, only when "
                        "given.")
    p.add_argument("--redact-term", action="append", default=None,
                   metavar="WORD",
                   help="Also hide this word (a customer, a code name); "
                        "repeatable.")
    p.add_argument("--by-author", action="store_true",
                   help="With --all (a team's repo): one Shipped section per "
                        "person, busiest first, instead of one mixed list. "
                        "Not with --style ytb, --week or --sprint. "
                        "--redact strict shows person-1, person-2 ... "
                        "instead of names.")
    p.add_argument("--no-fold", action="store_true",
                   help="List every commit: do not fold fixup!/WIP saves, "
                        "branch syncs, revert pairs and duplicates.")
    p.add_argument("--no-inflight", action="store_true",
                   help="Do not look at other branches, stashes and "
                        "worktrees.")
    p.add_argument("--agent-sessions", action="store_true",
                   help="Also count Claude Code sessions run in this repo "
                        "(reads ~/.claude/projects; local only; off by "
                        "default).")
    p.add_argument("--copy", action="store_true",
                   help="Also copy the text to the clipboard (pbcopy, clip, "
                        "wl-copy, xclip or xsel).")
    p.add_argument("--push-slack", nargs="?", const=True, default=None,
                   metavar="WEBHOOK_URL",
                   help="POST the notes to a Slack webhook; a bare "
                        "--push-slack uses AUTOSTANDUP_SLACK_WEBHOOK. An "
                        "empty value is an error.")
    p.add_argument("--workdays", default=None, metavar="DAYS",
                   help="Your working days, for 'last workday' (the default "
                        "--since): mon-fri (default), sun-thu, mon,wed,fri "
                        "... Also read from STANDUPGUY_WORKDAYS.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --draft and/or --push-slack: print the exact "
                        "prompt and endpoint, and the exact Slack text, "
                        "instead of sending anything.")
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("cmd", nargs="?", choices=["mcp", "serve", "doctor"],
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
    ("--workdays", "workdays", "working days such as mon-fri"),
)



def _check_values(args) -> bool:
    """An option given an empty value (`--push-slack "$HOOK"` with $HOOK
    never set) is a mistake, not a request for the default: say so (False,
    after printing it) instead of doing what nobody asked for. `""` is falsy,
    so it used to read as "not given": nothing was posted, nothing was said,
    exit code 0."""
    given = [("--root", "a repo path", value) for value in args.root or ()]
    given += [("--scan", "a folder", value) for value in args.scan or ()]
    given += [("--holidays", "days off such as 2026-12-25", args.holidays)]
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


def _check_format_flags(args) -> bool:
    """plain/slack/teams change what is shown or copied; --push-slack posts
    its own sanitized Markdown, so the two together would silently ignore
    one of them. --copy needs something to copy."""
    if args.cmd is not None:
        return True
    if args.format in ("plain", "slack", "teams") \
            and args.push_slack is not None:
        print(f"error: --format {args.format} changes what is shown or "
              "copied; --push-slack posts its own sanitized text. Drop one.",
              file=sys.stderr)
        return False
    if args.copy and args.format == "json":
        print("error: --copy copies text; --format json is printed as it "
              "is. Redirect it to a file instead.", file=sys.stderr)
        return False
    return True


def _check_redact_env(args) -> bool:
    """STANDUPGUY_REDACT must name a profile: a typo ("strcit") must not
    quietly leave the text less hidden than asked."""
    value = (os.environ.get("STANDUPGUY_REDACT") or "").strip()
    if args.redact is None and value and value not in redact_mod.PROFILES:
        print(stdio_mod.printable(
            f"error: STANDUPGUY_REDACT is {value[:40]!r}; expected one of "
            f"{', '.join(redact_mod.PROFILES)}"), file=sys.stderr)
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


def _check_by_author(args) -> bool:
    """--by-author is for the standup style; the others would quietly
    ignore it, so say so."""
    if not args.by_author or args.cmd is not None:
        return True
    clash = [flag for flag, given in (("--style ytb", args.style == "ytb"),
                                      ("--week", args.week),
                                      ("--sprint", args.sprint is not None))
             if given]
    if not clash:
        return True
    print(f"error: --by-author cannot be combined with {' or '.join(clash)}: "
          "it splits the standup style's Shipped list by person",
          file=sys.stderr)
    return False


def _check_workdays(args) -> bool:
    """--workdays (else STANDUPGUY_WORKDAYS) must name working days. A bad
    value is an error (False, after saying why), not a quiet Monday-to-
    Friday: the digest would start on the wrong day without a word."""
    spec, source = args.workdays, "--workdays"
    if spec is None:
        # an empty or blank variable counts as not set, like every other
        # variable standupguy reads (an empty --workdays is refused earlier,
        # like every other empty option)
        spec, source = (os.environ.get("STANDUPGUY_WORKDAYS") or "").strip() \
            or None, "STANDUPGUY_WORKDAYS"
    if spec is None:
        gitlog_mod.set_workdays(gitlog_mod.DEFAULT_WORKDAYS)
        return True
    try:
        gitlog_mod.set_workdays(gitlog_mod.parse_workdays(spec))
    except ValueError as e:
        print(stdio_mod.printable(
            f"error: {source} {e} (expected e.g. mon-fri, sun-thu or "
            f"mon,wed,fri)"), file=sys.stderr)
        return False
    return True


def _check_holidays(args) -> bool:
    """--holidays (else STANDUPGUY_HOLIDAYS) must be real dates; a typo must
    not quietly leave a holiday counted as a workday."""
    spec, source = args.holidays, "--holidays"
    if spec is None:
        spec, source = (os.environ.get("STANDUPGUY_HOLIDAYS") or "").strip() \
            or None, "STANDUPGUY_HOLIDAYS"
    try:
        gitlog_mod.set_holidays(
            gitlog_mod.parse_holidays(spec) if spec else ())
    except ValueError as e:
        print(stdio_mod.printable(
            f"error: {source} {e} (expected e.g. 2026-12-25,2026-12-28)"),
            file=sys.stderr)
        return False
    return True


def _same_path(path: str) -> str:
    """A folder in a form two spellings of it share: symbolic links
    resolved, and (on Windows only) upper and lower case folded, so
    `C:\\Code\\App` and `c:\\code\\app` are one repository."""
    return os.path.normcase(os.path.realpath(path))


def _expand_scan(args) -> bool:
    """Add the repos --scan finds to args.root. False (after saying why) when
    a scan cannot be done honestly."""
    if not args.scan:
        return True
    found: list[str] = []
    for base in args.scan:
        try:
            found += scan_mod.find_repos(base, args.scan_depth)
        except scan_mod.ScanError as e:
            print(stdio_mod.printable(f"error: {e}"), file=sys.stderr)
            return False
    roots = list(args.root or [])
    seen = {_same_path(r) for r in roots}
    for repo in found:
        # a repo git will not open (owned by another user, say) is skipped
        # with a word, not fatal: the rest of the scan still counts
        try:
            gitlog_mod._git(repo, "rev-parse", "--git-dir")
        except gitlog_mod.GitNotFound:
            raise
        except gitlog_mod.GitError:
            print(stdio_mod.printable(
                f"warning: skipped {repo}: git cannot open it"),
                file=sys.stderr)
            continue
        if _same_path(repo) not in seen:
            seen.add(_same_path(repo))
            roots.append(repo)
    if not roots:
        print("note: --scan found no git repositories", file=sys.stderr)
        args.root = []
    else:
        args.root = roots
    return True


def _check_dry_run(args) -> bool:
    """--dry-run shows what --draft or --push-slack would send. Alone (or
    with `mcp` / `serve`) it would change nothing: say so (False)."""
    if not args.dry_run or (args.cmd is None and (
            args.draft or args.push_slack is not None)):
        return True
    print("error: --dry-run only changes --draft and --push-slack (it shows "
          "what they would send); the digest itself sends nothing anyway",
          file=sys.stderr)
    return False


def cmd_doctor() -> int:
    """`standupguy doctor`: what this install can and cannot do, in plain
    words, for a first run or a bug report. Reads nothing but the version of
    git and the current folder's git e-mail; sends nothing anywhere."""
    import platform
    out = stdio_mod.stdout()
    rows = [("standupguy", f"{__version__} ({__author__})"),
            ("python", f"{platform.python_version()} on {platform.system()}"),
            ("output encoding", getattr(out, "encoding", None) or "unknown")]
    problems = 0
    try:
        said = gitlog_mod._git(os.getcwd(), "--version").strip()
    except gitlog_mod.GitNotFound as e:
        rows.append(("git", f"NOT FOUND - {e}"))
        problems += 1
        said = ""
    except (gitlog_mod.GitError, gitlog_mod.GitTimeout) as e:
        rows.append(("git", f"did not answer ({e})"))
        problems += 1
        said = ""
    if said:
        rows.append(("git", said))
        found = re.search(r"(\d+)\.(\d+)", said)
        have = (int(found.group(1)), int(found.group(2))) if found else None
        if have is None:
            rows.append(("git features", "version not recognised"))
        elif have < (2, 26):
            rows.append(("git features", "too old for the 'In progress' list "
                         "(needs 2.26); update git"))
            problems += 1
        else:
            rows.append(("git features", "all available"))
        try:
            email = gitlog_mod.default_author(os.getcwd())
        except (gitlog_mod.GitNotFound, gitlog_mod.GitTimeout):
            email = ""
        rows.append(("git e-mail here", email or
                     "not set (the digest will show every author; "
                     "`git config user.email you@x`)"))
    for name, value in rows:
        out.write(stdio_mod.printable(f"{name:17}{value}\n"))
    out.write("ok\n" if not problems else "problems found (see above)\n")
    return 0 if not problems else 2


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
    if args.cmd == "doctor":
        try:
            return cmd_doctor()
        except gitlog_mod.GitNotFound as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if not _check_values(args) or not _check_redact_env(args):
        return 2
    if args.scan and args.cmd is None and not (0 <= args.scan_depth
                                                <= scan_mod.MAX_DEPTH):
        print(f"error: --scan-depth must be from 0 to {scan_mod.MAX_DEPTH}",
              file=sys.stderr)
        return 2
    if not args.scan:
        args.root = args.root or [os.getcwd()]
    if not _check_output_flags(args) or not _check_format_flags(args) \
            or not _check_by_author(args) \
            or not _check_dry_run(args) or not _check_workdays(args) \
            or not _check_holidays(args):
        return 2
    try:
        if not _expand_scan(args):
            return 2
        if args.scan and not args.root:
            return 0
        if not _check_roots(args.root):     # mcp, serve and the digest alike
            return 2
        if args.cmd == "mcp":
            # the digest goes to the model behind your coding agent: the
            # server's owner (not the agent) picks how much is hidden
            profile = args.redact or (os.environ.get("STANDUPGUY_REDACT")
                                      or "").strip() \
                or redact_mod.DEFAULT_PROFILE
            return mcp_mod.serve(args.root, opts=digest_mod.Options(
                redactor=redact_mod.make(profile, args.redact_term or ()),
                escape=False))
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
