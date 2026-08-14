"""Application configuration.

All values can be overridden via environment variables (see .env.example).
Sensible defaults let the app run with zero external dependencies, while a
production systemd install can point DATA_DIR / DATABASE_URL elsewhere.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Where mutable state lives (SQLite db, generated secret). Overridable so a
# systemd unit can keep data under e.g. /var/lib/boring-builder while the code
# lives under /opt.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "instance"))


def _secret_key() -> str:
    """Return a stable secret key.

    Priority: SECRET_KEY env var, then a persisted key file in DATA_DIR (so
    sessions survive restarts without hard-coding a secret), otherwise a fresh
    random key is generated and saved.
    """
    env = os.environ.get("SECRET_KEY")
    if env and env != "change-me-to-a-long-random-string":
        return env
    key_file = DATA_DIR / "secret_key"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            return key_file.read_text().strip()
        key = secrets.token_hex(32)
        key_file.write_text(key)
        os.chmod(key_file, 0o600)
        return key
    except OSError:
        # Fall back to an ephemeral key if the data dir is not writable.
        return env or secrets.token_hex(32)


class Config:
    SECRET_KEY = _secret_key()

    # Database. Defaults to a SQLite file inside DATA_DIR so state persists
    # across restarts. Point DATABASE_URL at MariaDB/Postgres if preferred,
    # e.g. mysql+pymysql://user:pass@host/dbname
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{DATA_DIR / 'ollama_manager.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    DATA_DIR = str(DATA_DIR)
    APP_ROOT = str(BASE_DIR)

    # Ollama daemon
    OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))

    # Sessions
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 days
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Distinct cookie name so this app and the chat app can both be signed in
    # at the same time on localhost. Browsers scope cookies by hostname, not
    # port, so without this the two apps would clobber each other's session.
    SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "boring_builder_session")

    # Bootstrap admin created on first launch if no users exist.
    BOOTSTRAP_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

    # Initial value for the "allow self-registration" runtime setting. After
    # first run this is managed from the admin Settings tab and stored in the
    # database.
    ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "false").lower() == "true"
