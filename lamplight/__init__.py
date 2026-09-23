"""Lamplight — localhost LAMP workshop for Debian and Ubuntu."""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path

__version__ = "1.6"

# install.sh writes this beside the package before pip installs it. The copy
# in /opt/lamplight has no .git, so the running service cannot ask git itself.
_STAMP = Path(__file__).with_name("REVISION")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")


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
