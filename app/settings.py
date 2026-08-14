"""Persistent app settings for Boring Builder.

A tiny key->value store backed by the Setting table (same model the chat app
uses), with typed defaults. Kept deliberately small: the builder only needs a
handful of settings, all admin-editable from the Builder settings page.
"""
from .extensions import db
from .models import Setting

# key -> (default, type)
DEFAULTS = {
    "builder_model": ("", str),              # "" => BUILDER_MODEL env / auto
    "builder_output_dir": ("", str),         # "" => BUILDER_OUTPUT_DIR env / <DATA_DIR>/output
    "builder_num_ctx": (0, int),             # 0 => env / hardware-recommended
    "builder_temperature": ("", str),        # "" => env / 0.2
    "builder_default_mode": ("manifest", str),
    "builder_max_files": (0, int),
    "builder_keep_unzipped": (True, bool),
    "builder_auto_export": (True, bool),
    # branding
    "app_name": ("Boring Builder", str),
    "app_tagline": ("Local AI project builder", str),
    "allow_registration": (False, bool),
}


def _cast(raw, typ, default):
    if raw is None:
        return default
    try:
        if typ is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if typ is int:
            return int(raw)
        return str(raw)
    except (ValueError, TypeError):
        return default


def get(key):
    default, typ = DEFAULTS.get(key, ("", str))
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        return default
    return _cast(row.value, typ, default)


# Fixed application identity — not changeable at runtime.
_LOCKED = {"app_name", "app_tagline"}


def set_many(values: dict):
    for key, val in values.items():
        if key not in DEFAULTS or key in _LOCKED:
            continue
        if isinstance(val, bool):
            val = "true" if val else "false"
        row = Setting.query.filter_by(key=key).first()
        if row is None:
            row = Setting(key=key, value=str(val))
            db.session.add(row)
        else:
            row.value = str(val)
    db.session.commit()


def all_settings() -> dict:
    return {k: get(k) for k in DEFAULTS}
