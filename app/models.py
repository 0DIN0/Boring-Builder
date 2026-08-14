"""Database models."""
import json
from datetime import datetime, date

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    # is_active doubles as the "not blocked" flag. False == blocked.
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    blocked_reason = db.Column(db.Text, default="")

    # Whether the user is allowed to pull/delete/modify models.
    can_manage_models = db.Column(db.Boolean, default=False, nullable=False)

    # JSON list of model names this user may use. An empty list means
    # "no restriction — every model is allowed".
    _allowed_models = db.Column("allowed_models", db.Text, default="[]")

    # 0 means unlimited. Only enforced when > 0.
    daily_message_limit = db.Column(db.Integer, default=0, nullable=False)

    notes = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    conversations = db.relationship(
        "Conversation", backref="user", cascade="all, delete-orphan", lazy=True
    )
    preferences = db.relationship(
        "UserPreference",
        backref="user",
        cascade="all, delete-orphan",
        uselist=False,
        lazy=True,
    )

    # --- password helpers ---
    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # --- allowed models (stored as JSON text) ---
    @property
    def allowed_models(self):
        try:
            return json.loads(self._allowed_models or "[]")
        except (ValueError, TypeError):
            return []

    @allowed_models.setter
    def allowed_models(self, value):
        self._allowed_models = json.dumps(value or [])

    def can_use_model(self, model_name: str) -> bool:
        # Admins are never restricted.
        if self.is_admin:
            return True
        allowed = self.allowed_models
        if not allowed:  # empty == no restriction
            return True
        return model_name in allowed

    def messages_today(self) -> int:
        start = datetime.combine(date.today(), datetime.min.time())
        return (
            Message.query.join(Conversation)
            .filter(
                Conversation.user_id == self.id,
                Message.role == "user",
                Message.created_at >= start,
            )
            .count()
        )

    def effective_daily_limit(self) -> int:
        """Per-user limit, falling back to the global default when unset."""
        if self.daily_message_limit and self.daily_message_limit > 0:
            return self.daily_message_limit
        try:
            return int(Setting.get("default_daily_limit", "0") or 0)
        except (TypeError, ValueError):
            return 0

    def has_reached_daily_limit(self) -> bool:
        if self.is_admin:
            return False
        limit = self.effective_daily_limit()
        if limit <= 0:
            return False
        return self.messages_today() >= limit

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "blocked_reason": self.blocked_reason or "",
            "can_manage_models": self.can_manage_models,
            "allowed_models": self.allowed_models,
            "daily_message_limit": self.daily_message_limit,
            "effective_daily_limit": self.effective_daily_limit(),
            "messages_today": self.messages_today(),
            "notes": self.notes or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    title = db.Column(db.String(255), default="New chat")
    model = db.Column(db.String(120))
    system_prompt = db.Column(db.Text, default="")

    # Generation parameters. NULL means "use the model / Ollama default", i.e.
    # the model runs as-is unless the user deliberately overrides a value.
    temperature = db.Column(db.Float)
    top_p = db.Column(db.Float)
    top_k = db.Column(db.Integer)
    num_ctx = db.Column(db.Integer)
    repeat_penalty = db.Column(db.Float)
    seed = db.Column(db.Integer)

    pinned = db.Column(db.Boolean, default=False, nullable=False)

    # Optional knowledge base attached for retrieval-augmented answers.
    collection_id = db.Column(db.Integer, db.ForeignKey("collections.id"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    messages = db.relationship(
        "Message",
        backref="conversation",
        cascade="all, delete-orphan",
        lazy=True,
        order_by="Message.created_at",
    )

    def options(self) -> dict:
        """Assemble the Ollama options dict, omitting anything unset."""
        opts = {}
        if self.temperature is not None:
            opts["temperature"] = self.temperature
        if self.top_p is not None:
            opts["top_p"] = self.top_p
        if self.top_k is not None:
            opts["top_k"] = self.top_k
        if self.num_ctx is not None:
            opts["num_ctx"] = self.num_ctx
        if self.repeat_penalty is not None:
            opts["repeat_penalty"] = self.repeat_penalty
        if self.seed is not None:
            opts["seed"] = self.seed
        return opts

    def to_dict(self, include_messages=False):
        data = {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "system_prompt": self.system_prompt or "",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "num_ctx": self.num_ctx,
            "repeat_penalty": self.repeat_penalty,
            "seed": self.seed,
            "pinned": self.pinned,
            "collection_id": self.collection_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
        return data


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)  # user | assistant | system
    content = db.Column(db.Text, nullable=False)
    model = db.Column(db.String(120))
    citations = db.Column(db.Text)  # JSON list of {n, filename, score}
    meta = db.Column(db.Text)       # JSON: {eval_count, tokens_per_second}
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json as _json
        def _load(v):
            if not v:
                return None
            try:
                return _json.loads(v)
            except (ValueError, TypeError):
                return None
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "model": self.model,
            "citations": _load(self.citations),
            "meta": _load(self.meta),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ModelConfig(db.Model):
    """App-level configuration layered on top of an installed Ollama model.

    Lets an admin give a model a friendly display name, a default system
    prompt and default generation parameters, or hide it from users, without
    touching the underlying model on disk.
    """

    __tablename__ = "model_configs"

    name = db.Column(db.String(160), primary_key=True)  # the Ollama model tag
    display_name = db.Column(db.String(160), default="")
    description = db.Column(db.Text, default="")
    system_prompt = db.Column(db.Text, default="")

    temperature = db.Column(db.Float)
    top_p = db.Column(db.Float)
    top_k = db.Column(db.Integer)
    num_ctx = db.Column(db.Integer)
    repeat_penalty = db.Column(db.Float)

    is_hidden = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self):
        return {
            "name": self.name,
            "display_name": self.display_name or "",
            "description": self.description or "",
            "system_prompt": self.system_prompt or "",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "num_ctx": self.num_ctx,
            "repeat_penalty": self.repeat_penalty,
            "is_hidden": self.is_hidden,
        }


class Collection(db.Model):
    """A knowledge base: a named group of documents the user can attach to a
    conversation for retrieval-augmented answers."""

    __tablename__ = "collections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    documents = db.relationship(
        "Document", backref="collection", cascade="all, delete-orphan", lazy=True
    )

    def to_dict(self, with_docs=False):
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "document_count": len(self.documents),
            "chunk_count": sum(d.chunk_count for d in self.documents),
        }
        if with_docs:
            data["documents"] = [d.to_dict() for d in self.documents]
        return data


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    collection_id = db.Column(
        db.Integer, db.ForeignKey("collections.id"), nullable=False, index=True
    )
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(80))
    size = db.Column(db.Integer, default=0)
    chunk_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chunks = db.relationship(
        "Chunk", backref="document", cascade="all, delete-orphan", lazy=True
    )

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "chunk_count": self.chunk_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Chunk(db.Model):
    __tablename__ = "chunks"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True
    )
    collection_id = db.Column(db.Integer, index=True)
    ordinal = db.Column(db.Integer, default=0)
    content = db.Column(db.Text, nullable=False)
    # JSON-encoded list of floats when an embedding model was available.
    embedding = db.Column(db.Text)


class PromptPreset(db.Model):
    """A reusable, admin-managed system prompt users can apply to a chat."""

    __tablename__ = "prompt_presets"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "content": self.content}


class UserPreference(db.Model):
    """Per-user interface preferences (theme, density, shortcuts, …).

    Stored as a single JSON blob so new options can be added without a
    migration. Preferences follow the user across devices; the browser also
    keeps a local copy so the interface can paint correctly before the first
    network round trip.
    """

    __tablename__ = "user_prefs"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    data = db.Column(db.Text, default="{}")
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    @staticmethod
    def load(user_id) -> dict:
        row = db.session.get(UserPreference, user_id)
        if row is None:
            return {}
        try:
            value = json.loads(row.data or "{}")
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def merge(user_id, patch: dict) -> dict:
        """Shallow-merge `patch` into the stored preferences and return them."""
        row = db.session.get(UserPreference, user_id)
        if row is None:
            row = UserPreference(user_id=user_id, data="{}")
            db.session.add(row)
        current = UserPreference.load(user_id)
        for key, value in (patch or {}).items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        row.data = json.dumps(current)
        db.session.commit()
        return current

    @staticmethod
    def clear(user_id) -> dict:
        row = db.session.get(UserPreference, user_id)
        if row is not None:
            db.session.delete(row)
            db.session.commit()
        return {}


class Setting(db.Model):
    """Simple global key/value settings store."""

    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row is not None else default

    @staticmethod
    def set(key, value):
        row = db.session.get(Setting, key)
        if row:
            row.value = value
        else:
            row = Setting(key=key, value=value)
            db.session.add(row)
        db.session.commit()
