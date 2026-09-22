"""Name checks and the SQL the MariaDB page sends. No server required."""

import subprocess

import pytest

from lamplight import mariadb, systemops


def test_database_names_are_plain_identifiers():
    assert mariadb.clean_database("app_1") == "app_1"
    for name in ("mysql", "phpmyadmin"):
        with pytest.raises(ValueError, match="system database"):
            mariadb.clean_database(name)
    with pytest.raises(ValueError, match="letters, digits"):
        mariadb.clean_database("app`; DROP DATABASE mysql; --")
    with pytest.raises(ValueError, match="letters, digits"):
        mariadb.clean_database("a" * 65)


def test_system_accounts_and_remote_hosts_are_refused():
    for name in ("root", "mysql.sys", "debian-sys-maint", "mariadb.sys", "mysql", "phpmyadmin"):
        with pytest.raises(ValueError, match="system account"):
            mariadb.clean_user(name)
    assert mariadb.clean_host(None) == "localhost"
    assert mariadb.clean_host(" 127.0.0.1 ") == "127.0.0.1"
    with pytest.raises(ValueError, match="localhost or 127.0.0.1"):
        mariadb.clean_host("%")


def test_passwords_reject_newlines_and_nulls():
    assert mariadb.clean_password("o'brien\\x") == "o'brien\\x"
    with pytest.raises(ValueError, match="required"):
        mariadb.clean_password("")
    with pytest.raises(ValueError, match="newline or a null"):
        mariadb.clean_password("line\nbreak")
    with pytest.raises(ValueError, match="at most"):
        mariadb.clean_password("x" * (mariadb.MAX_PASSWORD + 1))


def test_create_and_drop_database_sql():
    assert mariadb.create_database_sql("shop") == "CREATE DATABASE `shop` CHARACTER SET utf8mb4;"
    assert mariadb.drop_database_sql("shop") == "DROP DATABASE `shop`;"


def test_user_sql_quotes_the_password_and_sets_the_session_mode():
    created = mariadb.create_user_sql("shop", "localhost", "o'brien\\x", "app")
    assert created.startswith(mariadb._SQL_MODE)
    assert "CREATE USER `shop`@`localhost` IDENTIFIED BY 'o''brien\\x'" in created
    assert "GRANT ALL PRIVILEGES ON `app`.* TO `shop`@`localhost`" in created

    plain = mariadb.create_user_sql("shop", "127.0.0.1", "secret", None)
    assert "GRANT " not in plain
    assert "IDENTIFIED BY 'secret'" in plain

    changed = mariadb.set_password_sql("shop", "localhost", "a;b")
    assert "ALTER USER `shop`@`localhost` IDENTIFIED BY 'a;b'" in changed
    assert mariadb.drop_user_sql("shop", "127.0.0.1") == "DROP USER `shop`@`127.0.0.1`;"


def test_grant_sql_revokes_what_went_away_and_grants_what_is_new():
    changed = mariadb.set_grants_sql("shop", "localhost", ["app"], ["shop", "other"])
    assert changed == (
        "REVOKE ALL PRIVILEGES ON `app`.* FROM `shop`@`localhost`;\n"
        "GRANT ALL PRIVILEGES ON `other`.* TO `shop`@`localhost`;\n"
        "GRANT ALL PRIVILEGES ON `shop`.* TO `shop`@`localhost`;"
    )
    assert mariadb.set_grants_sql("shop", "127.0.0.1", ["app"], ["app"]) is None
    with pytest.raises(ValueError, match="system database"):
        mariadb.set_grants_sql("shop", "localhost", [], ["phpmyadmin"])


def test_overview_hides_system_schemas_and_remote_accounts():
    text = "\n".join(
        [
            "db\tmysql",
            "db\tapp",
            "db\tphpmyadmin",
            "db\tinformation_schema",
            "user\troot\tlocalhost",
            "user\tmysql\tlocalhost",
            "user\tphpmyadmin\tlocalhost",
            "user\tapp\tlocalhost",
            "user\tremote\t%",
            "grant\tapp\tlocalhost\tapp",
            "grant\tapp\tlocalhost\tmysql",
            "grant\tapp\tlocalhost\tphpmyadmin",
            "grant\tphpmyadmin\tlocalhost\tphpmyadmin",
            "grant\troot\tlocalhost\tapp",
        ]
    )
    listed = mariadb.parse_overview(text)
    assert listed["databases"] == ["app"]
    assert listed["users"] == [{"name": "app", "host": "localhost", "databases": ["app"]}]


def test_execute_writes_sql_to_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0, "db\tapp\n", "")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(
        systemops, "which", lambda name: "/usr/bin/mysql" if name == "mysql" else None
    )
    monkeypatch.setattr(systemops, "run", fake_run)

    assert mariadb.execute("SELECT 1") == "db\tapp\n"
    assert seen["argv"][0] == "/usr/bin/mysql"
    assert "--protocol=socket" in seen["argv"]
    assert "SELECT 1" not in seen["argv"]
    assert seen["input"] == "SELECT 1\n"


def test_execute_uses_the_mariadb_client_when_mysql_is_absent(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(
        systemops, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None
    )
    monkeypatch.setattr(systemops, "run", fake_run)
    mariadb.execute("SELECT 1")
    assert seen["argv"][0] == "/usr/bin/mariadb"


def test_a_password_in_the_server_error_is_not_repeated(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "ERROR 1 (HY000): bad s3cret-phrase here")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: "/usr/bin/mysql")
    monkeypatch.setattr(systemops, "run", fake_run)
    with pytest.raises(mariadb.MariaDbError, match="refused the statement") as raised:
        mariadb.execute("ALTER USER", secret="s3cret-phrase")
    assert "s3cret-phrase" not in str(raised.value)


def test_execute_requires_root_and_a_client(monkeypatch):
    def fail_run(argv, **kwargs):
        raise AssertionError("mysql should not be started")

    monkeypatch.setattr(systemops, "run", fail_run)
    monkeypatch.setattr(systemops, "is_root", lambda: False)
    with pytest.raises(PermissionError, match="must run as root"):
        mariadb.execute("SELECT 1")

    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(systemops, "which", lambda name: None)
    with pytest.raises(mariadb.MariaDbError, match="not installed"):
        mariadb.execute("SELECT 1")
