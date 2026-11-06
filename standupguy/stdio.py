"""What is on the other end of our output: a reader that left, a terminal.

A reader that goes away is not an error. `standupguy --week | head -1`, an
MCP client that exits, an HTTP client that hangs up: we stop quietly
instead of printing a traceback.

POSIX reports it as BrokenPipeError (EPIPE). Windows does not. Measured on
Windows 11 with Python 3.11: print() to a stdout pipe whose reader has gone
succeeds (it only fills the buffer), the flush then raises a plain
OSError(EINVAL) -- no BrokenPipeError, no winerror -- and, if nobody
catches that, the flush Python does at exit fails again ("Exception
ignored ...", exit status 120).

A terminal, on the other hand, obeys whatever it is shown. Commit subjects,
author names and file names are written by whoever made the commit, so
before they reach a terminal their control characters are made visible
(see printable).
"""

from __future__ import annotations

import errno
import os
import sys

#: True on Windows. A module-level flag, not an `os.name` test at the call
#: site, so that a test on Linux can pretend to be there.
WINDOWS = os.name == "nt"

# WinSock's "connection aborted" / "connection reset". Python raises
# ConnectionAbortedError / ConnectionResetError for both.
_WINSOCK_GONE = (10053, 10054)


def pipe_closed(exc: BaseException | None) -> bool:
    """Is this the error of writing to a pipe nobody reads any more?

    BrokenPipeError (EPIPE) everywhere; on Windows also a plain OSError
    with errno EINVAL. Anywhere else EINVAL means "invalid argument" -- a
    bug that must stay loud -- so it is only believed on Windows."""
    if not isinstance(exc, OSError):
        return False
    if isinstance(exc, BrokenPipeError) or exc.errno == errno.EPIPE:
        return True
    return WINDOWS and exc.errno == errno.EINVAL


def client_gone(exc: BaseException | None) -> bool:
    """Did the other end of a connection hang up on us? A closed pipe, or a
    connection that was reset or aborted (Windows: winerror 10054, 10053)."""
    return (pipe_closed(exc)
            or isinstance(exc, (ConnectionResetError, ConnectionAbortedError))
            or getattr(exc, "winerror", None) in _WINSOCK_GONE)


def hush_stdout() -> None:
    """Point file descriptor 1 at the null device.

    Once the reader of stdout has gone, Python flushes stdout one more time
    while it exits, fails again and complains with nobody left to catch it.
    Call this after catching the error and the exit is quiet."""
    try:
        fd = sys.stdout.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            if null != fd:
                os.close(null)
    except (OSError, ValueError, AttributeError):   # no real descriptor
        pass


class StdoutClosed(Exception):
    """Standard output is closed: the process was started with it shut
    (`command >&-`; pythonw on Windows), so sys.stdout is None. print() then
    silently does nothing and .write() raises AttributeError."""


def stdout():
    """sys.stdout, or StdoutClosed when there is none."""
    if sys.stdout is None:
        raise StdoutClosed("standard output is closed, so there is nowhere "
                           "to write the result")
    return sys.stdout


# C0 (but not tab and newline), DEL and C1: each shown as \xNN
_ESCAPES = {c: f"\\x{c:02x}"
            for c in (*range(0x20), *range(0x7F, 0xA0))
            if c not in (0x09, 0x0A)}

#: Characters that are invisible, or that rearrange the text around them,
#: without being control characters (Unicode category Cc):
#:   - bidirectional controls: ALM, LRM, RLM, LRE..RLO, LRI..PDI - the
#:     "Trojan Source" characters (CVE-2021-42574) that make a line read
#:     differently from what it holds;
#:   - zero-width and invisible ones: ZWSP, WORD JOINER, the invisible math
#:     operators, BOM/ZWNBSP, the Mongolian vowel separator, the Hangul
#:     fillers that pass for an empty name;
#:   - LINE and PARAGRAPH SEPARATOR (they break a line in some viewers),
#:     the interlinear annotation marks;
#:   - the TAG characters U+E0000..U+E007F: invisible copies of ASCII that
#:     a language model still reads ("ASCII smuggling" prompt injection).
#: ZERO WIDTH JOINER and NON-JOINER (U+200D, U+200C) are left alone: emoji
#: sequences and Persian and Indic writing need them, and they cannot move
#: or hide text.
INVISIBLE = frozenset((
    0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A),
    0x200B, *range(0x2060, 0x2065), 0xFEFF, 0x180E,
    0x115F, 0x1160, 0x3164, 0xFFA0,
    0x2028, 0x2029, *range(0xFFF9, 0xFFFC),
    *range(0xE0000, 0xE0080),
))

_ESCAPES.update({c: (f"\\u{c:04x}" if c <= 0xFFFF else f"\\U{c:08x}")
                 for c in INVISIBLE})


def printable(text: str) -> str:
    """`text`, safe to show on a terminal.

    A terminal acts on the control characters it is shown: ESC [ 2 J clears
    the screen, ESC ] 0 ; ... BEL retitles the window, a lone CR lets the
    rest of a line overwrite the start of it, and the C1 characters (U+0080
    to U+009F) do the same as their two-byte ESC forms on terminals that
    honour them. Every C0 and C1 control character except tab and newline,
    plus DEL, is replaced by a visible escape: ESC becomes the four
    characters \\x1b. So is every character in INVISIBLE (bidi controls,
    zero-width and tag characters): U+202E becomes \\u202e, U+E0041
    \\U000e0041. All other text, non-ASCII included, is left alone.

    For output to a terminal only. JSON, the HTTP bridge and MCP carry the
    text exactly (JSON escapes the C0 characters itself, losslessly)."""
    return text.translate(_ESCAPES)
