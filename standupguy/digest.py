"""Digest: turn commits into grouped Markdown. Deterministic, offline.

Groups by conventional-commit prefix (feat/fix/docs/refactor/test/chore),
falls back to an 'other' bucket. Merge commits surface as shipped PRs.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from . import gitlog as gitlog_mod

_PREFIX_RE = re.compile(r"^(\w+)(?:\(.+\))?[:!]\s*(.+)$", re.IGNORECASE)

GROUP_ORDER = ["feat", "fix", "refactor", "docs", "test", "chore", "other"]
GROUP_TITLES = {
    "feat": "Features", "fix": "Fixes", "refactor": "Refactors",
    "docs": "Docs", "test": "Tests", "chore": "Chores", "other": "Other",
}


def classify(subject: str) -> tuple[str, str]:
    """(group, cleaned subject)."""
    m = _PREFIX_RE.match(subject.strip())
    if m and m.group(1).lower() in GROUP_TITLES:
        return m.group(1).lower(), m.group(2).strip()
    return "other", subject.strip()


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


def digest_markdown(root: str, since: str, label: str,
                    author: str | None = None) -> str:
    commits = gitlog_mod.commits_since(root, since, author)
    in_progress = gitlog_mod.uncommitted(root)
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


def weekly_markdown(root: str, author: str | None = None) -> str:
    commits = gitlog_mod.commits_since(root, "7 days ago", author)
    repo = gitlog_mod.repo_name(root)
    by_day: OrderedDict[str, list] = OrderedDict()
    for c in commits:
        by_day.setdefault(c.date, []).append(c)
    lines = [f"# Weekly summary — {repo}",
             f"_{len(commits)} commits in the last 7 days_", ""]
    for day in sorted(by_day):
        lines.append(f"## {day}")
        for group, items in group_commits(by_day[day]).items():
            if items:
                lines.append(f"### {GROUP_TITLES[group]}")
                lines.extend(_commit_line(c) for c in items)
        lines.append("")
    lines.append("## Highlights for sprint review")
    lines.append("_pick 3–5 bullets from above_")
    return "\n".join(lines) + "\n"
