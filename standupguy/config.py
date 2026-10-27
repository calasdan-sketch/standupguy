"""Environment settings: STANDUPGUY_* first, the older AUTOSTANDUP_* as fallback."""

from __future__ import annotations

import os

PREFIXES = ("STANDUPGUY_", "AUTOSTANDUP_")


def env(name: str, default: str = "") -> str:
    """`STANDUPGUY_<name>`, else `AUTOSTANDUP_<name>` (the original spelling);
    an empty variable counts as not set."""
    for prefix in PREFIXES:
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default
