"""The parts of PHP the panel manages: which extensions are installed, and the
handful of php.ini directives people actually reach for.

Both are stored in one JSON file next to settings.json, and both are applied by
a job. Extensions are ordinary apt packages (`php-curl` and friends). Options
are written to a single drop-in, `conf.d/99-lamplight.ini`, in every installed
SAPI — PHP reads conf.d after php.ini, and the `99-` prefix sorts last, so the
drop-in wins without php.ini ever being edited. Delete the file and PHP is back
to the distro defaults.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import config

PACKAGE_PREFIX = "php-"
DROP_IN_NAME = "99-lamplight.ini"
PHP_ETC = Path("/etc/php")
MAX_EXTENSIONS = 64
MAX_VALUE_LENGTH = 64
MAX_NAME_LENGTH = 32

_EXTENSION_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_SIZE_RE = re.compile(r"^(?:-1|\d{1,9}[KMGkmg]?)$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_-]+){0,2}$")
_TRUE = {"on", "1", "true", "yes"}
_FALSE = {"off", "0", "false", "no"}


DEFAULT_EXTENSIONS: tuple[str, ...] = (
    "mysql",
    "curl",
    "mbstring",
    "xml",
    "zip",
    "gd",
    "intl",
    "bcmath",
)

DEFAULT_PACKAGES: tuple[str, ...] = tuple(PACKAGE_PREFIX + name for name in DEFAULT_EXTENSIONS)


@dataclass(frozen=True)
class Option:
    name: str
    label: str
    kind: str  # size | int | bool | enum | timezone
    php_default: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None


OPTIONS: tuple[Option, ...] = (
    Option(
        name="memory_limit",
        label="Memory limit",
        kind="size",
        php_default="128M",
    ),
    Option(
        name="max_execution_time",
        label="Max execution time",
        kind="int",
        php_default="30",
        minimum=0,
    ),
    Option(
        name="upload_max_filesize",
        label="Max upload size",
        kind="size",
        php_default="2M",
    ),
    Option(
        name="post_max_size",
        label="Max POST size",
        kind="size",
        php_default="8M",
    ),
    Option(
        name="max_input_vars",
        label="Max input variables",
        kind="int",
        php_default="1000",
        minimum=1,
    ),
    Option(
        name="max_file_uploads",
        label="Max files per request",
        kind="int",
        php_default="20",
        minimum=0,
    ),
    Option(
        name="display_errors",
        label="Display errors",
        kind="bool",
        php_default="Off",
        choices=("On", "Off"),
    ),
    Option(
        name="error_reporting",
        label="Error reporting",
        kind="enum",
        php_default="E_ALL & ~E_DEPRECATED",
        choices=(
            "E_ALL",
            "E_ALL & ~E_DEPRECATED",
            "E_ALL & ~E_DEPRECATED & ~E_NOTICE",
            "E_ERROR | E_WARNING | E_PARSE",
        ),
    ),
    Option(
        name="date.timezone",
        label="Default timezone",
        kind="timezone",
        php_default="UTC",
    ),
    Option(
        name="opcache.enable",
        label="OPcache",
        kind="bool",
        php_default="On",
        choices=("On", "Off"),
    ),
    Option(
        name="opcache.memory_consumption",
        label="OPcache memory (MB)",
        kind="int",
        php_default="128",
        minimum=8,
    ),
)

_OPTION_BY_NAME = {option.name: option for option in OPTIONS}
OPTION_NAMES: tuple[str, ...] = tuple(option.name for option in OPTIONS)


def get_option(name: str) -> Option | None:
    return _OPTION_BY_NAME.get(name)


# -- validation -----------------------------------------------------------


def clean_extension(raw: object) -> str:
    """Normalise one extension name, or raise ValueError saying why not.

    `php-curl`, `PHP-Curl` and `curl` all mean the same extension, so they all
    come back as `curl`.
    """
    if not isinstance(raw, str):
        raise ValueError("extension names must be text")
    name = raw.strip().lower()
    if name.startswith(PACKAGE_PREFIX):
        name = name[len(PACKAGE_PREFIX) :]
    if not name:
        raise ValueError("empty extension name")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"extension name is too long: {raw!r}")
    if not _EXTENSION_RE.match(name):
        raise ValueError(f"not a package name: {raw!r}")
    return name


def clean_extensions(values: Iterable[object]) -> tuple[tuple[str, ...], list[str]]:
    """Normalise a whole selection. Returns the good names and one message per bad one."""
    names: list[str] = []
    problems: list[str] = []
    for raw in values:
        try:
            name = clean_extension(raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if name not in names:
            names.append(name)
    if len(names) > MAX_EXTENSIONS:
        problems.append(f"more than {MAX_EXTENSIONS} extensions at once")
        names = names[:MAX_EXTENSIONS]
    return tuple(names), problems


def clean_option(name: str, raw: object) -> str:
    """Normalise one directive value, or raise ValueError.

    Every kind is validated against a pattern or an allowed list, so nothing a
    browser sends can turn into a second line of the .ini file.
    """
    option = get_option(name)
    if option is None:
        raise ValueError(f"{name} is not an option Lamplight manages")
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise ValueError(f"{name}: expected a value, got {type(raw).__name__}")
    value = " ".join(str(raw).split())
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"{name}: value is too long")

    if option.kind == "bool":
        lowered = value.lower()
        if lowered in _TRUE:
            return "On"
        if lowered in _FALSE:
            return "Off"
        raise ValueError(f"{name}: expected On or Off, got {value!r}")
    if option.kind == "int":
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{name}: expected a whole number, got {value!r}") from None
        if option.minimum is not None and number < option.minimum:
            raise ValueError(f"{name}: must be at least {option.minimum}")
        return str(number)
    if option.kind == "size":
        if not _SIZE_RE.match(value):
            raise ValueError(f"{name}: expected a size like 256M, or -1, got {value!r}")
        # 512m and 512M are the same to PHP; write one of them, not both.
        return value[:-1] + value[-1].upper() if value[-1].isalpha() else value
    if option.kind == "enum":
        for choice in option.choices:
            if value.lower() == choice.lower():
                return choice
        raise ValueError(f"{name}: expected one of {', '.join(option.choices)}")
    if option.kind == "timezone":
        if not _TIMEZONE_RE.match(value):
            raise ValueError(f"{name}: expected an IANA timezone like Europe/Berlin")
        return value
    raise ValueError(f"{name}: unsupported option kind {option.kind}")  # pragma: no cover


def clean_options(data: Mapping[str, object]) -> tuple[dict[str, str], list[str]]:
    """Normalise a submitted set of options, in catalog order.

    A blank value means "stop overriding this", so it is dropped rather than
    written as an empty directive.
    """
    cleaned: dict[str, str] = {}
    problems: list[str] = []
    for name in data:
        if get_option(name) is None:
            problems.append(f"{name} is not an option Lamplight manages")
    for option in OPTIONS:
        if option.name not in data:
            continue
        raw = data[option.name]
        if isinstance(raw, str) and not raw.strip():
            continue
        try:
            cleaned[option.name] = clean_option(option.name, raw)
        except ValueError as exc:
            problems.append(str(exc))
    return cleaned, problems


# -- the stored selection -------------------------------------------------


@dataclass
class PhpConfig:
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    options: dict[str, str] = field(default_factory=dict)

    @property
    def packages(self) -> tuple[str, ...]:
        return tuple(PACKAGE_PREFIX + name for name in self.extensions)

    def to_json(self) -> str:
        payload = {"extensions": list(self.extensions), "options": self.options}
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PhpConfig:
        """Build from untrusted JSON. Anything unparseable is dropped, not fatal."""
        raw_extensions = data.get("extensions")
        if isinstance(raw_extensions, list):
            extensions, _ = clean_extensions(raw_extensions)
        else:
            extensions = DEFAULT_EXTENSIONS
        raw_options = data.get("options")
        options, _ = clean_options(raw_options) if isinstance(raw_options, dict) else ({}, [])
        return cls(extensions=extensions, options=options)


def load_config(paths) -> PhpConfig:
    try:
        data = json.loads(paths.php_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        return PhpConfig.from_dict(data)
    php_config = PhpConfig()
    save_config(paths, php_config)
    return php_config


def save_config(paths, php_config: PhpConfig) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    config.atomic_write(paths.php_path, php_config.to_json() + "\n", mode=0o644)


# -- the drop-in ----------------------------------------------------------


@dataclass(frozen=True)
class Sapi:
    """One `/etc/php/<version>/<sapi>` directory that PHP actually reads."""

    version: str
    name: str
    directory: Path

    @property
    def conf_d(self) -> Path:
        return self.directory / "conf.d"

    @property
    def drop_in(self) -> Path:
        return self.conf_d / DROP_IN_NAME


SAPI_ORDER = ("apache2", "fpm", "cli")


def sapis(root: Path | None = None) -> list[Sapi]:
    """Every installed SAPI, newest PHP first and the web ones before the CLI.

    A drop-in is written to all of them, but the order decides which one the page
    quotes when it shows you the value in effect.
    """
    found = [
        Sapi(version=directory.parent.name, name=directory.name, directory=directory)
        for directory in sorted((root or PHP_ETC).glob("*/*"))
        if (directory / "conf.d").is_dir()
    ]
    return sorted(found, key=_sapi_key)


def _sapi_key(sapi: Sapi) -> tuple:
    rank = SAPI_ORDER.index(sapi.name) if sapi.name in SAPI_ORDER else len(SAPI_ORDER)
    return (tuple(-part for part in _version_key(sapi.version)), rank, sapi.name)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def drop_in_text(options: Mapping[str, str]) -> str:
    """The .ini Lamplight owns. Values are already validated by clean_option."""
    lines = [
        "; Written by Lamplight. Edit it from the panel, not by hand — the next",
        "; Apply overwrites this file. Delete it to fall back to php.ini.",
        "",
    ]
    lines += [
        f"{option.name} = {options[option.name]}" for option in OPTIONS if option.name in options
    ]
    return "\n".join(lines) + "\n"


def _strip_comment(value: str) -> str:
    for index, char in enumerate(value):
        if char in ";#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _read_directives(path: Path, wanted: set[str], into: dict[str, tuple[str, str]]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in ";#[" or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if name not in wanted:
            continue
        value = _strip_comment(value).strip().strip('"')
        into[name] = (value, str(path))


def resolve_directives(sapi: Sapi, wanted: set[str]) -> dict[str, tuple[str, str]]:
    """Read `wanted` out of php.ini, then conf.d in the order PHP scans it.

    Returns name -> (value, the file that set it). Later files win, which is
    what PHP itself does.
    """
    resolved: dict[str, tuple[str, str]] = {}
    _read_directives(sapi.directory / "php.ini", wanted, resolved)
    for path in sorted(sapi.conf_d.glob("*.ini")) if sapi.conf_d.is_dir() else []:
        _read_directives(path, wanted, resolved)
    return resolved


def effective_options(sapi: Sapi) -> dict[str, tuple[str, str]]:
    """What this SAPI ends up with for the directives Lamplight manages."""
    return resolve_directives(sapi, set(OPTION_NAMES))


def error_log_paths(root: Path | None = None) -> list[str]:
    """Files PHP has been told to log to, across every installed SAPI.

    `error_log` is not one of the options Lamplight writes, so whatever is here
    was set by the distro or by hand. The two magic values — syslog and stderr —
    name no file and are dropped: the journal covers both.
    """
    found: list[str] = []
    for sapi in sapis(root):
        value = resolve_directives(sapi, {"error_log"}).get("error_log", ("", ""))[0]
        if value and value not in {"syslog", "stderr"} and value not in found:
            found.append(value)
    return found
