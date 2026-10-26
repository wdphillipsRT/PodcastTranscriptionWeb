"""Import-safe Flask application construction."""

from collections.abc import Mapping

from flask import Flask, jsonify, render_template, request
from flask_login import current_user
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from config import load_settings
from extensions import csrf, db, limiter, login_manager, migrate


def create_app(config_override: Mapping | None = None) -> Flask:
    """Build an application without connecting services or creating its schema."""
    app = Flask(__name__)
    app.config.from_object(load_settings())
    if config_override is not None:
        app.config.update(config_override)

    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
    )

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    limiter.init_app(app)

    from admin_cli import admin_cli
    from classification_cli import classification_cli
    from knowledge_cli import knowledge_cli
    from system_checks import ReadinessCache, system_cli
    from web.auth import auth
    from web.routes import workspace

    # Authentication precedes CSRF so expired API sessions always receive 401.
    @app.before_request
    def private_api_authentication():
        if request.path.startswith("/api/") and request.endpoint and not current_user.is_authenticated:
            return jsonify(error="Authentication required"), 401

    csrf.init_app(app)

    app.cli.add_command(admin_cli)
    app.cli.add_command(classification_cli)
    app.cli.add_command(knowledge_cli)
    app.cli.add_command(system_cli)
    readiness = ReadinessCache()
    app.register_blueprint(auth)
    app.register_blueprint(workspace)

    @app.after_request
    def private_cache_control(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(HTTPException)
    def safe_http_error(error):
        messages = {400: "Request could not be accepted", 404: "Not found", 405: "Method not allowed",
                    413: "Request is too large", 429: "Too many requests"}
        message = messages.get(error.code, "Request failed")
        if request.path.startswith("/api/") or request.is_json:
            return jsonify(error=message), error.code
        return render_template("index.html", error=message), error.code

    @app.errorhandler(Exception)
    def safe_internal_error(_error):
        db.session.rollback()
        if request.path.startswith("/api/") or request.is_json:
            return jsonify(error="Request failed"), 500
        return render_template("index.html", error="Request failed"), 500

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        results = readiness.get(app.config)
        return results, 200 if all(value == "ok" for value in results.values()) else 503

    return app
