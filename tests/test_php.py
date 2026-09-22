import json

import pytest

from lamplight import config, db, php, systemops
from lamplight.catalog import get_component
from lamplight.installer import Installer
from lamplight.jobs import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(db.connect(tmp_path / "jobs.sqlite3"))


@pytest.fixture
def fake_etc(tmp_path, monkeypatch):
    """A stand-in /etc/php with two SAPIs, each with its own php.ini."""
    root = tmp_path / "etc-php"
    for sapi in ("apache2", "cli"):
        (root / "8.5" / sapi / "conf.d").mkdir(parents=True)
        (root / "8.5" / sapi / "php.ini").write_text(
            "[PHP]\nmemory_limit = 128M   ; the stock value\ndisplay_errors = Off\n",
            encoding="utf-8",
        )
    (root / "8.5" / "mods-available").mkdir()  # no conf.d, so not a SAPI
    monkeypatch.setattr(php, "PHP_ETC", root)
    return root


@pytest.fixture
def rooted(monkeypatch):
    """Pretend to be root and never actually shell out."""
    monkeypatch.setattr(systemops, "is_root", lambda: True)
    monkeypatch.setattr(Installer, "_run", lambda self, job_id, argv: 0)


def logged(store, job_id):
    return store.get(job_id)["log"]


# -- the default selection ------------------------------------------------


def test_defaults_are_the_extensions_v0_1_installed():
    assert php.DEFAULT_EXTENSIONS == (
        "mysql",
        "curl",
        "mbstring",
        "xml",
        "zip",
        "gd",
        "intl",
        "bcmath",
    )
    assert get_component("php").extra_packages == php.DEFAULT_PACKAGES
    assert "php-mysql" in php.DEFAULT_PACKAGES


def test_the_defaults_are_unique():
    assert len(php.DEFAULT_EXTENSIONS) == len(set(php.DEFAULT_EXTENSIONS))


# -- extension names ------------------------------------------------------


@pytest.mark.parametrize("raw", ["curl", "PHP-Curl", "  php-CURL  ", "Curl"])
def test_the_php_prefix_and_case_are_optional(raw):
    assert php.clean_extension(raw) == "curl"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "php-",
        "../../etc/passwd",
        "curl; rm -rf /",
        "curl extra",
        "-curl",
        "curl\nphp-evil",
        "x" * 40,
    ],
)
def test_bad_extension_names_are_refused(raw):
    with pytest.raises(ValueError):
        php.clean_extension(raw)


def test_clean_extensions_dedupes_and_keeps_order():
    names, problems = php.clean_extensions(["curl", "php-curl", "redis", "CURL"])
    assert names == ("curl", "redis")
    assert problems == []


def test_clean_extensions_reports_each_bad_name_without_dropping_the_good_ones():
    names, problems = php.clean_extensions(["curl", "no good", 7])
    assert names == ("curl",)
    assert len(problems) == 2


def test_the_selection_is_capped():
    names, problems = php.clean_extensions(f"ext{n}" for n in range(php.MAX_EXTENSIONS + 5))
    assert len(names) == php.MAX_EXTENSIONS
    assert problems


# -- option values --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("memory_limit", "512m", "512M"),
        ("memory_limit", "-1", "-1"),
        ("memory_limit", "268435456", "268435456"),
        ("max_execution_time", "60", "60"),
        ("max_execution_time", 60, "60"),
        ("display_errors", "on", "On"),
        ("display_errors", "0", "Off"),
        ("opcache.enable", "true", "On"),
        ("date.timezone", "Europe/Berlin", "Europe/Berlin"),
        ("date.timezone", "UTC", "UTC"),
        ("error_reporting", "e_all", "E_ALL"),
    ],
)
def test_option_values_are_normalised(name, raw, expected):
    assert php.clean_option(name, raw) == expected


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("memory_limit", "512M\nevil = 1"),
        ("memory_limit", "lots"),
        ("memory_limit", "512MB"),
        ("max_execution_time", "-5"),
        ("max_execution_time", "1e6"),
        ("opcache.memory_consumption", "4"),
        ("display_errors", "maybe"),
        ("date.timezone", "../../etc/passwd"),
        ("date.timezone", "UTC\nmemory_limit = -1"),
        ("error_reporting", "E_ALL & shutdown()"),
        ("allow_url_fopen", "On"),
        ("memory_limit", ["512M"]),
    ],
)
def test_bad_option_values_are_refused(name, raw):
    with pytest.raises(ValueError):
        php.clean_option(name, raw)


def test_a_blank_value_means_stop_overriding():
    cleaned, problems = php.clean_options({"memory_limit": "  ", "max_execution_time": "60"})
    assert cleaned == {"max_execution_time": "60"}
    assert problems == []


def test_unmanaged_directives_are_refused_not_written():
    cleaned, problems = php.clean_options({"open_basedir": "/", "memory_limit": "256M"})
    assert cleaned == {"memory_limit": "256M"}
    assert problems == ["open_basedir is not an option Lamplight manages"]


def test_options_come_back_in_catalog_order():
    submitted = {name: php.get_option(name).php_default for name in reversed(php.OPTION_NAMES)}
    cleaned, _ = php.clean_options(submitted)
    assert list(cleaned) == list(php.OPTION_NAMES)


# -- the stored selection -------------------------------------------------


def test_config_round_trips(paths):
    php.save_config(paths, php.PhpConfig(extensions=("curl",), options={"memory_limit": "256M"}))
    reloaded = php.load_config(paths)
    assert reloaded.extensions == ("curl",)
    assert reloaded.options == {"memory_limit": "256M"}
    assert reloaded.packages == ("php-curl",)


def test_a_missing_file_is_written_with_the_defaults(paths):
    assert not paths.php_path.exists()
    assert php.load_config(paths).extensions == php.DEFAULT_EXTENSIONS
    assert json.loads(paths.php_path.read_text())["extensions"] == list(php.DEFAULT_EXTENSIONS)


def test_a_corrupt_file_falls_back_to_the_defaults(paths):
    paths.php_path.write_text("{not json", encoding="utf-8")
    assert php.load_config(paths) == php.PhpConfig()


def test_junk_on_disk_is_dropped_rather_than_loaded(paths):
    paths.php_path.write_text(
        json.dumps(
            {
                "extensions": ["curl", "../evil", 3],
                "options": {"memory_limit": "256M", "open_basedir": "/", "post_max_size": "nope"},
            }
        ),
        encoding="utf-8",
    )
    loaded = php.load_config(paths)
    assert loaded.extensions == ("curl",)
    assert loaded.options == {"memory_limit": "256M"}


# -- the drop-in ----------------------------------------------------------


def test_drop_in_lists_the_directives_in_catalog_order():
    text = php.drop_in_text({"date.timezone": "UTC", "memory_limit": "256M"})
    directives = [line for line in text.splitlines() if line and not line.startswith(";")]
    assert directives == ["memory_limit = 256M", "date.timezone = UTC"]


def test_sapis_are_newest_first_with_the_web_ones_before_the_cli(fake_etc):
    (fake_etc / "8.4" / "apache2" / "conf.d").mkdir(parents=True)
    assert [(s.version, s.name) for s in php.sapis()] == [
        ("8.5", "apache2"),
        ("8.5", "cli"),
        ("8.4", "apache2"),
    ]


def test_effective_options_resolve_php_ini_then_conf_d(fake_etc):
    sapi = php.sapis()[0]
    (sapi.conf_d / "20-other.ini").write_text("memory_limit = 256M\n", encoding="utf-8")
    sapi.drop_in.write_text("memory_limit = 512M\n", encoding="utf-8")

    resolved = php.effective_options(sapi)
    assert resolved["memory_limit"] == ("512M", str(sapi.drop_in))
    # The trailing "; the stock value" comment is not part of the value.
    assert resolved["display_errors"] == ("Off", str(sapi.directory / "php.ini"))
    assert "opcache.enable" not in resolved


def test_effective_options_ignore_directives_lamplight_does_not_manage(fake_etc):
    sapi = php.sapis()[0]
    sapi.drop_in.write_text("open_basedir = /srv\n", encoding="utf-8")
    assert "open_basedir" not in php.effective_options(sapi)


# -- applying it ----------------------------------------------------------


def test_php_installs_the_saved_selection_not_the_catalog_default(paths, store):
    php.save_config(paths, php.PhpConfig(extensions=("curl", "redis")))
    installer = Installer(paths, store)
    assert installer._packages(get_component("php")) == [
        "php",
        "libapache2-mod-php",
        "php-cli",
        "php-curl",
        "php-redis",
    ]


def test_options_are_written_to_every_sapi(paths, store, fake_etc, rooted):
    installer = Installer(paths, store)
    job_id = store.create("php", "options")
    installer.apply_php_options(job_id, {"memory_limit": "512M"})

    for sapi in php.sapis():
        assert "memory_limit = 512M" in sapi.drop_in.read_text()
        assert php.effective_options(sapi)["memory_limit"][0] == "512M"
    assert php.load_config(paths).options == {"memory_limit": "512M"}
    assert "Writing" in logged(store, job_id)


def test_clearing_every_option_deletes_the_drop_in(paths, store, fake_etc, rooted):
    installer = Installer(paths, store)
    installer.apply_php_options(store.create("php", "options"), {"memory_limit": "512M"})
    job_id = store.create("php", "options")
    installer.apply_php_options(job_id, {})

    assert not any(sapi.drop_in.exists() for sapi in php.sapis())
    assert php.effective_options(php.sapis()[0])["memory_limit"][0] == "128M"
    assert "Removing" in logged(store, job_id)


def test_php_ini_itself_is_never_touched(paths, store, fake_etc, rooted):
    original = {
        sapi.directory / "php.ini": (sapi.directory / "php.ini").read_text() for sapi in php.sapis()
    }
    installer = Installer(paths, store)
    installer.apply_php_options(store.create("php", "options"), {"memory_limit": "512M"})
    for path, text in original.items():
        assert path.read_text() == text


def test_options_survive_php_not_being_installed_yet(paths, store, tmp_path, monkeypatch):
    monkeypatch.setattr(php, "PHP_ETC", tmp_path / "nothing-here")
    installer = Installer(paths, store)
    job_id = store.create("php", "options")
    installer.apply_php_options(job_id, {"memory_limit": "512M"})
    assert php.load_config(paths).options == {"memory_limit": "512M"}
    assert "nothing to write until PHP is there" in logged(store, job_id)


def test_removing_php_takes_the_drop_in_with_it(paths, store, fake_etc, rooted):
    installer = Installer(paths, store)
    installer.apply_php_options(store.create("php", "options"), {"memory_limit": "512M"})
    job_id = store.create("php", "remove")
    installer.remove(job_id, "php")
    assert not any(sapi.drop_in.exists() for sapi in php.sapis())


def test_setting_extensions_saves_before_apt_runs(paths, store, monkeypatch, rooted):
    """A failed apt run must still leave the panel showing what was asked for."""
    monkeypatch.setattr(
        systemops, "package_states", lambda names: {n: systemops.PackageState() for n in names}
    )
    installer = Installer(paths, store)
    job_id = store.create("php", "extensions")
    installer.set_php_extensions(job_id, ("curl", "redis"))
    assert php.load_config(paths).extensions == ("curl", "redis")
    assert "PHP is not installed yet" in logged(store, job_id)


def test_setting_extensions_adds_and_removes_only_what_changed(paths, store, monkeypatch, rooted):
    php.save_config(paths, php.PhpConfig(extensions=("curl", "gd")))
    installed = {"php", "libapache2-mod-php", "php-cli", "php-curl", "php-gd"}
    monkeypatch.setattr(
        systemops,
        "package_states",
        lambda names: {name: systemops.PackageState(installed=name in installed) for name in names},
    )
    ran = []
    monkeypatch.setattr(Installer, "_run", lambda self, job_id, argv: ran.append(argv) or 0)

    installer = Installer(paths, store)
    installer.set_php_extensions(store.create("php", "extensions"), ("curl", "redis"))

    assert ["apt-get", "install", "-y", "--no-install-recommends", "php-redis"] in ran
    assert ["apt-get", "remove", "-y", "php-gd"] in ran
    # php-curl was already there and stays ticked, so it is in neither list.
    assert not any("php-curl" in argv for argv in ran)
    assert ["apt-get", "autoremove", "-y"] in ran


def test_an_unchanged_selection_runs_no_apt_at_all(paths, store, monkeypatch, rooted):
    php.save_config(paths, php.PhpConfig(extensions=("curl",)))
    installed = {"php", "libapache2-mod-php", "php-cli", "php-curl"}
    monkeypatch.setattr(
        systemops,
        "package_states",
        lambda names: {name: systemops.PackageState(installed=name in installed) for name in names},
    )
    ran = []
    monkeypatch.setattr(Installer, "_run", lambda self, job_id, argv: ran.append(argv) or 0)

    installer = Installer(paths, store)
    job_id = store.create("php", "extensions")
    installer.set_php_extensions(job_id, ("curl",))
    assert ran == []
    assert "Nothing to change" in logged(store, job_id)


def test_atomic_write_leaves_no_partial_file(tmp_path):
    target = tmp_path / "99-lamplight.ini"
    config.atomic_write(target, "memory_limit = 512M\n", mode=0o644)
    assert target.read_text() == "memory_limit = 512M\n"
    assert list(tmp_path.iterdir()) == [target]
