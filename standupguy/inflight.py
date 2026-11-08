"""Work that `git log HEAD` does not show: other branches, stashes, other
worktrees and commits only the reflog still remembers.

The daily digest reads the branch that is checked out. A day spent on
three feature branches, a stash and a second worktree would come back as
"nothing". This module looks (read-only) at the rest, for one author, in
the same window.

What it reports
  branch    a local branch with commits in the window that are not in HEAD;
            "pushed" when its upstream has them, "local only" when not
  stash     a stash entry made in the window
  worktree  another worktree of this repository with uncommitted files
  lost      a commit made in the window that no branch or tag reaches any
            more (a reset, a rebase, a deleted branch) - still in the reflog

Every list is capped, every git call is read-only, and names are shown as
git prints them (the caller escapes them for the output format).
"""

from __future__ import annotations

import dataclasses
import os

from . import gitlog as g

MAX_BRANCHES = 40
MAX_REFLOG = 300
MAX_PER_KIND = 12
MAX_LOST_CHECKS = 60        # `--contains` walks history: bound the work


@dataclasses.dataclass
class Item:
    kind: str           # branch | stash | worktree | lost
    name: str
    detail: str = ""    # "3 commits, local only" ...
    subject: str = ""   # latest subject (branch, lost, stash)
    sha: str = ""


def _window(since, until):
    w = [f"--since={g._since_to_arg(since)}"]
    if until:
        w.append(f"--until={g._since_to_arg(until, end=True)}")
    return w


def find(root: str, since: str, until: str | None, author: str | None
         ) -> list:
    items: list = []
    for fn in (_branches, _stashes, _worktrees, _lost):
        try:
            items += fn(root, since, until, author)
        except (g.GitNotFound, g.GitTimeout):
            raise
        except RuntimeError:
            continue            # one failing lookup must not sink the digest
    return items


def _branches(root, since, until, author):
    out = g._git(root, "for-each-ref", "--count=" + str(MAX_BRANCHES * 2),
                 "--format=%(refname:short)%09%(upstream:short)%09"
                 "%(upstream:track)", "refs/heads")
    head = g._git(root, "symbolic-ref", "--short", "-q", "HEAD").strip() \
        if _has_head(root) else ""
    items = []
    window = _window(since, until)
    n = 0
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        upstream, _, track = rest.partition("\t")
        if not name or name == head:
            continue
        n += 1
        if n > MAX_BRANCHES:
            break
        log = g._git(root, "log", "--no-merges", *window,
                     "--format=%H%x1f%aN%x1f%aE%x1f%s", f"HEAD..{name}",
                     "--")
        mine = []
        for rec in log.splitlines():
            parts = rec.split("\x1f", 3)
            if len(parts) != 4:
                continue
            c = g.Commit(parts[0], parts[1], parts[2], "", parts[3], [])
            if not author or g.author_matches(c, author):
                mine.append(c)
        if not mine:
            continue
        if not upstream:
            state = "local only"
        elif "ahead" in track:
            state = "ahead of its remote, not all pushed"
        else:
            state = "pushed"
        k = len(mine)
        items.append(Item("branch", name,
                          f"{k} commit{'' if k == 1 else 's'}, {state}",
                          mine[0].subject, mine[0].sha))
    return items[:MAX_PER_KIND]


def _has_head(root) -> bool:
    try:
        g._git(root, "rev-parse", "--verify", "-q", "HEAD")
        return True
    except g.GitError:
        return False


def _stashes(root, since, until, author):
    out = g._git(root, "stash", "list", "--date=format-local:%Y-%m-%d",
                 "--format=%cd%x1f%gs")
    first = _day(since)
    last = _day(until, end=True)
    items = []
    for i, line in enumerate(out.splitlines()):
        parts = line.split("\x1f", 1)
        if len(parts) != 2:
            continue
        ref, (day, msg) = f"stash@{{{i}}}", parts
        if first and day < first.isoformat():
            continue
        if last and day > last.isoformat():
            continue
        items.append(Item("stash", ref, day, msg))
    return items[:MAX_PER_KIND]


def _day(value, end=False):
    if not value:
        return None
    arg = g._since_to_arg(value, end)
    return _date_of(arg)


def _date_of(arg: str):
    import datetime
    head = arg.split(" ", 1)[0]
    try:
        return datetime.date.fromisoformat(head)
    except ValueError:
        return None         # a word git reads itself: do not guess


def _worktrees(root, since, until, author):
    out = g._git(root, "worktree", "list", "--porcelain")
    here = os.path.normcase(os.path.realpath(
        g.from_git_path(g._git(root, "rev-parse", "--show-toplevel").strip()))
    ) if _is_worktree(root) else None
    items = []
    path = branch = None
    blocks = []
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = g.from_git_path(line[9:])
        elif line.startswith("branch "):
            branch = line[7:].removeprefix("refs/heads/")
        elif not line and path:
            blocks.append((path, branch))
            path = branch = None
    for wt, br in blocks:
        if here and os.path.normcase(os.path.realpath(wt)) == here:
            continue
        try:
            dirty = g.uncommitted(wt)
        except RuntimeError:
            continue
        if dirty:
            k = len(dirty)
            items.append(Item("worktree", os.path.basename(wt),
                              f"{k} uncommitted file{'' if k == 1 else 's'}"
                              + (f" on {br}" if br else "")))
    return items[:MAX_PER_KIND]


def _is_worktree(root) -> bool:
    return g._git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"


def _lost(root, since, until, author):
    if not _has_head(root):
        return []
    window = _window(since, until)
    out = g._git(root, "log", "-g", "--no-abbrev-commit", *window,
                 "-n", str(MAX_REFLOG), "--format=%H%x1f%gs%x1f%aN%x1f%aE%x1f%s",
                 "HEAD")
    items, seen, checked = [], set(), 0
    for line in out.splitlines():
        parts = line.split("\x1f", 4)
        if len(parts) != 5:
            continue
        sha, action, an, ae, subject = parts
        if sha in seen or not action.startswith(("commit", "cherry-pick",
                                                 "merge", "rebase (pick")):
            continue
        seen.add(sha)
        c = g.Commit(sha, an, ae, "", subject, [])
        if author and not g.author_matches(c, author):
            continue
        checked += 1
        if checked > MAX_LOST_CHECKS:
            break
        reach = g._git(root, "for-each-ref", "--contains", sha,
                       "--count=1", "--format=%(refname)").strip()
        if reach:
            continue
        items.append(Item("lost", sha[:7], "in the reflog only", subject, sha))
        if len(items) >= MAX_PER_KIND:
            break
    return items
