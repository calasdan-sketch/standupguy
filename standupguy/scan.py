"""Find the git repositories under a folder (`--scan`).

Read-only and bounded: it never follows a symbolic link, never goes into a
repository it has found (a submodule or vendored checkout is not a project of
yours), skips the usual dependency and build folders, looks at most
`MAX_DIRS` folders and returns at most `MAX_REPOS` repositories. A tree that
needs more is refused with an error rather than half-listed: a standup that
silently leaves a project out is worse than none.
"""

from __future__ import annotations

import os

#: Folders that hold other people's code or build output, never your work.
SKIP_DIRS = frozenset({     # lower case: compared without regard to case
    "node_modules", ".venv", "venv", "env", "__pycache__", ".cache", ".tox",
    "target", "dist", "build", ".gradle", ".m2", ".cargo", ".npm", ".git",
    "site-packages", "vendor", "pods", ".trash", "$recycle.bin",
    "system volume information",
})
DEFAULT_DEPTH = 2
MAX_DEPTH = 6
MAX_REPOS = 100
MAX_DIRS = 20000


class ScanError(RuntimeError):
    """The scan cannot be completed honestly (not a folder, too big)."""


def _is_repo(path: str) -> bool:
    # `.git` is a folder in a normal clone and a FILE in a linked worktree or
    # a submodule; a bare repository has neither and is not found by a scan.
    return os.path.exists(os.path.join(path, ".git"))


def find_repos(base: str, depth: int = DEFAULT_DEPTH) -> list[str]:
    """Repositories at or below `base`, at most `depth` folders down (0 means
    `base` itself), sorted by path."""
    if not os.path.isdir(base):
        raise ScanError(f"--scan {base}: not a folder")
    if not 0 <= depth <= MAX_DEPTH:
        raise ScanError(f"--scan-depth must be from 0 to {MAX_DEPTH}")
    base = os.path.abspath(base)
    found: list[str] = []
    visited = 0
    stack = [(base, 0)]
    while stack:
        path, level = stack.pop()
        visited += 1
        if visited > MAX_DIRS:
            raise ScanError(
                f"--scan {base}: more than {MAX_DIRS} folders; scan a "
                "smaller folder or lower --scan-depth")
        if _is_repo(path):
            found.append(path)
            if len(found) > MAX_REPOS:
                raise ScanError(
                    f"--scan {base}: more than {MAX_REPOS} repositories; "
                    "scan a smaller folder or lower --scan-depth")
            continue                        # never look inside a repo
        if level >= depth:
            continue
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue                        # unreadable folder: skip it
        for e in entries:
            try:
                if e.name.lower() in SKIP_DIRS or e.is_symlink() \
                        or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stack.append((e.path, level + 1))
    return sorted(found)
