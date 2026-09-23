"""Lamplight — localhost LAMP workshop for Debian and Ubuntu."""

from __future__ import annotations

import re
import subprocess
import time
import urllib.error
import urllib.request
from functools import cache
from pathlib import Path

__version__ = "1.6"

# install.sh writes this beside the package before pip installs it. The copy
# in /opt/lamplight has no .git, so the running service cannot ask git itself.
_STAMP = Path(__file__).with_name("REVISION")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")

# The published branch is main. "master" is the same idea; this repo has no master.
_UPSTREAM_URL = "https://api.github.com/repos/michaelstaake/lamplight/commits/main"
_UPSTREAM_TTL_SECONDS = 600
_cached_upstream: tuple[float, str] | None = None


def display_version() -> str:
    """`1.6.abc1234` when this build's commit is known, otherwise `1.6`."""
    short = commit_id()
    return f"{__version__}.{short}" if short else __version__


@cache
def commit_id() -> str:
    """Abbreviated commit of this install, or '' when it cannot be known."""
    stamped = _stamped_commit()
    if stamped:
        return stamped
    return _checkout_commit()


def _stamped_commit() -> str:
    try:
        text = _STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if _COMMIT_RE.fullmatch(text) else ""


def _checkout_commit() -> str:
    """HEAD of a development checkout. An installed copy has no .git here."""
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = completed.stdout.strip()
    if completed.returncode != 0 or not _COMMIT_RE.fullmatch(text):
        return ""
    return text


def same_commit(local: str, upstream: str) -> bool:
    """True when one id is the other, including a 7-character abbreviation."""
    if not local or not upstream:
        return False
    local, upstream = local.lower(), upstream.lower()
    shorter, longer = sorted((local, upstream), key=len)
    return len(shorter) >= 7 and longer.startswith(shorter)


def update_available(local: str, upstream: str) -> bool:
    """True when both commits are known and this install is not upstream."""
    return bool(local and upstream and not same_commit(local, upstream))


def upstream_commit() -> str:
    """Full SHA of GitHub main, or '' when it cannot be fetched. Cached briefly."""
    global _cached_upstream
    now = time.monotonic()
    if _cached_upstream is not None and now - _cached_upstream[0] < _UPSTREAM_TTL_SECONDS:
        return _cached_upstream[1]
    sha = _fetch_upstream_commit()
    _cached_upstream = (now, sha)
    return sha


def clear_upstream_cache() -> None:
    global _cached_upstream
    _cached_upstream = None


def _fetch_upstream_commit() -> str:
    """Ask GitHub for the raw SHA. A miss, a timeout, or junk is ''."""
    request = urllib.request.Request(
        _UPSTREAM_URL,
        headers={
            "Accept": "application/vnd.github.sha",
            "User-Agent": "lamplight",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(65536).decode("utf-8", errors="replace").strip()
    except (OSError, urllib.error.URLError, UnicodeError):
        return ""
    if _COMMIT_RE.fullmatch(body):
        return body.lower()
    match = re.search(r'"sha"\s*:\s*"([0-9a-fA-F]{7,40})"', body)
    return match.group(1).lower() if match else ""
