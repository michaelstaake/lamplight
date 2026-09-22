"""The Flask app. Small on purpose: pages, a JSON API, and one SSE job stream."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import __version__, catalog, config, db, jobs, logsources, mariadb, php, status, systemops
from .firewall import FIREWALL_ACTIONS
from .installer import SERVICE_ACTIONS, Installer
from .jobs import JobRunner, JobStore

JobWork = Callable[[str, Installer], None]

ACTIONS = frozenset({"install", "remove", *SERVICE_ACTIONS, *FIREWALL_ACTIONS})
EDITABLE_SETTINGS = ("port", "document_root")
STREAM_POLL_SECONDS = 0.3
STREAM_MAX_SECONDS = 3600
MASKED_TOKEN = "•" * 12


def create_app(*, paths: config.AppPaths | None = None) -> Flask:
    paths = paths or config.AppPaths()
    paths.ensure()
    config.load_settings(paths)
    php.load_config(paths)
    token = config.load_or_create_token(paths)

    store = JobStore(db.connect(paths.db_path))
    runner = JobRunner()

    app = Flask(__name__)
    app.secret_key = token
    app.config.update(
        PATHS=paths,
        TOKEN=token,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        JSON_SORT_KEYS=False,
    )

    def current_settings() -> config.Settings:
        return config.load_settings(paths)

    def state() -> dict:
        """The host probe, computed at most once per request."""
        if not hasattr(g, "lamplight_state"):
            g.lamplight_state = status.dashboard(
                current_settings(), php_config=php.load_config(paths)
            )
        return g.lamplight_state

    def php_state() -> dict:
        return status.php_view(php.load_config(paths))

    def mariadb_view() -> dict | None:
        """Databases and accounts, once MariaDB is installed. None before that."""
        item = component_view("mariadb")
        if not item["installed"]:
            return None
        view = {
            "installed": True,
            "active": bool(item["active"]),
            "databases": [],
            "users": [],
            "error": None,
        }
        if not item["active"]:
            return view
        try:
            listed = mariadb.overview()
        except (mariadb.MariaDbError, PermissionError) as exc:
            return view | {"error": str(exc)}
        return view | listed

    def component_view(component_id: str) -> dict:
        for item in state()["components"]:
            if item["id"] == component_id:
                return item
        abort(404)

    # -- auth -------------------------------------------------------------

    def token_ok(candidate: str | None) -> bool:
        return bool(candidate) and secrets.compare_digest(candidate, token)

    def authenticated() -> bool:
        if token_ok(session.get("auth")):
            return True
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and token_ok(header[len("Bearer ") :]):
            return True
        if token_ok(request.args.get("token")):
            session["auth"] = token
            return True
        return False

    def require_auth(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if authenticated():
                return view(*args, **kwargs)
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.full_path))

        return wrapper

    @app.after_request
    def harden(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'",
        )
        return response

    @app.context_processor
    def template_globals():
        """The rail needs every component's status on every page."""
        nav = state()["components"] if authenticated() else []
        return {"app_version": __version__, "nav_components": nav}

    # -- pages ------------------------------------------------------------

    @app.get("/login")
    def login():
        if authenticated():
            return redirect(url_for("home"))
        return render_template("login.html", failed=bool(request.args.get("error")))

    @app.post("/login")
    def login_submit():
        if token_ok((request.form.get("token") or "").strip()):
            session["auth"] = token
            return redirect(url_for("home"))
        return redirect(url_for("login", error="1"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @require_auth
    def home():
        job = None
        job_id = request.args.get("job")
        if job_id:
            job = store.get(job_id)
            if job is None:
                abort(404)
        return render_template(
            "dashboard.html",
            data=state(),
            settings=current_settings(),
            jobs=store.list(),
            history=store.history,
            job=job,
        )

    def readable_logs(item: dict) -> bool:
        return bool(item["installed"] and logsources.has_own_logs(item["id"]))

    def service_log_context(item: dict) -> dict:
        if not readable_logs(item):
            return {}
        sources = logsources.sources_for(item["id"])
        source = logsources.find(item["id"], request.args.get("source"))
        log_text = logsources.read(source, logsources.DEFAULT_LINES) if source else None
        return {"sources": sources, "source": source, "log": log_text}

    @app.get("/c/<component_id>")
    @require_auth
    def component_page(component_id: str):
        item = component_view(component_id)
        return render_template(
            "component.html",
            item=item,
            php=php_state() if component_id == "php" else None,
            mariadb=mariadb_view() if component_id == "mariadb" else None,
            **service_log_context(item),
        )

    @app.get("/logs")
    @require_auth
    def logs_page():
        return redirect(url_for("home"))

    @app.get("/logs/job/<job_id>")
    @require_auth
    def logs_job_page(job_id: str):
        if store.get(job_id) is None:
            abort(404)
        return redirect(url_for("home", job=job_id))

    @app.get("/logs/<component_id>")
    @require_auth
    def logs_component_page(component_id: str):
        kwargs = {"component_id": component_id}
        if request.args.get("source"):
            kwargs["source"] = request.args["source"]
        return redirect(url_for("component_page", **kwargs))

    @app.get("/settings")
    @require_auth
    def settings_page():
        return render_template(
            "settings.html",
            settings=current_settings(),
            data=state(),
            token=MASKED_TOKEN,
            token_path=str(paths.auth_path),
            data_dir=str(paths.data_dir),
            db_path=str(paths.db_path),
        )

    # -- html partials, so card markup is never duplicated in JavaScript --

    @app.get("/partials/stack")
    @require_auth
    def stack_partial():
        return render_template("_stack.html", data=state())

    @app.get("/partials/component/<component_id>")
    @require_auth
    def component_partial(component_id: str):
        item = component_view(component_id)
        return render_template("_component.html", item=item)

    @app.get("/partials/component/<component_id>/log")
    @require_auth
    def component_log_partial(component_id: str):
        item = component_view(component_id)
        context = service_log_context(item)
        if not context:
            return ""
        return render_template("_service_log.html", item=item, **context)

    @app.get("/partials/php")
    @require_auth
    def php_partial():
        return render_template("_php.html", php=php_state())

    @app.get("/partials/mariadb")
    @require_auth
    def mariadb_partial():
        view = mariadb_view()
        if view is None:
            return ""
        return render_template("_mariadb.html", mariadb=view)

    # -- api --------------------------------------------------------------

    @app.get("/api/state")
    @require_auth
    def api_state():
        return jsonify(state())

    @app.get("/api/jobs")
    @require_auth
    def api_jobs():
        return jsonify({"jobs": store.list(), "busy": runner.busy, "current": runner.current_id})

    @app.get("/api/jobs/<job_id>")
    @require_auth
    def api_job(job_id: str):
        job = store.get(job_id)
        return jsonify(job) if job else (jsonify({"error": "no such job"}), 404)

    @app.get("/api/jobs/<job_id>/stream")
    @require_auth
    def api_job_stream(job_id: str):
        def events():
            sent = 0
            deadline = time.monotonic() + STREAM_MAX_SECONDS
            while time.monotonic() < deadline:
                job = store.get(job_id)
                if job is None:
                    yield "event: gone\ndata: {}\n\n"
                    return
                log = job["log"] or ""
                payload = {"status": job["status"]}
                if len(log) > sent:
                    payload["log"] = log[sent:]
                    sent = len(log)
                yield f"data: {json.dumps(payload)}\n\n"
                if job["status"] in jobs.TERMINAL_STATUSES:
                    return
                time.sleep(STREAM_POLL_SECONDS)

        return Response(
            events(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/logs/<component_id>")
    @require_auth
    def api_log(component_id: str):
        """The tail of one component log, for the Play/Pause follow on its page."""
        if catalog.get_component(component_id) is None:
            return jsonify({"error": f"unknown component: {component_id}"}), 404
        item = component_view(component_id)
        if not readable_logs(item):
            return jsonify({"error": f"{component_id} has no logs on this host"}), 404
        source = logsources.find(component_id, request.args.get("source"))
        if source is None:
            return jsonify({"error": f"{component_id} has no logs on this host"}), 404
        read = logsources.read(source, logsources.DEFAULT_LINES)
        return jsonify(logsources.as_dict(source) | {"text": read.text, "error": read.error})

    @app.post("/api/components/<component_id>/<action>")
    @require_auth
    def api_action(component_id: str, action: str):
        if action not in ACTIONS:
            return jsonify({"error": f"unknown action: {action}"}), 400
        if catalog.get_component(component_id) is None:
            return jsonify({"error": f"unknown component: {component_id}"}), 404
        return start_job(
            component_id,
            action,
            lambda job_id, installer: _perform(installer, job_id, component_id, action),
        )

    @app.get("/api/php")
    @require_auth
    def api_php():
        return jsonify(php_state())

    @app.post("/api/php/extensions")
    @require_auth
    def api_php_extensions():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("extensions"), list):
            return jsonify({"error": 'expected {"extensions": [...]}'}), 400
        desired, problems = php.clean_extensions(payload["extensions"])
        if problems:
            return jsonify({"error": "; ".join(problems)}), 400
        # Catch a typo here rather than three minutes into an apt job. A pinned
        # version apt has never heard of is not a typo — the job adds Surý's
        # repository first, then installs. Once that runtime is known, a missing
        # extension package is still refused.
        php_config = php.load_config(paths)
        runtime = php.runtime_packages(php_config.version)[0]
        runtime_known = (
            not php_config.version or systemops.apt_packages_exist([runtime]).get(runtime) is True
        )
        if runtime_known:
            known = systemops.apt_packages_exist(
                php.extension_package(name, php_config.version) for name in desired
            )
            unknown = [name for name, exists in known.items() if exists is False]
            if unknown:
                return jsonify({"error": "apt has no package " + ", ".join(unknown)}), 400

        def work(job_id: str, installer: Installer) -> None:
            installer.set_php_extensions(job_id, desired)

        return start_job("php", "extensions", work)

    @app.post("/api/php/version")
    @require_auth
    def api_php_version():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "version" not in payload:
            return jsonify({"error": 'expected {"version": "8.5"}'}), 400
        try:
            version = php.clean_version(payload["version"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        def work(job_id: str, installer: Installer) -> None:
            installer.set_php_version(job_id, version)

        return start_job("php", "version", work)

    @app.post("/api/php/options")
    @require_auth
    def api_php_options():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("options"), dict):
            return jsonify({"error": 'expected {"options": {...}}'}), 400
        options, problems = php.clean_options(payload["options"])
        if problems:
            return jsonify({"error": "; ".join(problems)}), 400

        def work(job_id: str, installer: Installer) -> None:
            installer.apply_php_options(job_id, options)

        return start_job("php", "options", work)

    @app.get("/api/mariadb")
    @require_auth
    def api_mariadb():
        view = mariadb_view()
        if view is None:
            return jsonify({"error": "MariaDB is not installed"}), 409
        return jsonify(view)

    @app.post("/api/mariadb/databases")
    @require_auth
    def api_mariadb_create_database():
        return _mariadb_action(lambda body: mariadb.create_database(body.get("name")))

    @app.post("/api/mariadb/databases/drop")
    @require_auth
    def api_mariadb_drop_database():
        return _mariadb_action(lambda body: mariadb.drop_database(body.get("name")))

    @app.post("/api/mariadb/users")
    @require_auth
    def api_mariadb_create_user():
        def work(body: dict) -> None:
            mariadb.create_user(
                body.get("name"), body.get("host"), body.get("password"), body.get("database")
            )

        return _mariadb_action(work)

    @app.post("/api/mariadb/users/password")
    @require_auth
    def api_mariadb_password():
        def work(body: dict) -> None:
            mariadb.set_password(body.get("name"), body.get("host"), body.get("password"))

        return _mariadb_action(work)

    @app.post("/api/mariadb/users/grants")
    @require_auth
    def api_mariadb_grants():
        def work(body: dict) -> None:
            mariadb.set_grants(body.get("name"), body.get("host"), body.get("databases"))

        return _mariadb_action(work)

    @app.post("/api/mariadb/users/drop")
    @require_auth
    def api_mariadb_drop_user():
        return _mariadb_action(lambda body: mariadb.drop_user(body.get("name"), body.get("host")))

    @app.post("/api/settings")
    @require_auth
    def api_settings():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        # The bind address is deliberately not editable here: changing it from a
        # browser is how a localhost panel accidentally becomes a public one.
        updates = {key: payload[key] for key in EDITABLE_SETTINGS if key in payload}
        merged = config.Settings.from_dict(json.loads(current_settings().to_json()) | updates)
        config.save_settings(paths, merged)
        return jsonify({"ok": True, "settings": json.loads(merged.to_json())})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "version": __version__})

    # -- internals --------------------------------------------------------

    def _mariadb_action(work: Callable[[dict], None]):
        """Run one database change in the request, so a password stays out of the job log."""
        item = component_view("mariadb")
        if not item["installed"]:
            return jsonify({"error": "MariaDB is not installed"}), 409
        if not item["active"]:
            return jsonify({"error": "MariaDB is not running"}), 409
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        try:
            work(payload)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except (ValueError, mariadb.MariaDbError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    def start_job(component_id: str, action: str, work: JobWork):
        """Queue one job in the single worker slot, or say why it cannot run."""
        if runner.busy:
            return jsonify({"error": "another job is already running"}), 409
        job_id = store.create(component_id, action)
        installer = Installer(paths, store)

        def run() -> None:
            store.mark_running(job_id)
            try:
                work(job_id, installer)
                store.finish(job_id, jobs.SUCCESS)
            except Exception as exc:  # noqa: BLE001 — this is the job boundary
                store.append_log(job_id, f"\nERROR: {exc}\n")
                store.finish(job_id, jobs.FAILED, str(exc))

        if not runner.submit(job_id, run):
            store.finish(job_id, jobs.FAILED, "another job is already running")
            return jsonify({"error": "another job is already running"}), 409
        return jsonify({"job_id": job_id, "status": jobs.QUEUED})

    def _perform(installer: Installer, job_id: str, component_id: str, action: str) -> None:
        if action == "install":
            installer.install(job_id, component_id)
        elif action == "remove":
            installer.remove(job_id, component_id)
        elif action in FIREWALL_ACTIONS:
            installer.firewall_action(job_id, component_id, action)
        else:
            installer.service_action(job_id, component_id, action)

    return app
