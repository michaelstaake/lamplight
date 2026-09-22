import os

from conftest import OWN_MEMORY, SERVICE_MEMORY, wait_for_job

from lamplight import __version__


def test_healthz_needs_no_token(app):
    body = app.test_client().get("/healthz").get_json()
    assert body["ok"] is True
    assert body["version"] == __version__


def test_pages_redirect_to_login_without_a_token(app):
    client = app.test_client()
    for path in ("/", "/logs", "/settings", "/c/apache", "/partials/stack"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path


def test_api_returns_401_rather_than_a_redirect(app):
    client = app.test_client()
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/components/apache/install").status_code == 401


def test_wrong_token_is_rejected(app):
    client = app.test_client()
    assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/?token=nope").status_code == 302


def test_query_token_unlocks_and_starts_a_session(app):
    token = app.config["TOKEN"]
    client = app.test_client()
    assert client.get(f"/?token={token}").status_code == 200
    assert client.get("/").status_code == 200  # session cookie now carries it


def test_bearer_token_works_for_the_api(app):
    token = app.config["TOKEN"]
    response = app.test_client().get("/api/state", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert len(response.get_json()["components"]) == 5


def test_login_form_and_logout(app):
    client = app.test_client()
    assert client.post("/login", data={"token": "wrong"}).headers["Location"].endswith("error=1")
    assert client.post("/login", data={"token": app.config["TOKEN"]}).status_code == 302
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/").status_code == 302


def test_security_headers_are_set(app):
    headers = app.test_client().get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_dashboard_lists_mariadb_and_mailpit(client):
    body = client.get("/").get_data(as_text=True)
    assert "MariaDB" in body and "Mailpit" in body
    assert "MySQL" not in body and "MailHog" not in body


def test_nav_has_a_link_per_component_in_catalog_order(client):
    from lamplight.catalog import COMPONENT_IDS

    body = client.get("/").get_data(as_text=True)
    positions = [body.index(f'data-nav="{cid}"') for cid in COMPONENT_IDS]
    assert positions == sorted(positions), "nav order must follow the catalog"
    for label in ("Dashboard", "Settings"):
        assert f">{label}</span>" in body
    assert ">Logs</span>" not in body


def test_every_component_has_its_own_page(client):
    from lamplight.catalog import COMPONENTS

    for component in COMPONENTS:
        body = client.get(f"/c/{component.id}").get_data(as_text=True)
        assert f"<h1>{component.name}</h1>" in body
        assert 'id="component-panel"' in body


def test_unknown_component_page_is_404(client):
    assert client.get("/c/mysql").status_code == 404
    assert client.get("/partials/component/mailhog").status_code == 404


def test_component_page_offers_install_then_service_controls(client):
    body = client.get("/c/apache").get_data(as_text=True)
    assert 'data-act="install"' in body
    assert 'data-act="restart"' not in body

    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "success"

    body = client.get("/c/apache").get_data(as_text=True)
    assert 'data-act="restart"' in body
    assert 'data-act="remove"' in body
    assert 'data-act="install"' not in body


def test_blocked_component_page_disables_install_and_links_to_blockers(client):
    body = client.get("/c/phpmyadmin").get_data(as_text=True)
    assert "disabled" in body
    assert "/c/apache" in body and "/c/mariadb" in body


def test_component_partial_matches_the_page_panel(client):
    partial = client.get("/partials/component/mariadb").get_data(as_text=True)
    assert partial.strip() in client.get("/c/mariadb").get_data(as_text=True)


def test_log_partial_matches_the_page_and_is_empty_before_install(client):
    assert client.get("/partials/component/apache/log").get_data(as_text=True) == ""
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    partial = client.get("/partials/component/apache/log").get_data(as_text=True)
    page = client.get("/c/apache").get_data(as_text=True)
    assert 'id="service-log"' in partial
    assert partial.strip() in page
    assert page.index('id="component-panel"') < page.index('id="service-log"')


def test_component_page_shows_the_service_log_not_the_job_log(client):
    """The job log says Lamplight restarted Apache. This panel is Apache's own."""
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    body = client.get("/c/apache").get_data(as_text=True)
    assert "Error log" in body and "Access log" in body
    assert 'data-source="error"' in body
    assert "[job] install apache" not in body
    assert 'id="log-play"' in body and 'id="log-pause"' in body
    assert 'id="log-follow"' not in body and 'id="log-refresh"' not in body
    assert "log-lines" not in body


def test_component_page_offers_each_log_source(client):
    job_id = client.post("/api/components/mariadb/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    body = client.get("/c/mariadb?source=journal").get_data(as_text=True)
    assert "systemd journal" in body
    assert 'data-source="journal"' in body
    assert 'class="subtab active"' in body


def test_a_single_source_hides_the_source_switcher(client):
    job_id = client.post("/api/components/mailpit/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    body = client.get("/c/mailpit").get_data(as_text=True)
    assert "subtab" not in body
    assert "systemd journal" not in body
    assert 'data-source="journal"' in body


def test_log_tail_api_reads_the_named_source(client, host):
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    payload = client.get("/api/logs/apache?source=access").get_json()
    assert payload["id"] == "access"
    assert payload["target"] == str(host.apache_logs / "access.log")
    assert payload["error"] is None
    assert "GET /" in payload["text"]


def test_log_tail_api_rejects_unknown_components_and_sources(client):
    assert client.get("/api/logs/mysql").status_code == 404
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    # An unknown source falls back to the first one rather than 404ing, so a
    # stale bookmark still shows you a log.
    assert client.get("/api/logs/apache?source=../../etc/shadow").get_json()["id"] == "error"


def test_uninstalled_components_have_no_log_panel(client):
    body = client.get("/c/apache").get_data(as_text=True)
    assert 'id="service-log"' not in body
    assert client.get("/api/logs/apache").status_code == 404
    assert client.get("/api/logs/php").status_code == 404

    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    body = client.get("/c/apache").get_data(as_text=True)
    assert 'id="service-log"' in body
    assert 'id="service-log"' not in client.get("/c/php").get_data(as_text=True)
    assert client.get("/c/php").status_code == 200
    assert client.get("/api/logs/php").status_code == 404


def test_old_log_urls_redirect_onto_the_pages_that_replaced_them(client):
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    index = client.get("/logs")
    assert index.status_code == 302
    assert index.headers["Location"].endswith("/")
    apache = client.get("/logs/apache?source=access")
    assert apache.status_code == 302
    assert apache.headers["Location"].endswith("/c/apache?source=access")


def test_job_log_is_reachable_from_the_dashboard(client):
    job_id = client.post("/api/components/mariadb/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    index = client.get("/").get_data(as_text=True)
    assert f'href="/?job={job_id}"' in index
    body = client.get(f"/?job={job_id}").get_data(as_text=True)
    assert "[job] install mariadb" in body
    assert client.get("/?job=zzzzzz").status_code == 404
    missing = client.get("/logs/job/zzzzzz")
    assert missing.status_code == 404
    found = client.get(f"/logs/job/{job_id}")
    assert found.status_code == 302
    assert f"job={job_id}" in found.headers["Location"]


def test_php_page_has_no_log_panel(client):
    body = client.get("/c/php").get_data(as_text=True)
    assert 'id="service-log"' not in body
    assert ">Notes</h2>" not in body


def test_unknown_component_and_action_are_rejected(client):
    assert client.post("/api/components/mysql/install").status_code == 404
    assert client.post("/api/components/apache/nuke").status_code == 400


def test_install_runs_a_job_and_updates_state(client):
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "success"
    assert "[job] install apache" in job["log"]

    apache = next(
        c for c in client.get("/api/state").get_json()["components"] if c["id"] == "apache"
    )
    assert apache["status"] == "running"


def test_install_unblocks_dependents(client):
    for component in ("apache", "php"):
        job_id = client.post(f"/api/components/{component}/install").get_json()["job_id"]
        assert wait_for_job(client, job_id)["status"] == "success"
    php = next(c for c in client.get("/api/state").get_json()["components"] if c["id"] == "php")
    assert php["blocked_by"] == []
    assert php["status"] == "installed"


def test_the_dashboard_counts_memory_for_lamplight_and_the_stack(client):
    """The tile used to count running services; a footprint is more use."""
    body = client.get("/").get_data(as_text=True)
    assert "Memory Usage" in body
    assert "Services running" not in body

    job_id = client.post("/api/components/mariadb/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    payload = client.get("/api/state").get_json()
    assert payload["memory"]["total"] == OWN_MEMORY + SERVICE_MEMORY
    assert [part["name"] for part in payload["memory"]["parts"]] == ["Lamplight", "MariaDB"]
    # The tile shows one figure. The breakdown stays in /api/state, where it
    # says what the total covers without crowding the dashboard.
    body = client.get("/").get_data(as_text=True)
    assert ">54 MB<" in body
    assert "MariaDB 24 MB" not in body


def test_a_stopped_service_stops_counting_towards_memory(client):
    for action in ("install", "stop"):
        job_id = client.post(f"/api/components/mariadb/{action}").get_json()["job_id"]
        wait_for_job(client, job_id)
    payload = client.get("/api/state").get_json()
    assert payload["memory"]["total"] == OWN_MEMORY
    assert [part["name"] for part in payload["memory"]["parts"]] == ["Lamplight"]


def test_stack_partial_matches_the_dashboard_grid(client):
    partial = client.get("/partials/stack").get_data(as_text=True)
    assert 'class="stack-row" href="/c/apache"' in partial
    assert partial.strip() in client.get("/").get_data(as_text=True)


def test_job_stream_ends_with_a_terminal_status(client):
    job_id = client.post("/api/components/mailpit/install").get_json()["job_id"]
    stream = client.get(f"/api/jobs/{job_id}/stream").get_data(as_text=True)
    assert "success" in stream


def test_missing_job_is_404(client):
    assert client.get("/api/jobs/zzzzzz").status_code == 404


def test_dashboard_lists_job_history(client):
    started = client.post("/api/components/apache/install").get_json()
    wait_for_job(client, started["job_id"])
    assert "apache" in client.get("/").get_data(as_text=True)


def test_settings_api_saves_allowed_keys_only(client, paths):
    from lamplight import config

    response = client.post(
        "/api/settings",
        json={"port": 4000, "document_root": "/srv/site", "host": "0.0.0.0"},
    )
    assert response.status_code == 200
    saved = config.load_settings(paths)
    assert (saved.port, saved.document_root) == (4000, "/srv/site")
    assert saved.host == "127.0.0.1", "the bind address must not be settable from the browser"


def test_settings_api_rejects_non_objects(client):
    assert client.post("/api/settings", json=["nope"]).status_code == 400


def test_dashboard_states_the_retention_cap_not_the_row_count(client):
    """The lede used to interpolate len(jobs), so one job read as 'the most
    recent 1 are kept'."""
    started = client.post("/api/components/apache/install").get_json()
    wait_for_job(client, started["job_id"])
    body = client.get("/").get_data(as_text=True)
    assert "most recent 200 jobs are kept" in body


def test_ui_is_dark_only_and_square(client):
    body = client.get("/").get_data(as_text=True)
    assert 'content="dark"' in body
    assert "data-theme" not in body

    css = client.get("/static/css/app.css").get_data(as_text=True)
    assert "border-radius: 0" in css
    assert "prefers-color-scheme" not in css
    assert "fonts.googleapis.com" not in css


def test_the_flame_mark_is_used_in_the_app_not_just_the_favicon(client, app):
    """The rail used to draw a CSS blob; it must be the same shape as the icon."""
    shape = "M16 2c4 6 9 8 9 14a9 9 0 0 1-18 0c0-4 2-6 4-9 1 2 2 3 3 3 1-3 1-6 2-8z"
    assert shape in client.get("/static/flame.svg").get_data(as_text=True)
    assert shape in client.get("/").get_data(as_text=True)
    assert shape in app.test_client().get("/login").get_data(as_text=True)


def test_boot_state_is_blank_until_the_component_is_installed(client):
    body = client.get("/c/apache").get_data(as_text=True)
    assert "Enabled at boot" in body
    assert ">no<" not in body

    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    assert ">yes<" in client.get("/c/apache").get_data(as_text=True)


def test_component_refresh_matches_the_component_page(client):
    """The refreshed partial is exactly what the component page was built from."""
    job_id = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, job_id)
    partial = client.get("/partials/component/apache").get_data(as_text=True)
    assert partial.strip() in client.get("/c/apache").get_data(as_text=True)


def test_the_root_banner_and_settings_agree_about_the_host(client):
    """The banner used to be suppressed while Settings still said otherwise."""
    rooted = os.geteuid() == 0
    assert ("Not running as root" in client.get("/").get_data(as_text=True)) is not rooted
    settings = client.get("/settings").get_data(as_text=True)
    assert f"<dd>{'yes' if rooted else 'no'}</dd>" in settings


def test_the_token_is_never_echoed_back_into_a_page(app, client):
    """Settings shows that a token exists, not what it is."""
    body = client.get("/settings").get_data(as_text=True)
    assert app.config["TOKEN"] not in body
    assert "•" in body


# -- php extensions and options ------------------------------------------


def test_only_the_php_page_carries_the_php_panels(client):
    body = client.get("/c/php").get_data(as_text=True)
    assert 'id="php-panel"' in body
    assert 'id="php-version-form"' in body
    assert 'id="php-extensions-form"' in body
    assert 'id="php-options-form"' in body
    assert "Distro default" in body
    assert 'id="php-panel"' not in client.get("/c/apache").get_data(as_text=True)


def test_the_default_extensions_are_the_starting_tags(client):
    from lamplight import php

    body = client.get("/c/php").get_data(as_text=True)
    for name in php.DEFAULT_EXTENSIONS:
        assert f'value="{name}"' in body, name
        assert f"php-{name}" in body, name
    # Only the selection is listed now, so an extension nobody picked is absent.
    assert 'value="xdebug"' not in body


def test_every_managed_option_has_a_field(client):
    from lamplight import php

    body = client.get("/c/php").get_data(as_text=True)
    for option in php.OPTIONS:
        assert f'name="{option.name}"' in body, option.name


def test_php_partial_matches_the_php_page(client):
    partial = client.get("/partials/php").get_data(as_text=True)
    assert partial.strip() in client.get("/c/php").get_data(as_text=True)


def test_extensions_can_be_added_and_removed(client, paths):
    from lamplight import php

    job_id = client.post(
        "/api/php/extensions", json={"extensions": ["curl", "php-redis"]}
    ).get_json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "success"

    assert php.load_config(paths).extensions == ("curl", "redis")
    body = client.get("/c/php").get_data(as_text=True)
    assert 'value="redis"' in body
    assert 'value="gd"' not in body


def test_a_custom_extension_gets_its_own_tag(client):
    job_id = client.post("/api/php/extensions", json={"extensions": ["curl", "snmp"]}).get_json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    body = client.get("/c/php").get_data(as_text=True)
    assert 'value="snmp"' in body
    assert "php-snmp" in body


def test_the_selection_decides_what_install_php_would_pull_in(client, paths):
    started = client.post("/api/php/extensions", json={"extensions": ["curl"]}).get_json()
    wait_for_job(client, started["job_id"])
    php_item = next(
        c for c in client.get("/api/state").get_json()["components"] if c["id"] == "php"
    )
    assert php_item["packages"] == ["php", "libapache2-mod-php", "php-cli", "php-curl"]


def test_a_version_pin_is_saved_and_shown(client, paths):
    from lamplight import php

    job_id = client.post("/api/php/version", json={"version": "PHP 8.5"}).get_json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "success"
    assert php.load_config(paths).version == "8.5"
    body = client.get("/c/php").get_data(as_text=True)
    assert 'value="8.5" selected' in body
    assert "php8.5-mysql" in body


def test_a_bad_version_is_refused_before_a_job_is_created(client):
    before = len(client.get("/api/jobs").get_json()["jobs"])
    assert client.post("/api/php/version", json={"version": "8.6"}).status_code == 400
    assert client.post("/api/php/version", json={"version": 85}).status_code == 400
    assert client.post("/api/php/version", json={}).status_code == 400
    assert len(client.get("/api/jobs").get_json()["jobs"]) == before


def test_an_unknown_extension_is_refused_once_the_pinned_runtime_is_known(
    client, paths, monkeypatch
):
    from lamplight import php, systemops

    php.save_config(paths, php.PhpConfig(version="8.5", extensions=()))

    def exist(names):
        return {name: name != "php8.5-nope" for name in names}

    monkeypatch.setattr(systemops, "apt_packages_exist", exist)
    response = client.post("/api/php/extensions", json={"extensions": ["nope"]})
    assert response.status_code == 400
    assert "php8.5-nope" in response.get_json()["error"]


def test_extensions_are_not_refused_before_the_pinned_runtime_is_in_apt(client, paths, monkeypatch):
    from lamplight import php, systemops

    php.save_config(paths, php.PhpConfig(version="8.5", extensions=()))
    monkeypatch.setattr(
        systemops, "apt_packages_exist", lambda names: {name: False for name in names}
    )
    assert client.post("/api/php/extensions", json={"extensions": ["curl"]}).status_code == 200


def test_options_are_saved_and_shown_back(client, paths):
    from lamplight import php

    job_id = client.post(
        "/api/php/options", json={"options": {"memory_limit": "512m", "display_errors": "on"}}
    ).get_json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "success"

    assert php.load_config(paths).options == {"memory_limit": "512M", "display_errors": "On"}
    assert 'value="512M"' in client.get("/c/php").get_data(as_text=True)


def test_a_bad_option_is_refused_before_a_job_is_created(client):
    before = len(client.get("/api/jobs").get_json()["jobs"])
    response = client.post(
        "/api/php/options", json={"options": {"memory_limit": "512M\nopen_basedir = /"}}
    )
    assert response.status_code == 400
    assert "memory_limit" in response.get_json()["error"]
    assert len(client.get("/api/jobs").get_json()["jobs"]) == before


def test_a_bad_extension_name_is_refused_before_a_job_is_created(client):
    before = len(client.get("/api/jobs").get_json()["jobs"])
    assert client.post("/api/php/extensions", json={"extensions": ["../evil"]}).status_code == 400
    assert len(client.get("/api/jobs").get_json()["jobs"]) == before


def test_php_endpoints_want_the_right_shape(client):
    assert client.post("/api/php/extensions", json={"extensions": "curl"}).status_code == 400
    assert client.post("/api/php/options", json=["nope"]).status_code == 400
    assert client.post("/api/php/extensions", json={}).status_code == 400
    assert client.post("/api/php/version", json=[]).status_code == 400


def test_php_endpoints_are_behind_the_token(app):
    client = app.test_client()
    assert client.get("/api/php").status_code == 401
    assert client.post("/api/php/options", json={"options": {}}).status_code == 401
    assert client.post("/api/php/extensions", json={"extensions": []}).status_code == 401
    assert client.post("/api/php/version", json={"version": "8.5"}).status_code == 401
    assert client.get("/partials/php").status_code == 302


def test_php_jobs_show_up_in_the_log_history(client):
    started = client.post("/api/php/options", json={"options": {"memory_limit": "256M"}}).get_json()
    wait_for_job(client, started["job_id"])
    body = client.get("/").get_data(as_text=True)
    assert "options" in body
    assert "[job] options" in client.get(f"/?job={started['job_id']}").get_data(as_text=True)


def test_apache_page_offers_the_firewall_button_next_to_the_boot_toggle(client):
    started = client.post("/api/components/apache/install").get_json()
    wait_for_job(client, started["job_id"])
    body = client.get("/c/apache").get_data(as_text=True)
    assert 'data-act="firewall-open"' in body
    assert "Allow 80/443 in UFW" in body
    # Same row of controls as the systemd buttons.
    assert body.index("Disable at boot") < body.index("Allow 80/443 in UFW")


def test_firewall_button_flips_to_block_once_the_ports_are_open(client):
    started = client.post("/api/components/apache/install").get_json()
    wait_for_job(client, started["job_id"])
    started = client.post("/api/components/apache/firewall-open").get_json()
    assert wait_for_job(client, started["job_id"])["status"] == "success"

    body = client.get("/c/apache").get_data(as_text=True)
    assert "Block 80/443 in UFW" in body
    assert "Allow 80/443 in UFW" not in body


def test_no_firewall_button_for_the_database(client):
    started = client.post("/api/components/mariadb/install").get_json()
    wait_for_job(client, started["job_id"])
    body = client.get("/c/mariadb").get_data(as_text=True)
    assert "UFW" not in body
    assert "firewall-open" not in body


def test_firewall_action_is_refused_for_unknown_components(client):
    assert client.post("/api/components/mysql/firewall-open").status_code == 404


def test_no_page_lists_what_gets_installed(client):
    for component in ("apache", "php", "mariadb", "phpmyadmin", "mailpit"):
        started = client.post(f"/api/components/{component}/install").get_json()
        wait_for_job(client, started["job_id"])
    for path in ("/", "/settings", "/c/apache", "/c/php", "/c/mailpit"):
        body = client.get(path).get_data(as_text=True)
        assert "What gets installed" not in body, path


def test_pages_have_a_heading_and_no_subtitle(client):
    for path in ("/", "/settings", "/c/mariadb"):
        body = client.get(path).get_data(as_text=True)
        assert "page-head" in body, path
        assert 'class="lede"' not in body, path
        assert ">Notes</h2>" not in body, path


# -- mariadb databases and users ------------------------------------------


def _mariadb_running(host, monkeypatch, stdout=""):
    """MariaDB is up, and the client records SQL instead of connecting."""
    from lamplight import mariadb, systemops

    host.apply("mariadb", "install")
    monkeypatch.setattr(systemops, "is_root", lambda: True)
    calls = []

    def fake_execute(sql, *, secret=None):
        calls.append({"sql": sql, "secret": secret})
        return stdout

    monkeypatch.setattr(mariadb, "execute", fake_execute)
    return calls


_MARIADB_LISTING = "\n".join(
    [
        "db\tapp",
        "db\tphpmyadmin",
        "db\tmysql",
        "user\tapp\tlocalhost",
        "user\tmysql\tlocalhost",
        "user\tphpmyadmin\t127.0.0.1",
        "grant\tapp\tlocalhost\tapp",
        "grant\tphpmyadmin\t127.0.0.1\tphpmyadmin",
    ]
)


def test_mariadb_tools_wait_until_it_is_installed(client, monkeypatch):
    from lamplight import mariadb

    def fail(sql, *, secret=None):
        raise AssertionError("mysql should not be asked yet")

    monkeypatch.setattr(mariadb, "execute", fail)
    body = client.get("/c/mariadb").get_data(as_text=True)
    assert 'id="mariadb-panel"' in body
    assert 'id="mariadb-database-form"' not in body
    assert client.get("/partials/mariadb").get_data(as_text=True) == ""
    assert client.get("/api/mariadb").status_code == 409


def test_mariadb_page_lists_databases_and_users(client, host, monkeypatch):
    _mariadb_running(host, monkeypatch, _MARIADB_LISTING)
    body = client.get("/c/mariadb").get_data(as_text=True)
    assert 'id="mariadb-database-form"' in body
    assert 'id="mariadb-user-form"' in body
    assert 'id="mariadb-password-form"' in body
    assert 'id="mariadb-delete-form"' in body
    assert 'id="mariadb-grants-form"' in body
    assert 'data-mariadb-open="database"' in body
    assert 'data-mariadb-open="user"' in body
    assert "data-mariadb-manage" in body
    assert 'data-mariadb-drop="user"' not in body
    assert "app@localhost" in body
    assert ">app<" in body
    assert body.index('id="mariadb-panel"') < body.index('id="service-log"')
    partial = client.get("/partials/mariadb").get_data(as_text=True)
    assert partial.strip() in body
    assert "phpmyadmin" not in partial
    assert 'data-name="mysql"' not in partial
    assert 'id="mariadb-panel"' not in client.get("/c/apache").get_data(as_text=True)

    listed = client.get("/api/mariadb").get_json()
    assert listed["databases"] == ["app"]
    assert listed["users"] == [{"name": "app", "host": "localhost", "databases": ["app"]}]


def test_mariadb_forms_stay_disabled_while_the_server_is_stopped(client, host, monkeypatch):
    from lamplight import mariadb

    host.apply("mariadb", "install")
    host.components["mariadb"]["active"] = False

    def fail(sql, *, secret=None):
        raise AssertionError("a stopped server is not queried")

    monkeypatch.setattr(mariadb, "execute", fail)
    body = client.get("/c/mariadb").get_data(as_text=True)
    assert "Start MariaDB" in body
    assert 'type="submit" disabled' in body
    assert client.post("/api/mariadb/databases", json={"name": "shop"}).status_code == 409


def test_creating_and_dropping_databases_and_users(client, host, monkeypatch):
    calls = _mariadb_running(host, monkeypatch, _MARIADB_LISTING)
    created = client.post("/api/mariadb/databases", json={"name": "shop"})
    assert created.status_code == 200
    assert calls[-1]["sql"] == "CREATE DATABASE `shop` CHARACTER SET utf8mb4;"
    assert calls[-1]["secret"] is None

    user = client.post(
        "/api/mariadb/users",
        json={"name": "shop", "host": "127.0.0.1", "password": "s3cret-phrase", "database": "app"},
    )
    assert user.status_code == 200
    assert "s3cret-phrase" not in user.get_data(as_text=True)
    assert calls[-1]["secret"] == "s3cret-phrase"
    assert "CREATE USER `shop`@`127.0.0.1` IDENTIFIED BY 's3cret-phrase'" in calls[-1]["sql"]
    assert "GRANT ALL PRIVILEGES ON `app`.*" in calls[-1]["sql"]

    password = client.post(
        "/api/mariadb/users/password",
        json={"name": "shop", "host": "localhost", "password": "next-phrase"},
    )
    assert password.status_code == 200
    assert "next-phrase" not in password.get_data(as_text=True)
    assert "ALTER USER `shop`@`localhost` IDENTIFIED BY 'next-phrase'" in calls[-1]["sql"]

    dropped_user = client.post(
        "/api/mariadb/users/drop", json={"name": "shop", "host": "localhost"}
    )
    assert dropped_user.status_code == 200
    assert calls[-1]["sql"] == "DROP USER `shop`@`localhost`;"

    dropped = client.post("/api/mariadb/databases/drop", json={"name": "shop"})
    assert dropped.status_code == 200
    assert calls[-1]["sql"] == "DROP DATABASE `shop`;"
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_setting_user_grants_reads_the_server_then_writes_the_difference(client, host, monkeypatch):
    listing = "\n".join(
        [
            "db\tapp",
            "db\tshop",
            "user\tapp\tlocalhost",
            "grant\tapp\tlocalhost\tapp",
        ]
    )
    calls = _mariadb_running(host, monkeypatch, listing)
    same = client.post(
        "/api/mariadb/users/grants",
        json={"name": "app", "host": "localhost", "databases": ["app"]},
    )
    assert same.status_code == 200
    assert len(calls) == 1
    assert "SCHEMATA" in calls[0]["sql"]

    changed = client.post(
        "/api/mariadb/users/grants",
        json={"name": "app", "host": "localhost", "databases": ["shop"]},
    )
    assert changed.status_code == 200
    assert "REVOKE ALL PRIVILEGES ON `app`.* FROM `app`@`localhost`" in calls[-1]["sql"]
    assert "GRANT ALL PRIVILEGES ON `shop`.* TO `app`@`localhost`" in calls[-1]["sql"]
    assert calls[-1]["secret"] is None

    missing = client.post(
        "/api/mariadb/users/grants",
        json={"name": "app", "host": "localhost", "databases": ["other"]},
    )
    assert missing.status_code == 400
    assert "not a database" in missing.get_json()["error"]

    unknown = client.post(
        "/api/mariadb/users/grants",
        json={"name": "nobody", "host": "localhost", "databases": []},
    )
    assert unknown.status_code == 400
    assert "no such user" in unknown.get_json()["error"]


def test_protected_names_and_a_bad_host_never_reach_the_client(client, host, monkeypatch):
    calls = _mariadb_running(host, monkeypatch)
    refused = [
        ("/api/mariadb/databases", {"name": "mysql"}),
        ("/api/mariadb/databases", {"name": "phpmyadmin"}),
        ("/api/mariadb/databases/drop", {"name": "information_schema"}),
        ("/api/mariadb/databases/drop", {"name": "phpmyadmin"}),
        ("/api/mariadb/users", {"name": "root", "password": "secret", "host": "localhost"}),
        ("/api/mariadb/users", {"name": "mysql", "password": "secret", "host": "localhost"}),
        ("/api/mariadb/users", {"name": "phpmyadmin", "password": "secret", "host": "localhost"}),
        ("/api/mariadb/users/drop", {"name": "mysql.sys", "host": "localhost"}),
        ("/api/mariadb/users/drop", {"name": "mysql", "host": "localhost"}),
        ("/api/mariadb/users/drop", {"name": "phpmyadmin", "host": "localhost"}),
        (
            "/api/mariadb/users/grants",
            {"name": "phpmyadmin", "host": "localhost", "databases": []},
        ),
        (
            "/api/mariadb/users/grants",
            {"name": "mysql", "host": "localhost", "databases": ["shop"]},
        ),
        (
            "/api/mariadb/users/password",
            {"name": "debian-sys-maint", "host": "localhost", "password": "x"},
        ),
        ("/api/mariadb/users", {"name": "shop", "password": "secret", "host": "%"}),
        ("/api/mariadb/databases", {"name": "shop`; DROP DATABASE mysql"}),
        ("/api/mariadb/users", ["nope"]),
    ]
    for path, payload in refused:
        response = client.post(path, json=payload)
        assert response.status_code == 400, path
    assert calls == []


def test_mariadb_changes_require_root(client, host, monkeypatch):
    from lamplight import systemops

    host.apply("mariadb", "install")
    monkeypatch.setattr(systemops, "is_root", lambda: False)
    response = client.post("/api/mariadb/databases", json={"name": "shop"})
    assert response.status_code == 403
    assert "must run as root" in response.get_json()["error"]


def test_mariadb_endpoints_are_behind_the_token(app):
    guest = app.test_client()
    assert guest.get("/api/mariadb").status_code == 401
    assert guest.post("/api/mariadb/databases", json={"name": "shop"}).status_code == 401
    assert guest.post("/api/mariadb/databases/drop", json={"name": "shop"}).status_code == 401
    assert guest.post("/api/mariadb/users", json={"name": "shop"}).status_code == 401
    assert guest.post("/api/mariadb/users/drop", json={"name": "shop"}).status_code == 401
    assert guest.post("/api/mariadb/users/password", json={"name": "shop"}).status_code == 401
    assert guest.post("/api/mariadb/users/grants", json={"name": "shop"}).status_code == 401
    assert guest.get("/partials/mariadb").status_code == 302
    assert guest.get("/partials/component/mariadb/log").status_code == 302


def test_job_ids_are_random_strings_not_a_counter(client):
    first = client.post("/api/components/apache/install").get_json()["job_id"]
    wait_for_job(client, first)
    second = client.post("/api/components/mariadb/install").get_json()["job_id"]
    wait_for_job(client, second)

    for job_id in (first, second):
        assert len(job_id) == 6
        assert set(job_id) <= set("123456789abcdefghijklmnpqrstuvwxyz")
    assert first != second
    assert {first, second} <= {job["id"] for job in client.get("/api/jobs").get_json()["jobs"]}
