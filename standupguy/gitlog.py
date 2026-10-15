"""Git ground truth: commits, files, merges, uncommitted work."""

from __future__ import annotations

import dataclasses
import re
import subprocess


@dataclasses.dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str          # YYYY-MM-DD
    subject: str
    files: list[str]
    pr_number: int | None = None


def _git(root: str, *args: str, env: dict | None = None) -> str:
    import os
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                       text=True, timeout=60, env=full_env)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:200]}")
    return p.stdout


_MERGE_RE = re.compile(r"merge pull request #(\d+)", re.IGNORECASE)


def _since_to_arg(since: str) -> str:
    """Normalize `since` to an unambiguous form for git.

    Git's approxidate parses a bare "2026-10-14" (and words like
    "yesterday") as NOON, not midnight — silently dropping morning
    commits. Rewrite the common forms to explicit midnight strings,
    which git handles correctly. Anything else passes through.
    """
    from datetime import datetime, timedelta
    s = since.strip()
    now = datetime.now().astimezone()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{s} 00:00"
    if s.lower() == "yesterday":
        day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return f"{day} 00:00"
    m = re.fullmatch(r"(\d+)\s+days?\s+ago", s.lower())
    if m:
        day = (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        return f"{day} 00:00"
    return since


def commits_since(root: str, since: str, author: str | None = None
                  ) -> list[Commit]:
    """since: 'yesterday', '7 days ago', '2026-10-01', or git date syntax."""
    since = _since_to_arg(since)
    args = ["log", f"--since={since}", "--date=short",
            "--pretty=format:COMMIT:%H|%an|%ae|%ad|%s",
            "--name-only", "HEAD"]
    if author:
        args.append(f"--author={author}")
    try:
        out = _git(root, *args)
    except RuntimeError as e:
        # empty repo (no commits yet) -> no history, not an error
        if "unknown revision" in str(e):
            return []
        raise
    commits: list[Commit] = []
    cur: Commit | None = None
    for line in out.splitlines():
        if line.startswith("COMMIT:"):
            if cur:
                commits.append(cur)
            _, rest = line.split("COMMIT:", 1)
            sha, an, ae, ad, subject = rest.split("|", 4)
            m = _MERGE_RE.search(subject)
            cur = Commit(sha=sha, author=an, email=ae, date=ad,
                         subject=subject, files=[],
                         pr_number=int(m.group(1)) if m else None)
        elif cur and line.strip():
            cur.files.append(line.strip())
    if cur:
        commits.append(cur)
    return commits


def uncommitted(root: str) -> list[str]:
    """Repo-relative paths with uncommitted changes."""
    out = _git(root, "status", "--porcelain")
    paths = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # format: XY <path> [-> <newpath>]
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        paths.append(path)
    return paths


def default_author(root: str) -> str:
    try:
        return _git(root, "config", "user.email").strip()
    except RuntimeError:
        return ""


def repo_name(root: str) -> str:
    import os
    try:
        out = _git(root, "rev-parse", "--show-toplevel").strip()
        return os.path.basename(out)
    except RuntimeError:
        return root
