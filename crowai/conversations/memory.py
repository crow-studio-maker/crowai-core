from __future__ import annotations

import json
import re
from typing import Any

from crowai.storage.database import utcnow

MAX_SUMMARY_CHARS = 6000
MAX_FACTS = 24
MAX_FACT_CHARS = 500
MAX_MODE_STATE_CHARS = 6000

_FACT_PATTERNS = (
    re.compile(r"\bmy\s+([a-z][a-z0-9 _-]{1,40})\s+(?:is|are)\s+(.{1,220})", re.I),
    re.compile(r"\bi\s+(?:use|prefer|work with)\s+(.{2,220})", re.I),
    re.compile(r"\b(adım|ismim)\s+(.{1,100})", re.I),
    re.compile(r"\b(.{2,60})\s+kullanıyorum\b", re.I),
    re.compile(r"\btercihim\s+(.{2,180})", re.I),
)
_CORRECTION = re.compile(r"\b(?:actually|correction|instead|no,|hayır|düzeltme|aslında)\b", re.I)


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return fallback
    return value


def extract_facts(question: str, *, source_message_id: int | None = None) -> list[dict[str, Any]]:
    text = _clip(question, 3000)
    output: list[dict[str, Any]] = []
    for pattern in _FACT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = [item.strip(" .,:;!?\"'") for item in match.groups() if item]
        if len(groups) >= 2:
            key = _clip(groups[0], 80).casefold()
            value = _clip(groups[1], MAX_FACT_CHARS)
        else:
            key = "preference"
            value = _clip(groups[0], MAX_FACT_CHARS)
        if key and value:
            output.append({
                "key": key,
                "value": value,
                "source": "user",
                "source_message_id": source_message_id,
                "updated_at": utcnow(),
            })
        if len(output) >= 4:
            break
    return output


def merge_facts(existing: list[Any], question: str, *, source_message_id: int | None = None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in existing[-MAX_FACTS:]:
        if isinstance(item, dict) and item.get("key") and item.get("value"):
            normalized.append({
                "key": _clip(item.get("key"), 80).casefold(),
                "value": _clip(item.get("value"), MAX_FACT_CHARS),
                "source": _clip(item.get("source") or "user", 40),
                "source_message_id": item.get("source_message_id"),
                "updated_at": _clip(item.get("updated_at"), 80),
            })
    additions = extract_facts(question, source_message_id=source_message_id)
    correction = bool(_CORRECTION.search(question))
    for fact in additions:
        key = fact["key"]
        if correction or any(item["key"] == key for item in normalized):
            normalized = [item for item in normalized if item["key"] != key]
        normalized.append(fact)
    return normalized[-MAX_FACTS:]


def bounded_mode_state(current: Any, update: Any) -> dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    if isinstance(update, dict):
        base.update(update)
    # Round-trip to enforce serializable, then trim deterministically by keys.
    safe = _safe_json(base, {})
    if not isinstance(safe, dict):
        return {}
    raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(raw) <= MAX_MODE_STATE_CHARS:
        return safe
    output: dict[str, Any] = {}
    for key in sorted(safe):
        candidate = {**output, str(key)[:80]: safe[key]}
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > MAX_MODE_STATE_CHARS:
            break
        output = candidate
    return output


def build_summary(rows: list[dict[str, Any]], *, recent_limit: int = 20) -> str:
    if len(rows) <= recent_limit:
        return ""
    older = rows[:-recent_limit]
    blocks: list[str] = []
    for row in older:
        role = str(row.get("role") or "").casefold()
        if role not in {"user", "assistant"}:
            continue
        content = _clip(row.get("content"), 420)
        if content:
            blocks.append(f"{role}: {content}")
    # Preserve the earliest durable context and the newest compressed context.
    if not blocks:
        return ""
    if len("\n".join(blocks)) <= MAX_SUMMARY_CHARS:
        return "\n".join(blocks)
    head = blocks[:4]
    tail: list[str] = []
    remaining = MAX_SUMMARY_CHARS - len("\n".join(head)) - 40
    for block in reversed(blocks[4:]):
        if len(block) + 1 > remaining:
            continue
        tail.append(block)
        remaining -= len(block) + 1
    return "\n".join(head + ["… older turns compacted …"] + list(reversed(tail)))[:MAX_SUMMARY_CHARS]
