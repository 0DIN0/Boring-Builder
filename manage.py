"""Management CLI.

Usage:
  python manage.py create-admin <username> <password>
  python manage.py set-password <username> <password>
  python manage.py block <username> [reason...]
  python manage.py unblock <username>
  python manage.py list-users
"""
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app
from app.extensions import db
from app.models import User

app = create_app()


def create_admin(username, password):
    with app.app_context():
        if User.query.filter_by(username=username).first():
            print(f"User '{username}' already exists. Use set-password instead.")
            return
        u = User(username=username, is_admin=True, is_active=True, can_manage_models=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        print(f"Created admin '{username}'.")


def set_password(username, password):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if not u:
            print(f"No user named '{username}'.")
            return
        u.set_password(password)
        db.session.commit()
        print(f"Password updated for '{username}'.")


def block(username, reason=""):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if not u:
            print(f"No user named '{username}'.")
            return
        u.is_active = False
        u.blocked_reason = reason
        db.session.commit()
        print(f"Blocked '{username}'." + (f" Reason: {reason}" if reason else ""))


def unblock(username):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if not u:
            print(f"No user named '{username}'.")
            return
        u.is_active = True
        u.blocked_reason = ""
        db.session.commit()
        print(f"Unblocked '{username}'.")


def list_users():
    with app.app_context():
        for u in User.query.order_by(User.id).all():
            role = "admin" if u.is_admin else "user"
            st = "active" if u.is_active else "blocked"
            print(f"  #{u.id:<3} {u.username:<20} {role:<6} {st}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    cmd = args[0]
    if cmd == "create-admin" and len(args) == 3:
        create_admin(args[1], args[2])
    elif cmd == "set-password" and len(args) == 3:
        set_password(args[1], args[2])
    elif cmd == "block" and len(args) >= 2:
        block(args[1], " ".join(args[2:]))
    elif cmd == "unblock" and len(args) == 2:
        unblock(args[1])
    elif cmd == "list-users":
        list_users()
    else:
        print(__doc__); sys.exit(1)
