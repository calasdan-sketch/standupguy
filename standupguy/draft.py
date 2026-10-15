"""Draft: turn a digest into Yesterday/Today/Blockers prose via a local model.

Local-first: the default endpoint is Ollama on localhost. Point
AUTOSTANDUP_BASE_URL at freellmapi or any OpenAI-compatible endpoint.
Your git history never leaves the machine except to the endpoint YOU
configured. No cloud SaaS, no data-leak ceremony.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error


class DraftError(RuntimeError):
    pass


# Largest digest (characters) sent to the model for --draft. A full-repo
# digest can be hundreds of KB (the Round 2 panel measured 569KB/142k
# tokens on one repo), so the digest is truncated for the MODEL CALL only;
# the digest printed to the terminal is untouched.
MAX_DIGEST_CHARS = 100_000

TRUNCATION_NOTE = (
    "\n\n[Note: this digest was truncated to 100,000 characters for the "
    "model call. The full digest is still available above.]")


DRAFT_TEMPLATE = """You are writing a developer's daily standup notes. Be terse.
Use exactly these three sections:

Yesterday:
- 2-5 bullets of what was accomplished, in plain past tense.

Today:
- 2-4 bullets of what is in progress or planned next, inferred from the
  in-progress files and the trajectory of yesterday's work.

Blockers:
- "None reported." unless the digest clearly shows something stuck.

Rules: no fluff, no emojis, no inventing work that isn't in the digest.
If the digest is thin, say so in one line and stop.

Digest:
---
{digest}
---
"""


def _cfg(base_url=None, api_key=None, model=None):
    return {
        "base_url": (base_url or os.environ.get("AUTOSTANDUP_BASE_URL")
                     or "http://localhost:11434/v1").rstrip("/"),
        "api_key": api_key or os.environ.get("AUTOSTANDUP_API_KEY") or "ollama",
        "model": model or os.environ.get("AUTOSTANDUP_MODEL") or "llama3.1",
    }


def _cap_digest(digest_md: str) -> str:
    """Truncate the digest for the model call; the terminal copy is full.

    Returns the digest unchanged when it fits; otherwise keeps the first
    MAX_DIGEST_CHARS characters and appends a note saying so. The note is
    plain text without {} so DRAFT_TEMPLATE.format() stays safe.
    """
    if len(digest_md) <= MAX_DIGEST_CHARS:
        return digest_md
    return digest_md[:MAX_DIGEST_CHARS] + TRUNCATION_NOTE


def draft(digest_md: str, *, base_url=None, api_key=None, model=None,
          timeout: int = 120) -> str:
    digest_md = _cap_digest(digest_md)
    cfg = _cfg(base_url, api_key, model)
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user",
                      "content": DRAFT_TEMPLATE.format(digest=digest_md)}],
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise DraftError(
            f"cannot reach model at {cfg['base_url']} "
            f"(set AUTOSTANDUP_BASE_URL): {e.reason}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise DraftError(f"unexpected response shape: {e}")
