"""Import-safe Flask application construction."""

from collections.abc import Mapping

from flask import Flask
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
    csrf.init_app(app)
    limiter.init_app(app)

    from admin_cli import admin_cli
    from web.auth import auth

    app.cli.add_command(admin_cli)
    app.register_blueprint(auth)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
