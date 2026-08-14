"""Application factory for Boring Builder — a standalone local AI project
generator. Reuses the same proven auth/user foundation as the chat app, but
ships only the builder features."""
import os
from pathlib import Path

from flask import Flask, redirect, url_for
from flask_login import current_user, logout_user

from .config import Config
from .extensions import db, login_manager


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if uri.startswith("sqlite:///"):
        os.makedirs(Path(uri.replace("sqlite:///", "")).parent, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # Blueprints — auth, the builder, and account/user management.
    from .auth.routes import auth_bp
    from .builder.routes import builder_bp
    from .accounts import accounts_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(builder_bp, url_prefix="/builder")
    app.register_blueprint(accounts_bp, url_prefix="/builder")

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("builder.index"))
        return redirect(url_for("auth.login"))

    @app.route("/api/status")
    def api_status():
        """Lightweight Ollama daemon health check for the UI status pill."""
        import os as _os
        import requests
        from flask import jsonify
        host = _os.environ.get("OLLAMA_HOST",
                               app.config.get("OLLAMA_HOST", "http://localhost:11434"))
        try:
            r = requests.get(host.rstrip("/") + "/api/tags", timeout=2)
            return jsonify({"online": r.status_code == 200})
        except Exception:
            return jsonify({"online": False})

    @app.route("/sw.js")
    def service_worker():
        from flask import send_from_directory
        resp = send_from_directory(app.static_folder, "sw.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(403)
    def _forbidden(e):
        from flask import render_template
        return render_template("error.html", code=403,
                               title="Not allowed",
                               message="You don't have permission to view this page. "
                                       "Ask an administrator if you need access."), 403

    @app.errorhandler(404)
    def _not_found(e):
        from flask import render_template
        return render_template("error.html", code=404,
                               title="Page not found",
                               message="That page doesn't exist."), 404

    @app.before_request
    def _enforce_block():
        if current_user.is_authenticated and not current_user.is_active:
            logout_user()

    @app.context_processor
    def _inject_branding():
        from . import settings as app_settings
        try:
            name = app_settings.get("app_name")
            tagline = app_settings.get("app_tagline")
        except Exception:
            name, tagline = "Boring Builder", "Local AI project builder"
        return {"app_name": name, "app_tagline": tagline}

    with app.app_context():
        db.create_all()
        _bootstrap_admin(app)

    return app


def _bootstrap_admin(app):
    from .models import User
    if User.query.first() is not None:
        return
    admin = User(
        username=app.config["BOOTSTRAP_ADMIN_USERNAME"],
        is_admin=True,
        is_active=True,
        can_manage_models=True,
    )
    admin.set_password(app.config["BOOTSTRAP_ADMIN_PASSWORD"])
    db.session.add(admin)
    db.session.commit()
    app.logger.info("Created bootstrap admin account '%s'.", admin.username)
