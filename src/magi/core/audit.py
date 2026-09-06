import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from magi.core.db import Database


class AuditLogger:
    def __init__(self, db: Database, log_dir: Path):
        self.db = db
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_daily_log_file(self) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"audit-{date_str}.jsonl"

    def _write_jsonl_sync(self, filepath: Path, entry: Dict[str, Any]) -> None:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def log_event(
        self,
        actor: str,
        action: str,
        tier: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        now_str = datetime.now(timezone.utc).isoformat()
        clean_detail = detail or {}

        # 1. Guardar en SQLite (aiosqlite)
        log_id = await self.db.add_audit_entry(
            actor=actor,
            action=action,
            tier=tier,
            detail=clean_detail,
        )

        # 2. Guardar en JSONL append-only diario (Opción B)
        jsonl_entry = {
            "id": log_id,
            "timestamp": now_str,
            "actor": actor,
            "action": action,
            "tier": tier,
            "detail": clean_detail,
        }

        log_file = self._get_daily_log_file()
        await asyncio.to_thread(self._write_jsonl_sync, log_file, jsonl_entry)

        return log_id

    async def get_recent_logs(
        self, limit: int = 25, offset: int = 0
    ) -> List[Dict[str, Any]]:
        return await self.db.get_audit_logs(limit=limit, offset=offset)

    async def get_action_detail(self, action_id: str) -> Optional[Dict[str, Any]]:
        action = await self.db.get_action(action_id)
        if not action:
            return None
        votes = await self.db.get_votes_for_action(action_id)
        result = dict(action)
        result["votes"] = votes
        return result
