from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from crowai.conversations.memory import bounded_mode_state, build_summary, merge_facts
from crowai.storage.database import Database, utcnow


class ConversationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._ledger_operations = 0

    def _maybe_cleanup_ledger(self) -> None:
        self._ledger_operations += 1
        if self._ledger_operations % 64:
            return
        now = datetime.now(timezone.utc)
        retention = (now - timedelta(days=7)).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM request_ledger WHERE (state='complete' AND updated_at<?) OR "
                "(state='processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<?)",
                (retention, now.isoformat()),
            )

    def list_for_owner(self, owner_key: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.all(
            "SELECT id,title,model_id,created_at,updated_at FROM conversations WHERE owner_key=? ORDER BY updated_at DESC LIMIT ?",
            (owner_key, limit),
        )

    def get_for_owner(self, conversation_id: str, owner_key: str) -> dict[str, Any] | None:
        return self.database.one("SELECT * FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key))

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.database.all(
            "SELECT id,role,content,payload_json,created_at FROM messages WHERE conversation_id=? ORDER BY id",
            (conversation_id,),
        )
        for item in rows:
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (TypeError, ValueError):
                item["payload"] = {}
        return rows

    def recent_history(self, conversation_id: str, *, limit: int = 20) -> list[dict[str, str]]:
        rows = self.database.all(
            "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        )
        return list(reversed(rows))

    def memory_snapshot(self, conversation_id: str, owner_key: str, *, recent_limit: int = 20) -> dict[str, Any]:
        owned = self.database.one("SELECT id FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key))
        if not owned:
            return {"conversation_id": conversation_id, "recent_messages": [], "summary": "", "relevant_facts": [], "mode_state": {}}
        row = self.database.one(
            "SELECT summary,facts_json,mode_state_json,turn_count FROM conversation_memory WHERE conversation_id=?",
            (conversation_id,),
        ) or {}
        try:
            facts = json.loads(row.get("facts_json") or "[]")
        except (TypeError, ValueError):
            facts = []
        try:
            mode_state = json.loads(row.get("mode_state_json") or "{}")
        except (TypeError, ValueError):
            mode_state = {}
        return {
            "conversation_id": conversation_id,
            "recent_messages": self.recent_history(conversation_id, limit=recent_limit),
            "summary": str(row.get("summary") or "")[:6000],
            "relevant_facts": facts[-24:] if isinstance(facts, list) else [],
            "mode_state": mode_state if isinstance(mode_state, dict) else {},
            "turn_count": int(row.get("turn_count") or 0),
        }

    def update_memory(
        self,
        *,
        conversation_id: str,
        owner_key: str,
        user_message_id: int,
        question: str,
        result: dict[str, Any],
        memory_update: dict[str, Any] | None = None,
        recent_limit: int = 20,
    ) -> None:
        owned = self.database.one("SELECT id FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key))
        if not owned:
            return
        existing = self.database.one(
            "SELECT summary,facts_json,mode_state_json,turn_count FROM conversation_memory WHERE conversation_id=?",
            (conversation_id,),
        ) or {}
        try:
            facts = json.loads(existing.get("facts_json") or "[]")
        except (TypeError, ValueError):
            facts = []
        try:
            mode_state = json.loads(existing.get("mode_state_json") or "{}")
        except (TypeError, ValueError):
            mode_state = {}
        facts = merge_facts(facts if isinstance(facts, list) else [], question, source_message_id=user_message_id)
        if not isinstance(memory_update, dict):
            memory_update = result.get("memory_update") if isinstance(result.get("memory_update"), dict) else {}
        package_state = memory_update.get("mode_state") if isinstance(memory_update.get("mode_state"), dict) else {}
        mode_state = bounded_mode_state(mode_state, package_state)
        # A bounded query is sufficient for prompt memory and keeps the earliest context durable.
        first = self.database.all(
            "SELECT id,role,content FROM messages WHERE conversation_id=? ORDER BY id ASC LIMIT 40",
            (conversation_id,),
        )
        last = self.database.all(
            "SELECT id,role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 120",
            (conversation_id,),
        )
        combined = {int(row["id"]): row for row in [*first, *last]}
        rows = [combined[key] for key in sorted(combined)]
        summary = build_summary(rows, recent_limit=recent_limit) or str(existing.get("summary") or "")[:6000]
        explicit_summary = str(memory_update.get("summary") or "").strip()
        if explicit_summary:
            summary = explicit_summary[:6000]
        now = utcnow()
        turn_count = int(existing.get("turn_count") or 0) + 1
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO conversation_memory(conversation_id,schema_version,summary,facts_json,mode_state_json,turn_count,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET summary=excluded.summary,"
                "facts_json=excluded.facts_json,mode_state_json=excluded.mode_state_json,turn_count=excluded.turn_count,updated_at=excluded.updated_at",
                (conversation_id, 1, summary, json.dumps(facts, ensure_ascii=False), json.dumps(mode_state, ensure_ascii=False), turn_count, now),
            )

    def create(self, *, conversation_id: str, owner_key: str, model_id: str, request_key: str) -> tuple[str, bool]:
        now = utcnow()
        with self.database.transaction() as connection:
            if request_key:
                existing = connection.execute(
                    "SELECT cc.conversation_id FROM conversation_creations cc "
                    "JOIN conversations c ON c.id=cc.conversation_id "
                    "WHERE cc.owner_key=? AND cc.request_key=?",
                    (owner_key, request_key),
                ).fetchone()
                if existing:
                    return str(existing["conversation_id"]), True
            connection.execute(
                "INSERT INTO conversations(id,owner_key,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (conversation_id, owner_key, "New conversation", model_id, now, now),
            )
            if request_key:
                connection.execute(
                    "INSERT INTO conversation_creations(owner_key,request_key,conversation_id,created_at) VALUES(?,?,?,?)",
                    (owner_key, request_key, conversation_id, now),
                )
        return conversation_id, False

    def rename(self, conversation_id: str, owner_key: str, title: str) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key)
            ).fetchone()
            if not row:
                return False
            connection.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title, utcnow(), conversation_id))
        return True

    def add_user_message(self, *, conversation_id: str, content: str, attachment_ids: tuple[str, ...]) -> int:
        with self.database.transaction() as connection:
            payload = {"attachments": [{"id": value} for value in attachment_ids]}
            cursor = connection.execute(
                "INSERT INTO messages(conversation_id,role,content,payload_json,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, "user", content, json.dumps(payload), utcnow()),
            )
            message_id = int(cursor.lastrowid)
            for upload_id in attachment_ids:
                connection.execute(
                    "INSERT OR IGNORE INTO message_uploads(message_id,upload_id) VALUES(?,?)", (message_id, upload_id)
                )
        return message_id

    def add_assistant_message(self, *, conversation_id: str, answer: str, result: dict[str, Any], title_source: str) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(conversation_id,role,content,payload_json,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, "assistant", answer, json.dumps(result, ensure_ascii=False), utcnow()),
            )
            title = title_source[:58] + ("…" if len(title_source) > 58 else "")
            if not title:
                title = "Attachment conversation"
            connection.execute(
                "UPDATE conversations SET title=CASE WHEN title='New conversation' THEN ? ELSE title END,updated_at=? WHERE id=?",
                (title, utcnow(), conversation_id),
            )
        return int(cursor.lastrowid)

    def delete(self, conversation_id: str, owner_key: str) -> tuple[bool, list[str]]:
        stored_paths: list[str] = []
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key)
            ).fetchone()
            if not row:
                return False, []
            uploads = connection.execute(
                "SELECT DISTINCT u.id,u.stored_path FROM uploads u "
                "JOIN message_uploads mu ON mu.upload_id=u.id "
                "JOIN messages m ON m.id=mu.message_id "
                "WHERE m.conversation_id=? AND u.owner_key=? AND NOT EXISTS ("
                "SELECT 1 FROM message_uploads mu2 JOIN messages m2 ON m2.id=mu2.message_id "
                "WHERE mu2.upload_id=u.id AND m2.conversation_id<>?"
                ")",
                (conversation_id, owner_key, conversation_id),
            ).fetchall()
            upload_ids = [str(item["id"]) for item in uploads]
            stored_paths = [str(item["stored_path"]) for item in uploads]
            if upload_ids:
                placeholders = ",".join("?" for _ in upload_ids)
                connection.execute(f"DELETE FROM uploads WHERE id IN ({placeholders}) AND owner_key=?", (*upload_ids, owner_key))
            connection.execute("DELETE FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key))
        return True, stored_paths

    def ledger_response(self, *, owner_key: str, request_key: str, operation: str) -> dict[str, Any] | None:
        if not request_key:
            return None
        row = self.database.one(
            "SELECT state,response_json FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=?",
            (owner_key, request_key, operation),
        )
        if not row or row["state"] != "complete" or not row["response_json"]:
            return None
        try:
            value = json.loads(row["response_json"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _lease_is_stale(value: Any, *, now: datetime | None = None) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return True
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= (now or datetime.now(timezone.utc))

    def processing_operation(self, *, owner_key: str, operation: str) -> dict[str, Any]:
        """Report whether one non-stale turn for this owner/operation is active."""
        now = datetime.now(timezone.utc)
        row = self.database.one(
            "SELECT request_key,created_at,updated_at,lease_expires_at FROM request_ledger "
            "WHERE owner_key=? AND operation=? AND state='processing' ORDER BY updated_at DESC LIMIT 1",
            (owner_key, operation),
        )
        if not row or self._lease_is_stale(row.get("lease_expires_at"), now=now):
            return {"active": False}
        return {
            "active": True,
            "started_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "lease_expires_at": str(row.get("lease_expires_at") or ""),
        }

    def claim_ledger(self, *, owner_key: str, request_key: str, operation: str, lease_seconds: int = 300) -> bool:
        if not request_key:
            return True
        self._maybe_cleanup_ledger()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease = (now_dt + timedelta(seconds=max(30, int(lease_seconds)))).isoformat()
        with self.database.transaction() as connection:
            # One conversation turn at a time, even when a refreshed/new browser
            # generates a different idempotency key. BEGIN IMMEDIATE makes this
            # operation-wide claim atomic across concurrent request threads.
            connection.execute(
                "DELETE FROM request_ledger WHERE owner_key=? AND operation=? AND state='processing' "
                "AND (lease_expires_at IS NULL OR lease_expires_at='' OR lease_expires_at<=?)",
                (owner_key, operation, now),
            )
            active = connection.execute(
                "SELECT request_key FROM request_ledger WHERE owner_key=? AND operation=? AND state='processing' LIMIT 1",
                (owner_key, operation),
            ).fetchone()
            if active is not None:
                return False

            row = connection.execute(
                "SELECT state FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=?",
                (owner_key, request_key, operation),
            ).fetchone()
            if row is not None and str(row["state"] or "") == "complete":
                return False
            if row is not None:
                connection.execute(
                    "UPDATE request_ledger SET state='processing',response_json=NULL,updated_at=?,lease_expires_at=? "
                    "WHERE owner_key=? AND request_key=? AND operation=?",
                    (now, lease, owner_key, request_key, operation),
                )
            else:
                connection.execute(
                    "INSERT INTO request_ledger(owner_key,request_key,operation,state,response_json,created_at,updated_at,lease_expires_at) VALUES(?,?,?,?,?,?,?,?)",
                    (owner_key, request_key, operation, "processing", None, now, now, lease),
                )
        return True

    def renew_ledger(
        self,
        *,
        owner_key: str,
        request_key: str,
        operation: str,
        lease_seconds: int = 600,
    ) -> bool:
        """Extend one live processing lease while a model turn is still running."""
        if not request_key:
            return False
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease = (now_dt + timedelta(seconds=max(30, int(lease_seconds)))).isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE request_ledger SET updated_at=?,lease_expires_at=? "
                "WHERE owner_key=? AND request_key=? AND operation=? AND state='processing'",
                (now, lease, owner_key, request_key, operation),
            )
            return int(cursor.rowcount or 0) > 0

    def complete_ledger(self, *, owner_key: str, request_key: str, operation: str, response: dict[str, Any]) -> None:
        if not request_key:
            return
        self.database.execute(
            "UPDATE request_ledger SET state='complete',response_json=?,updated_at=?,lease_expires_at=NULL "
            "WHERE owner_key=? AND request_key=? AND operation=? AND state='processing'",
            (json.dumps(response, ensure_ascii=False), utcnow(), owner_key, request_key, operation),
        )

    def release_ledger(self, *, owner_key: str, request_key: str, operation: str) -> None:
        if request_key:
            self.database.execute(
                "DELETE FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=? AND state='processing'",
                (owner_key, request_key, operation),
            )

    def release_operation(self, *, owner_key: str, operation: str) -> None:
        """Release any active request for a deleted conversation."""
        self.database.execute(
            "DELETE FROM request_ledger WHERE owner_key=? AND operation=? AND state='processing'",
            (owner_key, operation),
        )
