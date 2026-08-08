from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from flask import Flask

from crowai.auth.repository import UserRepository
from crowai.auth.security import SlidingWindowLimiter
from crowai.auth.service import AuthService
from crowai.conversations.repository import ConversationRepository
from crowai.conversations.service import ConversationService
from crowai.models.service import ModelService
from crowai.runtime import Runtime
from crowai.settings.service import SettingsService
from crowai.storage.database import Database
from crowai.storage.idempotency import RequestLedgerRepository
from crowai.storage.sessions import SQLiteSessionInterface
from crowai.uploads.repository import UploadRepository
from crowai.uploads.service import UploadService
from crowai.user_store import UserJSONStore
from crowai.users.service import UserSnapshotService
from models import ModelRegistry
from models.runtime_state import configure_state_root


def initialize_extensions(app: Flask) -> Runtime:
    strict_permissions = bool(app.config.get("PRIVATE_PERMISSIONS_STRICT", False))
    database = Database(
        Path(app.config["DATABASE_PATH"]),
        private_root=Path(app.config["INSTANCE_DIR"]),
        strict_permissions=strict_permissions,
    )
    configure_state_root(Path(app.config["MODEL_STATE_DIR"]))
    registry = ModelRegistry(
        Path(app.config["MODELS_DIR"]),
        development=bool(app.config["MODEL_DEVELOPMENT_RELOAD"]),
        strict_capabilities=bool(app.config["STRICT_MODEL_CAPABILITIES"]),
    )
    user_store = UserJSONStore(
        Path(app.config["USERS_DIR"]),
        database,
        strict_permissions=strict_permissions,
    )
    model_service = ModelService(registry, enable_web_search=bool(app.config["ENABLE_WEB_SEARCH"]))
    upload_service = UploadService(
        UploadRepository(database),
        model_service,
        root=Path(app.config["UPLOAD_DIR"]),
        maximum_bytes=int(app.config["MAX_UPLOAD_BYTES"]),
        maximum_files=int(app.config["MAX_UPLOAD_FILES"]),
        strict_permissions=strict_permissions,
    )
    runtime = Runtime(
        database=database,
        registry=registry,
        user_store=user_store,
        auth_service=AuthService(UserRepository(database)),
        model_service=model_service,
        upload_service=upload_service,
        conversation_service=ConversationService(ConversationRepository(database), upload_service, model_service),
        settings_service=SettingsService(database, model_service),
        snapshot_service=UserSnapshotService(user_store, database),
        auth_limiter=SlidingWindowLimiter(),
        request_ledger=RequestLedgerRepository(database),
    )
    app.extensions["crowai"] = runtime
    app.session_interface = SQLiteSessionInterface(database, guest_lifetime=timedelta(hours=24))
    return runtime
