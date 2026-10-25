"""Digest: turn commits into grouped Markdown. Deterministic, offline.

Groups by conventional-commit prefix (feat/fix/docs/refactor/test/chore),
falls back to an 'other' bucket. Merge commits surface as shipped PRs.
Day headers are the committer date of each commit as a local calendar day
of this machine (see gitlog).
"""

from __future__ import annotations

import re
from collections import OrderedDict

from . import gitlog as gitlog_mod

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


def _commit_line(c: gitlog_mod.Commit) -> str:
    _, clean = classify(c.subject)
    files = f" ({len(c.files)} files)" if c.files else ""
    pr = f" [PR #{c.pr_number}]" if c.pr_number else ""
    return f"- {clean}{pr}{files} `{c.sha[:7]}`"


def ledger_lines(ledger: dict | None) -> list[str]:
    """The "Agent ledger" block, or [] when no commit credits an agent (or
    git could not be asked). Counted from the Co-Authored-By lines in the
    commit messages, so it is a count of what the commits say."""
    if not ledger or not ledger.get("with_agent"):
        return []
    total, n = ledger["commits"], ledger["with_agent"]
    word = "commit" if total == 1 else "commits"
    lines = ["## Agent ledger",
             f"- {n} of {total} {word} ({round(n * 100 / total)}%) credit "
             f"Claude as a co-author"]
    if ledger.get("agents"):
        lines.append("- " + ", ".join(f"{name}: {count}"
                                      for name, count in
                                      ledger["agents"].items()))
    lines.append("_Counted from `Co-Authored-By:` lines in the commit "
                 "messages: it shows what the commits say, not how the code "
                 "was written._")
    lines.append("")
    return lines


def _collect(root: str, since: str, author: str | None, until: str | None,
             commits: list | None, in_progress: list | None
             ) -> tuple[list, list]:
    """The git facts a renderer needs — fetched once, or taken as given
    so callers that also need the JSON don't run git twice."""
    if commits is None:
        commits = gitlog_mod.commits_since(root, since, author, until)
    if in_progress is None:
        in_progress = gitlog_mod.uncommitted_for(root, author)
    return commits, in_progress


def commit_dict(c: gitlog_mod.Commit) -> dict:
    """The one JSON shape for a commit, shared by the CLI and the HTTP
    bridge (`sha, author, email, date, subject, files, pr, wip`)."""
    return {"sha": c.sha, "author": c.author, "email": c.email,
            "date": c.date, "subject": c.subject, "files": c.files,
            "pr": c.pr_number, "wip": is_wip(c.subject)}


def to_json(root: str, label: str, since: str, until: str | None,
            commits: list, in_progress: list,
            markdown: str | None = None) -> dict:
    """The machine-readable digest (same keys from `--format json` and
    from `serve`; `markdown` is included when given). `agent_ledger` counts
    the commits whose message credits Claude as a co-author (null when git
    could not be asked)."""
    data = {
        "repo": gitlog_mod.repo_name(root), "label": label,
        "since": since, "until": until,
        "in_progress": in_progress,
        "commits": [commit_dict(c) for c in commits],
        "agent_ledger": gitlog_mod.agent_ledger(root, commits),
    }
    if markdown is not None:
        data["markdown"] = markdown
    return data


def digest_markdown(root: str, since: str, label: str,
                    author: str | None = None,
                    until: str | None = None, *,
                    commits: list | None = None,
                    in_progress: list | None = None) -> str:
    commits, in_progress = _collect(root, since, author, until,
                                    commits, in_progress)
    repo = gitlog_mod.repo_name(root)
    lines = [f"# Standup — {repo}", f"_{label}_", ""]
    if not commits and not in_progress:
        lines.append("No commits or uncommitted changes in range. "
                     "Touch grass. 🌱")
        return "\n".join(lines) + "\n"
    if commits:
        lines.append("## Shipped")
        for group, items in group_commits(commits).items():
            if not items:
                continue
            lines.append(f"### {GROUP_TITLES[group]}")
            lines.extend(_commit_line(c) for c in items)
            lines.append("")
        lines += ledger_lines(gitlog_mod.agent_ledger(root, commits))
    if in_progress:
        lines.append("## In progress (uncommitted)")
        lines.extend(f"- `{p}`" for p in sorted(in_progress)[:20])
        if len(in_progress) > 20:
            lines.append(f"- …and {len(in_progress) - 20} more")
        lines.append("")
    lines.append("## Blockers")
    lines.append("_none reported — fill in yours_")
    lines.append("")
    return "\n".join(lines)


def _by_day(commits: list[gitlog_mod.Commit]) -> "OrderedDict[str, list]":
    by_day: OrderedDict[str, list] = OrderedDict()
    for c in sorted(commits, key=lambda c: c.date):
        by_day.setdefault(c.date, []).append(c)
    return by_day


def _day_sections(by_day: "OrderedDict[str, list]") -> list[str]:
    lines: list[str] = []
    for day in by_day:
        lines.append(f"## {day}")
        for group, items in group_commits(by_day[day]).items():
            if items:
                lines.append(f"### {GROUP_TITLES[group]}")
                lines.extend(_commit_line(c) for c in items)
        lines.append("")
    return lines


def weekly_markdown(root: str, author: str | None = None,
                    since: str = "7 days ago", until: str | None = None, *,
                    commits: list | None = None) -> str:
    if commits is None:
        commits = gitlog_mod.commits_since(root, since, author, until)
    repo = gitlog_mod.repo_name(root)
    window = f"{since} → {until}" if until else f"since {since}"
    lines = [f"# Weekly summary — {repo}",
             f"_{len(commits)} commits {window} (local dates)_", ""]
    lines += _day_sections(_by_day(commits))
    lines += ledger_lines(gitlog_mod.agent_ledger(root, commits))
    lines.append("## Highlights for sprint review")
    lines.append("_pick 3–5 bullets from above_")
    return "\n".join(lines) + "\n"


def sprint_markdown(root: str, since: str, until: str | None = None,
                    author: str | None = None, *,
                    commits: list | None = None,
                    in_progress: list | None = None) -> str:
    """Sprint-review bullets for the window since..until (git date syntax,
    see gitlog._since_to_arg), grouped by local day, plus a *Carried over*
    section: work that is not shipped yet — WIP/draft commits in the
    window and the uncommitted changes in the working tree."""
    commits, in_progress = _collect(root, since, author, until,
                                    commits, in_progress)
    shipped = [c for c in commits if not is_wip(c.subject)]
    wip = [c for c in commits if is_wip(c.subject)]
    repo = gitlog_mod.repo_name(root)
    window = f"{since} → {until}" if until else f"since {since}"
    lines = [f"# Sprint review — {repo}",
             f"_{window} · {len(shipped)} commits shipped · "
             f"dates in local time_", ""]
    if shipped:
        lines += _day_sections(_by_day(shipped))
    else:
        lines += ["_no commits shipped in this window_", ""]
    lines += ledger_lines(gitlog_mod.agent_ledger(root, shipped))
    lines.append("## Carried over")
    carried = [f"- {_commit_line(c)[2:]} _(WIP commit)_" for c in wip]
    carried += [f"- `{p}` _(uncommitted)_" for p in sorted(in_progress)[:20]]
    if len(in_progress) > 20:
        carried.append(f"- …and {len(in_progress) - 20} more uncommitted")
    lines += carried or ["_nothing carried over_"]
    lines.append("")
    lines.append("## Highlights for sprint review")
    lines.append("_pick 3–5 bullets from above_")
    return "\n".join(lines) + "\n"
