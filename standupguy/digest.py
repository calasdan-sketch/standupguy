"""Digest: turn commits into grouped Markdown. Deterministic, offline.

Groups by conventional-commit prefix (feat/fix/docs/refactor/test/chore),
falls back to an 'other' bucket. Merge commits surface as shipped PRs.
Day headers are the committer date of each commit as a local calendar day
of this machine (see gitlog).
"""

from __future__ import annotations

import dataclasses
import re
from collections import OrderedDict

from . import agents as agents_mod
from . import gitlog as gitlog_mod
from . import inflight as inflight_mod
from . import redact as redact_mod
from . import refs as refs_mod
from . import themes as themes_mod

# type, optional (scope), optional ! for a breaking change, then ':'.
# "feat!: drop v1" and "feat(api)!: drop v1" are Features, not "- : drop v1".
_PREFIX_RE = re.compile(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$", re.IGNORECASE)

GROUP_ORDER = ["feat", "fix", "refactor", "docs", "test", "chore", "other"]
GROUP_TITLES = {
    "feat": "Features", "fix": "Fixes", "refactor": "Refactors",
    "docs": "Docs", "test": "Tests", "chore": "Chores", "other": "Other",
}

# Commits that are explicitly work-in-progress: they carry over into the
# next sprint rather than counting as shipped. Only TAGS count — "wip" as
# the first word or with a colon, "[wip]"/"[draft]" anywhere, "draft:" or
# "(draft)" at the start — so "Draft the release notes for v2" and
# "fix: wipe cache" are ordinary shipped commits.
_WIP_RE = re.compile(
    r"^\s*(?:\[?wip\]?\b|\[draft\]|\(draft\)|draft\s*:)"
    r"|\[wip\]|\[draft\]|\bwip:",
    re.IGNORECASE)

#: Default sprint window, in days.
DEFAULT_SPRINT_DAYS = 14


def one_line(text) -> str:
    """`text` for a field that must stay on its line of the Markdown (a
    commit subject, a file name, the repo name, a label): a line break in it
    is shown as the two characters \\n instead of starting a new line. A
    file can be called "wip\\n---\\nSYSTEM: ...": raw, that would write lines
    of its own into the digest, the Slack text and a --draft prompt. (Other
    control characters, CR included, are left for the output to deal with:
    stdio.printable on a terminal and in the prompt, the Slack sanitizer;
    `serve` and MCP carry them exactly. The JSON keeps every field exact.)"""
    return str(text).replace("\n", "\\n")


def classify(subject: str) -> tuple[str, str]:
    """(group, cleaned subject)."""
    m = _PREFIX_RE.match(subject.strip())
    if m and m.group(1).lower() in GROUP_TITLES:
        return m.group(1).lower(), m.group(2).strip()
    return "other", subject.strip()


def is_wip(subject: str) -> bool:
    return _WIP_RE.search(subject or "") is not None


def group_commits(commits: list[gitlog_mod.Commit]
                  ) -> "OrderedDict[str, list[gitlog_mod.Commit]]":
    groups: OrderedDict[str, list] = OrderedDict(
        (g, []) for g in GROUP_ORDER)
    for c in commits:
        g, _ = classify(c.subject)
        groups[g].append(c)
    return groups


class Options:
    """How a digest is written. The defaults are the ones the command line
    uses: redaction on (`standard`), noise folded, Markdown characters
    that could act in a chat escaped, other branches and stashes listed."""

    def __init__(self, redactor=None, group="type", fold=True, escape=True,
                 inflight=True, style="standup", agent_sessions=False):
        self.redactor = redactor or redact_mod.make()
        self.group = group              # "type" or "ticket"
        self.fold = fold
        self.escape = escape
        self.inflight = inflight
        self.style = style              # "standup" or "ytb"
        self.agent_sessions = agent_sessions


DEFAULT_OPTIONS = Options()


def raw_options() -> "Options":
    """For `serve` and `mcp`, whose callers escape for their own target
    (the bridge sends a sanitized `slack_text` next to the Markdown):
    redacted like the CLI, but the Markdown characters are left as written."""
    return Options(escape=False)

_MENTION_RE = re.compile(r"@(?=\w)")
_MASS_RE = re.compile(r"<!(channel|here|everyone)(?:\|[^<>]*)?>"
                      r"|@(channel|here|everyone)\b", re.I)


def md_escape(text: str) -> str:
    """Commit text for a Markdown line that will be pasted into a chat, a
    wiki or a pull request. Only what can act is changed: the `(` of an
    inline link or image (`[x](url)`), a `<` that opens an autolink, HTML
    tag or `<!here>` / `<@user>` markup, an `@mention` and an unbalanced
    backtick. Plain brackets, asterisks and underscores are left alone."""
    out = _MASS_RE.sub(lambda m: "[at]" + (m.group(1) or m.group(2)).lower(),
                       text)
    out = out.replace("](", "]\\(")
    out = re.sub(r"<(?=[A-Za-z!@#/?])", r"\\<", out)
    out = _MENTION_RE.sub(r"\\@", out)
    if out.count("`") % 2:
        out = out.replace("`", "\\`")
    return out


def _t(opts: Options, text) -> str:
    """Untrusted text as one safe line: line breaks shown as \\n, redacted,
    Markdown-escaped."""
    t = opts.redactor.text(one_line(text))
    return md_escape(t) if opts.escape else t


def _commit_line(c: gitlog_mod.Commit, opts: Options | None = None,
                 item=None) -> str:
    opts = opts or DEFAULT_OPTIONS
    if item is None:
        item = themes_mod.Item(c, themes_mod.classify_smart(
            c.subject, c.files, c.body), themes_mod.clean_subject(c.subject),
            refs_mod.extract(c.subject, c.body))
    return _item_line(item, opts)


def _item_line(item, opts: Options) -> str:
    c = item.commit
    n = len(c.files)
    files = f" ({n} file{'' if n == 1 else 's'})" if n else ""
    tags = refs_mod.tags(item.refs)
    tags = f" {tags}" if tags else ""
    # an old-style merge number the log parser found but the text did not
    if c.pr_number and c.pr_number not in item.refs.prs:
        tags += f" [PR #{c.pr_number}]"
    agent = agents_mod.agent_of(c)
    agent = f" _(with {agent})_" if agent else ""
    times = f" (x{item.count})" if item.count > 1 else ""
    return (f"- {_t(opts, item.text)}{tags}{agent}{times}{files} "
            f"`{c.sha[:7]}`")


def _collect(root: str, since: str, author: str | None, until: str | None,
             commits: list | None, in_progress: list | None
             ) -> tuple[list, list]:
    """The git facts a renderer needs — fetched once, or taken as given
    so callers that also need the JSON don't run git twice."""
    if commits is None:
        commits = gitlog_mod.commits_since(root, since, author, until)
    if in_progress is None:
        in_progress = gitlog_mod.uncommitted(root)
    return commits, in_progress


def commit_dict(c: gitlog_mod.Commit) -> dict:
    """The one JSON shape for a commit, shared by the CLI and the HTTP
    bridge (`sha, author, email, date, subject, files, pr, wip,
    coauthors`)."""
    return {"sha": c.sha, "author": c.author, "email": c.email,
            "date": c.date, "subject": c.subject, "files": c.files,
            "pr": c.pr_number, "wip": is_wip(c.subject),
            "coauthors": list(c.coauthors),
            "group": themes_mod.classify_smart(c.subject, c.files, c.body),
            "refs": refs_dict(refs_mod.extract(c.subject, c.body)),
            "agent": agents_mod.agent_of(c)}


def refs_dict(r) -> dict:
    return {"tickets": list(r.tickets), "issues": list(r.issues),
            "prs": list(r.prs), "closes": list(r.closes)}


def to_json(root: str, label: str, since: str, until: str | None,
            commits: list, in_progress: list,
            markdown: str | None = None, in_flight=None) -> dict:
    """The machine-readable digest (same keys from `--format json` and
    from `serve`; `markdown` is included when given)."""
    data = {
        "repo": gitlog_mod.repo_name(root), "label": label,
        "since": since, "until": until,
        "in_progress": in_progress,
        "commits": [commit_dict(c) for c in commits],
    }
    if in_flight is not None:
        data["in_flight"] = [dataclasses.asdict(i) for i in in_flight]
    if markdown is not None:
        data["markdown"] = markdown
    return data


def _folded(commits, opts: Options):
    return themes_mod.fold(commits, do_fold=opts.fold)


def _group_items(items, opts: Options):
    """[(heading, [Item])] by type (feat, fix ...) or by ticket."""
    if opts.group == "ticket":
        buckets: OrderedDict[str, list] = OrderedDict()
        for it in items:
            key = it.refs.tickets[0] if it.refs.tickets else ""
            buckets.setdefault(key, []).append(it)
        named = [(k, v) for k, v in buckets.items() if k]
        named.sort(key=lambda kv: kv[0])
        if "" in buckets:
            named.append(("No ticket", buckets[""]))
        return named
    groups: OrderedDict[str, list] = OrderedDict(
        (g, []) for g in GROUP_ORDER)
    for it in items:
        groups[it.group].append(it)
    return [(GROUP_TITLES[g], v) for g, v in groups.items() if v]


def _shipped_lines(folded, commits, opts: Options) -> list[str]:
    lines = ["## Shipped"]
    for heading, items in _group_items(folded.items, opts):
        lines.append(f"### {_t(opts, heading)}")
        lines.extend(_item_line(it, opts) for it in items)
        lines.append("")
    note = folded.note()
    if note:
        lines.append(note)
    agent_line = agents_mod.summary(commits)
    if agent_line:
        lines.append(agent_line)
    if note or agent_line:
        lines.append("")
    return lines


def _inflight_lines(found, opts: Options) -> list[str]:
    if not found:
        return []
    lines = ["## Also in flight (not in this branch)"]
    for it in found:
        name = _t(opts, it.name)
        sub = f" - {_t(opts, it.subject)}" if it.subject else ""
        if it.kind == "branch":
            lines.append(f"- branch `{name}`: {it.detail}{sub}")
        elif it.kind == "stash":
            lines.append(f"- stash `{name}` ({it.detail}){sub}")
        elif it.kind == "worktree":
            lines.append(f"- worktree `{name}`: {it.detail}")
        else:
            lines.append(f"- not on any branch: `{name}`{sub} (reflog only)")
    lines.append("")
    return lines


def _session_lines(root, first_day, last_day, opts: Options) -> list[str]:
    if not opts.agent_sessions or first_day is None:
        return []
    found = agents_mod.sessions(root, first_day, last_day)
    if not found:
        return []
    prompts = sum(s["prompts"] for s in found)
    lines = ["## Agent sessions (local transcripts)",
             f"- {len(found)} Claude Code session"
             f"{'' if len(found) == 1 else 's'}, {prompts} request"
             f"{'' if prompts == 1 else 's'} in this repository"]
    for s in found[:5]:
        lines.append(f"- started with: {_t(opts, s['first'])}")
    lines.append("")
    return lines


def _window_days(since, until):
    first = gitlog_mod.calendar_day(since) or _first_iso(since)
    last = gitlog_mod.calendar_day(until) if until else None
    return first, last


def _first_iso(value):
    import datetime
    try:
        return datetime.date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def _in_flight(root, since, until, author, opts: Options, given):
    if given is not None:
        return given
    if not opts.inflight:
        return []
    return inflight_mod.find(root, since, until, author)


def digest_markdown(root: str, since: str, label: str,
                    author: str | None = None,
                    until: str | None = None, *,
                    commits: list | None = None,
                    in_progress: list | None = None,
                    opts: Options | None = None,
                    in_flight: list | None = None) -> str:
    opts = opts or DEFAULT_OPTIONS
    commits, in_progress = _collect(root, since, author, until,
                                    commits, in_progress)
    found = _in_flight(root, since, until, author, opts, in_flight)
    if opts.style == "ytb":
        return ytb_markdown(root, since, label, commits, in_progress,
                            found, opts)
    repo = opts.redactor.repo(gitlog_mod.repo_name(root))
    lines = [f"# Standup — {_t(opts, repo)}", f"_{_t(opts, label)}_", ""]
    first_day, last_day = _window_days(since, until)
    sessions = _session_lines(root, first_day, last_day, opts)
    if not commits and not in_progress and not found and not sessions:
        lines.append("No commits or uncommitted changes in range. "
                     "Touch grass. 🌱")
        return "\n".join(lines) + "\n"
    if commits:
        lines += _shipped_lines(_folded(commits, opts), commits, opts)
    lines += _inflight_lines(found, opts)
    lines += sessions
    if in_progress:
        lines.append("## In progress (uncommitted)")
        lines.extend(f"- `{_t(opts, p)}`" for p in sorted(in_progress)[:20])
        if len(in_progress) > 20:
            lines.append(f"- …and {len(in_progress) - 20} more")
        lines.append("")
    lines.append("## Blockers")
    lines.append("_none reported — fill in yours_")
    lines.append("")
    return "\n".join(lines)


def _by_day(items) -> "OrderedDict[str, list]":
    by_day: OrderedDict[str, list] = OrderedDict()
    for it in sorted(items, key=lambda it: it.commit.date):
        by_day.setdefault(it.commit.date, []).append(it)
    return by_day


def _day_sections(by_day, opts: Options) -> list[str]:
    lines: list[str] = []
    for day in by_day:
        lines.append(f"## {day}")
        for heading, items in _group_items(by_day[day], opts):
            lines.append(f"### {_t(opts, heading)}")
            lines.extend(_item_line(it, opts) for it in items)
        lines.append("")
    return lines


def weekly_markdown(root: str, author: str | None = None,
                    since: str = "7 days ago", until: str | None = None, *,
                    commits: list | None = None,
                    opts: Options | None = None) -> str:
    opts = opts or DEFAULT_OPTIONS
    if commits is None:
        commits = gitlog_mod.commits_since(root, since, author, until)
    folded = _folded(commits, opts)
    repo = opts.redactor.repo(gitlog_mod.repo_name(root))
    window = f"{since} → {until}" if until else f"since {since}"
    lines = [f"# Weekly summary — {_t(opts, repo)}",
             f"_{len(commits)} commits {_t(opts, window)} (local dates)_", ""]
    lines += _day_sections(_by_day(folded.items), opts)
    note = folded.note()
    if note:
        lines += [note, ""]
    lines.append("## Highlights for sprint review")
    lines.append("_pick 3–5 bullets from above_")
    return "\n".join(lines) + "\n"


def sprint_markdown(root: str, since: str, until: str | None = None,
                    author: str | None = None, *,
                    commits: list | None = None,
                    in_progress: list | None = None,
                    opts: Options | None = None,
                    in_flight: list | None = None) -> str:
    """Sprint-review bullets for the window since..until (git date syntax,
    see gitlog._since_to_arg), grouped by local day, plus a *Carried over*
    section: work that is not shipped yet — WIP/draft commits in the
    window, other branches, and the uncommitted changes in the working tree."""
    opts = opts or DEFAULT_OPTIONS
    commits, in_progress = _collect(root, since, author, until,
                                    commits, in_progress)
    found = _in_flight(root, since, until, author, opts, in_flight)
    shipped = [c for c in commits if not is_wip(c.subject)]
    wip = [c for c in commits if is_wip(c.subject)]
    folded = _folded(shipped, opts)
    repo = opts.redactor.repo(gitlog_mod.repo_name(root))
    window = f"{since} → {until}" if until else f"since {since}"
    lines = [f"# Sprint review — {_t(opts, repo)}",
             f"_{_t(opts, window)} · {len(shipped)} commits shipped · "
             f"dates in local time_", ""]
    if shipped:
        lines += _day_sections(_by_day(folded.items), opts)
        if folded.note():
            lines += [folded.note(), ""]
    else:
        lines += ["_no commits shipped in this window_", ""]
    lines.append("## Carried over")
    carried = [f"- {_commit_line(c, opts)[2:]} _(WIP commit)_" for c in wip]
    carried += [f"- branch `{_t(opts, f.name)}`: {f.detail} _(not merged)_"
                for f in found if f.kind == "branch"]
    carried += [f"- `{_t(opts, p)}` _(uncommitted)_"
                for p in sorted(in_progress)[:20]]
    if len(in_progress) > 20:
        carried.append(f"- …and {len(in_progress) - 20} more uncommitted")
    lines += carried or ["_nothing carried over_"]
    lines.append("")
    lines.append("## Highlights for sprint review")
    lines.append("_pick 3–5 bullets from above_")
    return "\n".join(lines) + "\n"


# ---- Yesterday / Today / Blockers, no model --------------------------------

_BLOCKER_RE = re.compile(
    r"\b(?:blocked|blocker|waiting (?:on|for)|stuck|workaround|hack|"
    r"todo|fixme|can'?t|cannot|not working)\b", re.I)


def _yesterday_heading(since: str) -> str:
    day = gitlog_mod.calendar_day(since)
    if day is None:
        return "Since " + since
    if (gitlog_mod._today() - day).days == 1:
        return "Yesterday"
    return _FULL_DAYS[day.weekday()]


_FULL_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")


def _theme_bullets(items, opts: Options) -> list[str]:
    """One bullet per theme: the ticket, else the conventional scope, else
    the kind of change."""
    buckets: OrderedDict[tuple, list] = OrderedDict()
    for it in reversed(items):          # git lists newest first: read oldest first
        if it.refs.tickets:
            key = (0, it.refs.tickets[0])
        else:
            scope = themes_mod.scope_of(it.commit.subject)
            key = (1, scope) if scope else (2, GROUP_TITLES[it.group])
        buckets.setdefault(key, []).append(it)
    out = []
    for (_, label), members in sorted(buckets.items(), key=lambda kv: kv[0][0]):
        texts, seen = [], set()
        for m in members:
            t = _t(opts, m.text)
            if t.lower() not in seen:
                seen.add(t.lower())
                texts.append(t)
        shown = "; ".join(texts[:3])
        more = f" (+{len(texts) - 3} more)" if len(texts) > 3 else ""
        prs = sorted({n for m in members for n in m.refs.prs})
        pr = " " + " ".join(f"[PR #{n}]" for n in prs) if prs else ""
        out.append(f"- **{_t(opts, label)}**: {shown}{more}{pr}")
    return out


def _dir_summary(paths, opts: Options) -> list[str]:
    by_dir: OrderedDict[str, int] = OrderedDict()
    top = []
    for p in sorted(paths):
        if "/" in p:
            head = p.split("/", 1)[0]
            by_dir[head] = by_dir.get(head, 0) + 1
        else:
            top.append(p)
    out = [f"- uncommitted: `{_t(opts, p)}`" for p in top[:5]]
    if len(top) > 5:
        out.append(f"- ...and {len(top) - 5} more uncommitted files")
    for d, n in list(by_dir.items())[:6]:
        out.append(f"- uncommitted in `{_t(opts, d)}/` ({n} file"
                   f"{'' if n == 1 else 's'})")
    if len(by_dir) > 6:
        out.append(f"- ...and {len(by_dir) - 6} more folders")
    return out


def ytb_markdown(root, since, label, commits, in_progress, found,
                 opts: Options) -> str:
    """The digest as Yesterday / Today / Blockers, with no model: shipped
    work grouped by theme (ticket, scope, kind), what is still open under
    Today, and Blockers only where the commit text says something is stuck."""
    folded = _folded(commits, opts)
    repo = opts.redactor.repo(gitlog_mod.repo_name(root))
    lines = [f"# Standup — {_t(opts, repo)}", f"_{_t(opts, label)}_", "",
             f"## {_yesterday_heading(since)}"]
    lines += _theme_bullets(folded.items, opts) or ["- nothing committed"]
    note = folded.note()
    if note:
        lines.append(note)
    agent_line = agents_mod.summary(commits)
    if agent_line:
        lines.append(agent_line)
    lines += ["", "## Today"]
    today = []
    for f in found:
        if f.kind == "branch":
            today.append(f"- continue `{_t(opts, f.name)}` ({f.detail})")
        elif f.kind == "worktree":
            today.append(f"- worktree `{_t(opts, f.name)}`: {f.detail}")
        elif f.kind == "stash":
            today.append(f"- pick up the stash: {_t(opts, f.subject)}")
        else:
            today.append(f"- recover or redo `{_t(opts, f.name)}`: "
                         f"{_t(opts, f.subject)}")
    today += _dir_summary(in_progress, opts)
    wip = [it for it in folded.items if is_wip(it.commit.subject)]
    today += [f"- finish: {_t(opts, it.text)}" for it in wip[:3]]
    lines += today or ["- _fill in yours_"]
    lines += ["", "## Blockers"]
    hints = [it for it in folded.items if _BLOCKER_RE.search(it.commit.subject)]
    if hints:
        lines += [f"- possibly: {_t(opts, it.text)} _(from a commit message;"
                  f" confirm)_" for it in hints[:3]]
    else:
        lines.append("_none reported — fill in yours_")
    lines.append("")
    return "\n".join(lines)
