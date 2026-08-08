from __future__ import annotations

from dataclasses import dataclass

from flask import current_app

from crowai.auth.security import SlidingWindowLimiter
from crowai.auth.service import AuthService
from crowai.conversations.service import ConversationService
from crowai.models.service import ModelService
from crowai.settings.service import SettingsService
from crowai.storage.database import Database
from crowai.storage.idempotency import RequestLedgerRepository
from crowai.uploads.service import UploadService
from crowai.user_store import UserJSONStore
from crowai.users.service import UserSnapshotService
from models import ModelRegistry


@dataclass
class Runtime:
    database: Database
    registry: ModelRegistry
    user_store: UserJSONStore
    auth_service: AuthService
    model_service: ModelService
    upload_service: UploadService
    conversation_service: ConversationService
    settings_service: SettingsService
    snapshot_service: UserSnapshotService
    auth_limiter: SlidingWindowLimiter
    request_ledger: RequestLedgerRepository


def get_runtime() -> Runtime:
    return current_app.extensions["crowai"]
