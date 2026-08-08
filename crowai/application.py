from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any, Mapping

from flask import Flask

from crowai.auth.routes import auth_bp
from crowai.config import PROJECT_ROOT, initial_instance_path, load_configuration, load_project_environment
from crowai.conversations.routes import conversation_api_bp
from crowai.error_handlers import register_error_handlers
from crowai.extensions import initialize_extensions
from crowai.logging_config import configure_logging
from crowai.request_context import register_request_hooks
from crowai.security import register_security_headers
from crowai.settings.routes import settings_api_bp
from crowai.system_routes import system_bp
from crowai.uploads.routes import upload_api_bp
from crowai.user_routes import user_api_bp
from crowai.workspace_routes import workspace_api_bp, workspace_bp


def register_blueprints(app: Flask) -> None:
    for blueprint in (
        auth_bp,
        workspace_api_bp,
        conversation_api_bp,
        upload_api_bp,
        settings_api_bp,
        user_api_bp,
        system_bp,
        workspace_bp,
    ):
        app.register_blueprint(blueprint)


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    load_project_environment(config)
    instance_path = initial_instance_path(config)
    app = Flask(
        "crowai",
        instance_path=str(instance_path),
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    load_configuration(app, config)
    configure_logging(app)
    runtime = initialize_extensions(app)
    register_blueprints(app)
    register_error_handlers(app)
    register_request_hooks(app)
    register_security_headers(app)

    def shutdown_models() -> None:
        runtime.registry.shutdown()

    atexit.register(shutdown_models)
    app.extensions["crowai_shutdown"] = shutdown_models
    return app
