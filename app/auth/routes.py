"""Authentication routes: login, logout, optional self-registration."""
from datetime import datetime

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_user, logout_user, login_required, current_user

from ..extensions import db
from ..models import User
from .. import settings as app_settings

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("builder.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))

        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash("Incorrect username or password.", "error")
            return render_template("login.html", allow_registration=_reg_enabled())

        if not user.is_active:
            reason = (user.blocked_reason or "").strip()
            msg = "This account is blocked."
            if reason:
                msg += f" Reason: {reason}"
            flash(msg, "error")
            return render_template("login.html", allow_registration=_reg_enabled())

        user.last_login = datetime.utcnow()
        db.session.commit()

        login_user(user, remember=remember)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("builder.index"))

    return render_template("login.html", allow_registration=_reg_enabled())


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if not _reg_enabled():
        flash("Self-registration is disabled.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.", "error")
        else:
            user = User(username=username)
            user.set_password(password)
            user.last_login = datetime.utcnow()
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("builder.index"))

    return render_template("register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


def _reg_enabled() -> bool:
    return app_settings.get("allow_registration")
