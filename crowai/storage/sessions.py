from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

from crowai.storage.database import Database, utcnow

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_ALLOWED_KEYS = {"user_id", "csrf_token", "guest_token", "guest_used", "_permanent"}


class ServerSideSession(CallbackDict, SessionMixin):
    def __init__(self, initial: dict[str, Any] | None = None, *, sid: str | None = None, new: bool = False) -> None:
        def on_update(_: CallbackDict) -> None:
            self.modified = True

        super().__init__(initial, on_update)
        self.sid = sid or secrets.token_urlsafe(32)
        self.previous_sid: str | None = None
        self.new = new
        self.modified = False

    def rotate_id(self) -> None:
        if not self.previous_sid:
            self.previous_sid = self.sid
        self.sid = secrets.token_urlsafe(32)
        self.modified = True


class SQLiteSessionInterface(SessionInterface):
    """SQLite-backed session storage with an opaque random cookie identifier."""

    session_class = ServerSideSession

    def __init__(self, database: Database, *, guest_lifetime: timedelta = timedelta(hours=24)) -> None:
        self.database = database
        self.guest_lifetime = guest_lifetime

    @staticmethod
    def _expired(value: str) -> bool:
        try:
            expires = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    def open_session(self, app, request):  # type: ignore[override]
        cookie_name = str(app.config.get("SESSION_COOKIE_NAME", "session"))
        sid = str(request.cookies.get(cookie_name) or "")
        if not _SESSION_ID.fullmatch(sid):
            return self.session_class(new=True)
        row = self.database.one("SELECT data_json,expires_at FROM sessions WHERE id=?", (sid,))
        if not row or self._expired(str(row["expires_at"])):
            if row:
                self.database.execute("DELETE FROM sessions WHERE id=?", (sid,))
            return self.session_class(new=True)
        try:
            raw = json.loads(row["data_json"])
        except (TypeError, ValueError):
            raw = {}
        data = {key: value for key, value in raw.items() if key in _ALLOWED_KEYS} if isinstance(raw, dict) else {}
        return self.session_class(data, sid=sid, new=False)

    def save_session(self, app, session, response):  # type: ignore[override]
        if not isinstance(session, self.session_class):
            return
        cookie_name = str(app.config.get("SESSION_COOKIE_NAME", "session"))
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        secure = self.get_cookie_secure(app)
        httponly = self.get_cookie_httponly(app)
        samesite = self.get_cookie_samesite(app)

        if session.previous_sid:
            self.database.execute("DELETE FROM sessions WHERE id=?", (session.previous_sid,))
            session.previous_sid = None

        if not session:
            self.database.execute("DELETE FROM sessions WHERE id=?", (session.sid,))
            response.delete_cookie(cookie_name, domain=domain, path=path, secure=secure, httponly=httponly, samesite=samesite)
            return

        cookie_expiration = self.get_expiration_time(app, session)
        database_expiration = cookie_expiration or (datetime.now(timezone.utc) + self.guest_lifetime)
        now = utcnow()
        data = {key: value for key, value in dict(session).items() if key in _ALLOWED_KEYS}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(id,data_json,expires_at,created_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (session.sid, payload, database_expiration.isoformat(), now, now),
            )
            connection.execute("DELETE FROM sessions WHERE expires_at<?", (datetime.now(timezone.utc).isoformat(),))

        response.set_cookie(
            cookie_name,
            session.sid,
            expires=cookie_expiration,
            httponly=httponly,
            secure=secure,
            samesite=samesite,
            domain=domain,
            path=path,
        )
