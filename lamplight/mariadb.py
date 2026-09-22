"""Create and drop MariaDB databases and local accounts.

The panel talks to the server the way a root shell does: the mysql client over
the unix socket, with no password of its own. SQL is written to the client's
stdin. A password therefore never appears in the process list, and these
actions are not jobs, so it is not written to the job log either.
"""

from __future__ import annotations

import re
import subprocess

from . import systemops

# Schemas and accounts the server ships with, plus the phpMyAdmin package's
# own database and logins. People manage their own databases here; these stay
# off this page so they cannot be dropped from the panel.
SYSTEM_DATABASES = frozenset(
    {"information_schema", "mysql", "performance_schema", "phpmyadmin", "sys"}
)
SYSTEM_USERS = frozenset(
    {
        "debian-sys-maint",
        "mariadb.sys",
        "mysql",
        "mysql.infoschema",
        "mysql.session",
        "mysql.sys",
        "phpmyadmin",
        "root",
    }
)
# A localhost workshop. `%` and other hosts stay a CLI job.
HOSTS = frozenset({"localhost", "127.0.0.1"})
MAX_NAME = 64
MAX_PASSWORD = 128
NAME_RE = re.compile(rf"^[A-Za-z0-9_]{{1,{MAX_NAME}}}$")

# One client invocation, so this mode is in effect for the statement that
# carries the password. NO_BACKSLASH_ESCAPES makes doubled quotes the only
# way out of a string — a backslash in the password stays a backslash.
_SQL_MODE = "SET SESSION sql_mode = CONCAT_WS(',', @@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES')"

ROOT_REQUIRED = (
    "Lamplight must run as root to install packages and manage services. "
    "Use the systemd service (see the README)."
)

_OVERVIEW_SQL = """
SELECT 'db', SCHEMA_NAME FROM information_schema.SCHEMATA;
SELECT 'user', User, Host FROM mysql.user;
SELECT 'grant', User, Host, Db FROM mysql.db;
""".strip()


class MariaDbError(RuntimeError):
    """The server, or the client in front of it, refused the operation."""


def clean_database(raw: object) -> str:
    """A database name this page is willing to create or drop."""
    name = _clean_name(raw, kind="database")
    if name.lower() in SYSTEM_DATABASES:
        raise ValueError(f"{name} is a system database")
    return name


def clean_user(raw: object) -> str:
    """An account name this page is willing to create, alter, or drop."""
    if not isinstance(raw, str):
        raise ValueError("user name must be text")
    name = raw.strip()
    # `mysql.sys` contains a dot, so the system-account check has to come first.
    if name.lower() in SYSTEM_USERS:
        raise ValueError(f"{name} is a system account")
    if not NAME_RE.fullmatch(name):
        raise ValueError(
            f"user name must be letters, digits, and underscores, at most {MAX_NAME} characters"
        )
    return name


def clean_host(raw: object) -> str:
    """`localhost` when the form leaves the host blank."""
    if raw is None or raw == "":
        return "localhost"
    if not isinstance(raw, str):
        raise ValueError("host must be text")
    host = raw.strip().lower()
    if host not in HOSTS:
        raise ValueError("host must be localhost or 127.0.0.1")
    return host


def clean_password(raw: object) -> str:
    """A password safe to place inside one SQL string."""
    if not isinstance(raw, str) or raw == "":
        raise ValueError("password is required")
    if len(raw) > MAX_PASSWORD:
        raise ValueError(f"password must be at most {MAX_PASSWORD} characters")
    if any(char in raw for char in "\x00\n\r"):
        raise ValueError("password cannot contain a newline or a null")
    return raw


def clean_grant(raw: object) -> str | None:
    """The one database a new account may receive, or none."""
    if raw is None or raw == "":
        return None
    return clean_database(raw)


def clean_databases(raw: object) -> list[str]:
    """The databases an account should be able to use, with duplicates removed."""
    if not isinstance(raw, list):
        raise ValueError("databases must be a list")
    names: list[str] = []
    for item in raw:
        name = clean_database(item)
        if name not in names:
            names.append(name)
    return names


def create_database_sql(name: str) -> str:
    return f"CREATE DATABASE {_quote_ident(clean_database(name))} CHARACTER SET utf8mb4;"


def drop_database_sql(name: str) -> str:
    return f"DROP DATABASE {_quote_ident(clean_database(name))};"


def create_user_sql(name: str, host: str, password: str, database: str | None) -> str:
    account = _account(clean_user(name), clean_host(host))
    secret = clean_password(password)
    grant = clean_grant(database)
    statements = [
        _SQL_MODE,
        f"CREATE USER {account} IDENTIFIED BY {_quote_literal(secret)}",
    ]
    if grant:
        statements.append(f"GRANT ALL PRIVILEGES ON {_quote_ident(grant)}.* TO {account}")
    return _script(statements)


def drop_user_sql(name: str, host: str) -> str:
    return f"DROP USER {_account(clean_user(name), clean_host(host))};"


def set_password_sql(name: str, host: str, password: str) -> str:
    account = _account(clean_user(name), clean_host(host))
    secret = clean_password(password)
    return _script([_SQL_MODE, f"ALTER USER {account} IDENTIFIED BY {_quote_literal(secret)}"])


def set_grants_sql(name: str, host: str, current: list[str], desired: list[str]) -> str | None:
    """Grant and revoke database-level access so `current` becomes `desired`.

    No statement when the two sets already match. Each grant is every privilege
    on that database, which is the same access creating a user can hand out.
    """
    account = _account(clean_user(name), clean_host(host))
    have = {clean_database(database) for database in current}
    want = {clean_database(database) for database in desired}
    statements = [
        f"REVOKE ALL PRIVILEGES ON {_quote_ident(database)}.* FROM {account}"
        for database in sorted(have - want)
    ]
    statements += [
        f"GRANT ALL PRIVILEGES ON {_quote_ident(database)}.* TO {account}"
        for database in sorted(want - have)
    ]
    if not statements:
        return None
    return _script(statements)


def create_database(name: object) -> None:
    execute(create_database_sql(clean_database(name)))


def drop_database(name: object) -> None:
    execute(drop_database_sql(clean_database(name)))


def create_user(name: object, host: object, password: object, database: object) -> None:
    secret = clean_password(password)
    execute(
        create_user_sql(clean_user(name), clean_host(host), secret, clean_grant(database)),
        secret=secret,
    )


def drop_user(name: object, host: object) -> None:
    execute(drop_user_sql(clean_user(name), clean_host(host)))


def set_password(name: object, host: object, password: object) -> None:
    secret = clean_password(password)
    execute(set_password_sql(clean_user(name), clean_host(host), secret), secret=secret)


def set_grants(name: object, host: object, databases: object) -> None:
    """Make `name`@`host` able to use exactly the databases in `databases`."""
    user = clean_user(name)
    host_name = clean_host(host)
    desired = clean_databases(databases)
    listed = overview()
    known = set(listed["databases"])
    missing = [database for database in desired if database not in known]
    if missing:
        raise ValueError(f"{missing[0]} is not a database")
    match = next(
        (
            account
            for account in listed["users"]
            if account["name"] == user and account["host"] == host_name
        ),
        None,
    )
    if match is None:
        raise ValueError(f"no such user {user}@{host_name}")
    sql = set_grants_sql(user, host_name, match["databases"], desired)
    if sql:
        execute(sql)


def overview() -> dict:
    """Databases and local accounts this page is willing to show."""
    return parse_overview(execute(_OVERVIEW_SQL))


def parse_overview(text: str) -> dict:
    """Turn the tagged batch output of `_OVERVIEW_SQL` into lists."""
    databases: list[str] = []
    accounts: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        if not line:
            continue
        fields = [_unescape(part) for part in line.split("\t")]
        kind = fields[0]
        if kind == "db" and len(fields) == 2 and _listable_database(fields[1]):
            databases.append(fields[1])
        elif kind == "user" and len(fields) == 3 and _listable_account(fields[1], fields[2]):
            accounts.setdefault((fields[1], fields[2]), [])
        elif (
            kind == "grant"
            and len(fields) == 4
            and _listable_account(fields[1], fields[2])
            and _listable_database(fields[3])
        ):
            accounts.setdefault((fields[1], fields[2]), []).append(fields[3])
    users = [
        {
            "name": name,
            "host": host,
            "databases": sorted(set(grants)),
        }
        for (name, host), grants in sorted(accounts.items())
    ]
    return {"databases": sorted(set(databases)), "users": users}


def execute(sql: str, *, secret: str | None = None) -> str:
    """Run `sql` as root through the local client. `secret` is scrubbed from errors."""
    if not systemops.is_root():
        raise PermissionError(ROOT_REQUIRED)
    binary = systemops.which("mysql") or systemops.which("mariadb")
    if not binary:
        raise MariaDbError("The mysql client is not installed")
    argv = [
        binary,
        "--protocol=socket",
        "--user=root",
        "--batch",
        "--skip-column-names",
        "--connect-timeout=5",
        "--default-character-set=utf8mb4",
    ]
    try:
        result = systemops.run(argv, input=sql if sql.endswith("\n") else sql + "\n")
    except (OSError, subprocess.TimeoutExpired):
        raise MariaDbError("could not run the mysql client") from None
    if result.returncode != 0:
        raise MariaDbError(_public_error(result.stderr, secret))
    return result.stdout


def _clean_name(raw: object, *, kind: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{kind} name must be text")
    name = raw.strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError(
            f"{kind} name must be letters, digits, and underscores, at most {MAX_NAME} characters"
        )
    return name


def _listable_database(name: str) -> bool:
    return NAME_RE.fullmatch(name) is not None and name.lower() not in SYSTEM_DATABASES


def _listable_account(name: str, host: str) -> bool:
    return (
        NAME_RE.fullmatch(name) is not None and name.lower() not in SYSTEM_USERS and host in HOSTS
    )


def _account(name: str, host: str) -> str:
    return f"{_quote_ident(name)}@{_quote_ident(host)}"


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _quote_literal(value: str) -> str:
    """Quote a string for a session that has NO_BACKSLASH_ESCAPES set."""
    return "'" + value.replace("'", "''") + "'"


def _script(statements: list[str]) -> str:
    return ";\n".join(statements) + ";"


def _unescape(value: str) -> str:
    """Undo the escapes `mysql --batch` applies to a field."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            out.append(char)
            index += 1
            continue
        out.append(
            {
                "0": "\0",
                "b": "\b",
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "Z": "\x1a",
                "\\": "\\",
            }.get(value[index + 1], value[index + 1])
        )
        index += 2
    return "".join(out)


def _public_error(stderr: str, secret: str | None) -> str:
    """The last line MariaDB printed, minus the locator and any password."""
    text = stderr.strip()
    if not text or (secret and secret in text):
        return "MariaDB refused the statement"
    line = text.splitlines()[-1].strip()
    if line.startswith("ERROR ") and ": " in line:
        line = line.split(": ", 1)[1]
    if not line or (secret and secret in line):
        return "MariaDB refused the statement"
    return line
