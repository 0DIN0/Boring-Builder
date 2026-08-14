"""Account self-service and admin user management for Boring Builder."""
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models import User

accounts_bp = Blueprint("accounts", __name__)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# Self-service account
# --------------------------------------------------------------------------- #
@accounts_bp.route("/account")
@login_required
def account():
    return render_template("account.html", user=current_user)


@accounts_bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not current_user.check_password(current):
        flash("Your current password is incorrect.")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.")
    elif new != confirm:
        flash("New passwords do not match.")
    else:
        current_user.set_password(new)
        db.session.commit()
        flash("Password updated.")
    return redirect(url_for("accounts.account"))


# --------------------------------------------------------------------------- #
# Admin: users
# --------------------------------------------------------------------------- #
@accounts_bp.route("/users")
@login_required
@admin_required
def users():
    everyone = User.query.order_by(User.created_at.asc()).all()
    admin_count = sum(1 for u in everyone if u.is_admin)
    return render_template("users.html", user=current_user,
                           users=everyone, admin_count=admin_count)


@accounts_bp.route("/users/create", methods=["POST"])
@login_required
@admin_required
def users_create():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    is_admin = request.form.get("is_admin") == "on"

    if not username or len(password) < 6:
        flash("Enter a username and a password of at least 6 characters.")
    elif User.query.filter_by(username=username).first():
        flash(f"A user named '{username}' already exists.")
    else:
        u = User(username=username, is_admin=is_admin, is_active=True,
                 can_manage_models=is_admin)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"Created user '{username}'.")
    return redirect(url_for("accounts.users"))


@accounts_bp.route("/users/<int:uid>/update", methods=["POST"])
@login_required
@admin_required
def users_update(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    action = request.form.get("action")
    admin_count = User.query.filter_by(is_admin=True).count()

    if action == "block":
        if u.id == current_user.id:
            flash("You cannot block your own account.")
        elif u.is_admin and admin_count <= 1:
            flash("You cannot block the last administrator.")
        else:
            u.is_active = False
            db.session.commit()
            flash(f"Blocked '{u.username}'.")
    elif action == "unblock":
        u.is_active = True
        db.session.commit()
        flash(f"Unblocked '{u.username}'.")
    elif action == "make_admin":
        u.is_admin = True
        u.can_manage_models = True
        db.session.commit()
        flash(f"'{u.username}' is now an administrator.")
    elif action == "revoke_admin":
        if u.is_admin and admin_count <= 1:
            flash("You cannot remove the last administrator.")
        else:
            u.is_admin = False
            db.session.commit()
            flash(f"'{u.username}' is no longer an administrator.")
    elif action == "reset_password":
        new = request.form.get("new_password") or ""
        if len(new) < 6:
            flash("Password must be at least 6 characters.")
        else:
            u.set_password(new)
            db.session.commit()
            flash(f"Reset password for '{u.username}'.")
    return redirect(url_for("accounts.users"))


@accounts_bp.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def users_delete(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    if u.id == current_user.id:
        flash("You cannot delete your own account.")
    elif u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("You cannot delete the last administrator.")
    else:
        name = u.username
        db.session.delete(u)
        db.session.commit()
        flash(f"Deleted user '{name}'.")
    return redirect(url_for("accounts.users"))
