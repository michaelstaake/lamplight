import re
from pathlib import Path

import pytest

from lamplight import logsources


@pytest.fixture
def logdirs(tmp_path, monkeypatch):
    """Stand-in log directories with the files a real host would have."""
    apache = tmp_path / "apache2"
    mysql = tmp_path / "mysql"
    apache.mkdir()
    mysql.mkdir()
    for name in ("error.log", "access.log", "other_vhosts_access.log"):
        (apache / name).write_text("hello\n", encoding="utf-8")
    (mysql / "error.log").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(logsources, "APACHE_LOG_DIR", apache)
    monkeypatch.setattr(logsources, "MYSQL_LOG_DIR", mysql)
    monkeypatch.setattr(logsources.systemops, "which", lambda name: "/usr/bin/" + name)
    return apache, mysql


def test_apache_files_come_before_the_journal(logdirs):
    ids = [source.id for source in logsources.sources_for("apache")]
    assert ids[:2] == ["error", "access"]
    assert ids[-1] == "journal"


def test_php_and_phpmyadmin_have_no_log_sources(logdirs):
    assert logsources.sources_for("php") == []
    assert logsources.sources_for("phpmyadmin") == []
    assert not logsources.has_own_logs("php")
    assert not logsources.has_own_logs("phpmyadmin")
    assert logsources.has_own_logs("apache")
    assert logsources.has_own_logs("mailpit")


def test_every_component_with_a_service_offers_its_journal(logdirs):
    for component_id in ("apache", "mariadb", "mailpit"):
        sources = logsources.sources_for(component_id)
        assert any(source.kind == "journal" for source in sources), component_id


def test_source_ids_are_unique_per_component(logdirs):
    for component_id in ("apache", "mariadb", "mailpit"):
        ids = [source.id for source in logsources.sources_for(component_id)]
        assert len(ids) == len(set(ids)), component_id


def test_unknown_component_has_no_sources():
    assert logsources.sources_for("mysql") == []
    assert logsources.find("mysql", None) is None


def test_find_falls_back_to_the_first_source(logdirs):
    assert logsources.find("apache", "no-such-log").id == "error"
    assert logsources.find("apache", "access").id == "access"


def test_a_log_file_that_is_not_there_is_not_offered(logdirs):
    """The panel lists what exists; a missing access.log must not become a row
    that only ever reports an error."""
    apache, _ = logdirs
    (apache / "access.log").unlink()
    assert "access" not in [source.id for source in logsources.sources_for("apache")]


def test_clamp_lines_survives_junk_from_the_query_string():
    assert logsources.clamp_lines(None) == logsources.DEFAULT_LINES
    assert logsources.clamp_lines("nope") == logsources.DEFAULT_LINES
    assert logsources.clamp_lines("0") == 1
    assert logsources.clamp_lines(10**9) == logsources.MAX_LINES


def test_reading_a_file_returns_only_the_last_lines(tmp_path):
    path = tmp_path / "access.log"
    path.write_text("\n".join(f"line {n}" for n in range(500)) + "\n")
    source = logsources.LogSource("access", "Access log", "file", str(path))
    read = logsources.read(source, 5)
    assert read.error is None
    assert read.text.splitlines() == [f"line {n}" for n in range(495, 500)]


def test_a_huge_file_is_tailed_rather_than_slurped(tmp_path, monkeypatch):
    monkeypatch.setattr(logsources, "TAIL_BYTES", 200)
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"line {n}" for n in range(500)) + "\n")
    # Ask for far more lines than 200 bytes can hold, so what comes back is
    # bounded by the tail, and the half line the seek landed in is dropped.
    read = logsources.read(logsources.LogSource("x", "x", "file", str(path)), 500)
    assert len(read.text.splitlines()) < 40
    assert read.text.splitlines()[-1] == "line 499"
    assert all(re.fullmatch(r"line \d+", line) for line in read.text.splitlines())


def test_an_unreadable_file_reports_why(tmp_path):
    source = logsources.LogSource("x", "x", "file", str(tmp_path / "gone.log"))
    read = logsources.read(source, 10)
    assert read.text == ""
    assert "gone.log" in read.error


def test_an_empty_file_says_so(tmp_path):
    path = tmp_path / "empty.log"
    path.touch()
    read = logsources.read(logsources.LogSource("x", "x", "file", str(path)), 10)
    assert read.error is None
    assert "empty" in read.text


def test_php_error_log_paths_read_the_ini(tmp_path):
    from lamplight import php

    conf_d = tmp_path / "8.5" / "apache2" / "conf.d"
    conf_d.mkdir(parents=True)
    (conf_d.parent / "php.ini").write_text("error_log = /var/log/php_errors.log\n")
    assert php.error_log_paths(tmp_path) == ["/var/log/php_errors.log"]


def test_php_error_log_paths_drop_the_magic_values(tmp_path):
    from lamplight import php

    conf_d = tmp_path / "8.5" / "cli" / "conf.d"
    conf_d.mkdir(parents=True)
    (conf_d.parent / "php.ini").write_text("error_log = syslog\n")
    assert php.error_log_paths(tmp_path) == []


def test_journal_reads_are_asked_for_by_unit(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "Sep 10 08:00:00 host apache2[1]: started\n"
        stderr = ""

    monkeypatch.setattr(logsources.systemops, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        logsources.systemops, "run", lambda argv, **kw: calls.append(argv) or Result()
    )
    read = logsources.read(logsources.LogSource("journal", "j", "journal", "apache2"), 7)
    assert calls == [["journalctl", "-u", "apache2.service", "-n", "7", "--no-pager"]]
    assert "started" in read.text


def test_a_failing_journalctl_reports_its_own_error(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Failed to add match: Invalid argument\n"

    monkeypatch.setattr(logsources.systemops, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(logsources.systemops, "run", lambda argv, **kw: Result())
    read = logsources.read(logsources.LogSource("journal", "j", "journal", "apache2"))
    assert read.error == "Failed to add match: Invalid argument"


def test_no_journalctl_means_no_journal_source(monkeypatch):
    monkeypatch.setattr(logsources.systemops, "which", lambda name: None)
    assert not any(s.kind == "journal" for s in logsources.sources_for("mailpit"))


def test_a_real_apache_dir_is_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(logsources, "APACHE_LOG_DIR", tmp_path)
    monkeypatch.setattr(logsources.systemops, "which", lambda name: None)
    for name in ("error.log", "access.log", "mysite-access.log"):
        (tmp_path / name).write_text("hello\n")
    ids = [source.id for source in logsources.sources_for("apache")]
    assert ids == ["error", "access", "mysite-access"]


def test_sources_are_addressed_by_id_never_by_path(logdirs):
    """The browser names `apache/error`; a path from the query string must not
    be able to turn into a read."""
    apache, _ = logdirs
    assert logsources.find("apache", "../../etc/shadow").id == "error"
    assert Path(logsources.find("apache", "error").target) == apache / "error.log"
