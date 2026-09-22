"""Where each component actually writes its logs, and how to read the tail.

The job log answers "what did Lamplight do" — apt output, a systemctl restart.
This module is the other half: Apache's access and error logs, MariaDB's
journal, Mailpit's unit. A component page shows these once it is installed,
because "apache2 restarted" is already in Lamplight's job history and saying
it twice is not a second log. PHP and phpMyAdmin share Apache's logs, so they
have no log panel of their own.

Sources are resolved here and addressed by id everywhere else. The browser
names `apache/error`, never a path, so no request can turn into "read me an
arbitrary file as root".
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import catalog, systemops

DEFAULT_LINES = 1000
MAX_LINES = 1000
# A log file is read backwards from its end, never wholesale: access.log on a
# busy box is measured in gigabytes. This is enough tail for MAX_LINES of
# anything short of a stack trace.
TAIL_BYTES = 1024 * 1024
JOURNAL_TIMEOUT = 15

APACHE_LOG_DIR = Path("/var/log/apache2")
MYSQL_LOG_DIR = Path("/var/log/mysql")


@dataclass(frozen=True)
class LogSource:
    """One readable thing: a file on disk, or one unit's journal."""

    id: str
    label: str
    kind: str  # "file" or "journal"
    target: str  # a path, or a unit name
    hint: str = ""


@dataclass(frozen=True)
class LogText:
    text: str = ""
    error: str | None = None


def as_dict(source: LogSource) -> dict:
    return {
        "id": source.id,
        "label": source.label,
        "kind": source.kind,
        "target": source.target,
        "hint": source.hint,
    }


# -- building the list ----------------------------------------------------


def sources_for(component_id: str) -> list[LogSource]:
    """Every log this component has, in the order a person would look at them."""
    component = catalog.get_component(component_id)
    if component is None:
        return []
    builder = _BUILDERS.get(component_id, _default_sources)
    found = builder(component)
    return list({source.id: source for source in found}.values())


def find(component_id: str, source_id: str | None) -> LogSource | None:
    """The named source, or the first one, or None when there is nothing to read."""
    sources = sources_for(component_id)
    if not sources:
        return None
    return next((item for item in sources if item.id == source_id), sources[0])


def has_own_logs(component_id: str) -> bool:
    """True when this component has a log panel of its own.

    PHP and phpMyAdmin write into Apache's logs; showing those files again is a
    second copy of the same stream. Everything with a systemd unit has a journal
    (and often a file) worth tailing.
    """
    component = catalog.get_component(component_id)
    return bool(component and component.service)


def _file(source_id: str, label: str, path: Path | str, hint: str = "") -> LogSource | None:
    path = Path(path)
    if not path.is_file():
        return None
    return LogSource(source_id, label, "file", str(path), hint)


def _journal(unit: str | None, label: str = "systemd journal"):
    if not unit:
        return None
    if not systemops.which("journalctl"):
        return None
    return LogSource("journal", label, "journal", unit, f"journalctl -u {unit}")


def _strays(directory: Path, known: set[str], *, prefix: str = "") -> list[LogSource]:
    """Every other *.log in a log directory — vhost logs, mostly."""
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.log")):
        if str(path) in known:
            continue
        found.append(LogSource(prefix + path.stem.replace("_", "-"), path.name, "file", str(path)))
    return found


def _compact(items) -> list[LogSource]:
    return [item for item in items if item is not None]


def _apache_files() -> list[LogSource]:
    named = _compact(
        [
            _file(
                "error",
                "Error log",
                APACHE_LOG_DIR / "error.log",
                "Apache's own errors, and anything mod_php prints",
            ),
            _file(
                "access",
                "Access log",
                APACHE_LOG_DIR / "access.log",
                "One line per request to the default vhost",
            ),
            _file(
                "other-vhosts",
                "Other vhosts",
                APACHE_LOG_DIR / "other_vhosts_access.log",
                "Requests to every vhost but the default one",
            ),
        ]
    )
    return named + _strays(APACHE_LOG_DIR, {source.target for source in named})


def _default_sources(component: catalog.Component) -> list[LogSource]:
    return _compact([_journal(component.service)])


def _apache_sources(component: catalog.Component) -> list[LogSource]:
    return _apache_files() + _compact([_journal(component.service)])


def _mariadb_sources(component: catalog.Component) -> list[LogSource]:
    named = _compact(
        [
            _file(
                "error",
                "Error log",
                MYSQL_LOG_DIR / "error.log",
                "Empty on Ubuntu unless log_error is set — the journal has it instead",
            )
        ]
    )
    strays = _strays(MYSQL_LOG_DIR, {source.target for source in named})
    return named + strays + _compact([_journal(component.service)])


_BUILDERS = {
    "apache": _apache_sources,
    "mariadb": _mariadb_sources,
}


# -- reading --------------------------------------------------------------


def clamp_lines(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return max(1, min(value, MAX_LINES))


def read(source: LogSource, lines: int = DEFAULT_LINES) -> LogText:
    lines = clamp_lines(lines)
    if source.kind == "journal":
        return _read_journal(source.target, lines)
    return _read_file(Path(source.target), lines)


def _read_file(path: Path, lines: int) -> LogText:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError as exc:
        return LogText(error=f"could not read {path}: {exc.strerror or exc}")
    text = data.decode("utf-8", errors="replace")
    if size > TAIL_BYTES:
        # The seek landed mid-line; that half line is not a log entry.
        text = text.partition("\n")[2]
    kept = text.splitlines()[-lines:]
    if not kept:
        return LogText(f"{path} is empty.")
    return LogText("\n".join(kept))


def _read_journal(unit: str, lines: int) -> LogText:
    if not systemops.which("journalctl"):
        return LogText(error="journalctl is not installed on this host")
    name = unit if unit.endswith(".service") else f"{unit}.service"
    argv = ["journalctl", "-u", name, "-n", str(lines), "--no-pager"]
    try:
        result = systemops.run(argv, timeout=JOURNAL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return LogText(error=f"journalctl did not answer for {name}")
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        return LogText(error=detail or f"journalctl exited {result.returncode} for {name}")
    text = (result.stdout or "").strip()
    return LogText(text or f"The journal has nothing for {name}.")
