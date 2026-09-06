import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from magi.core.config import Settings
from magi.core.db import Database
from magi.core.audit import AuditLogger


class SessionManager:
    def __init__(self, db: Database, settings: Settings, audit: AuditLogger):
        self.db = db
        self.settings = settings
        self.audit = audit

    def _freeze_prompts(self) -> Dict[str, Any]:
        """Copia inmutable de los prompts de todas las mentes configuradas."""
        snapshot = {}
        for mind_name, mind_cfg in self.settings.minds.items():
            snapshot[mind_name] = {
                "name": mind_cfg.name,
                "display_name": mind_cfg.display_name,
                "model": mind_cfg.model,
                "temperature": mind_cfg.temperature,
                "max_tokens": mind_cfg.max_tokens,
                "system_prompt": mind_cfg.system_prompt,
                "tolerance_level": mind_cfg.tolerance_level,
                "custom_tolerance_prompt": mind_cfg.custom_tolerance_prompt,
            }
        return snapshot

    async def create_new_session(
        self,
        chat_id: int,
        title: Optional[str] = None,
        server_snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        session_title = title or f"Operación {now_str}"

        prompts_snapshot = self._freeze_prompts()

        await self.db.create_session(
            session_id=session_id,
            title=session_title,
            chat_id=chat_id,
            prompts_snapshot=prompts_snapshot,
            server_snapshot_id=server_snapshot_id,
        )

        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="SESSION_CREATED",
            tier=0,
            detail={"session_id": session_id, "title": session_title},
        )

        active = await self.db.get_active_session(chat_id)
        return active or {"id": session_id, "title": session_title}

    async def get_or_create_active_session(self, chat_id: int) -> Dict[str, Any]:
        active = await self.db.get_active_session(chat_id)
        if not active:
            return await self.create_new_session(chat_id)
        return active

    async def list_sessions(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        return await self.db.list_sessions(chat_id=chat_id, limit=limit)

    async def switch_session(self, chat_id: int, session_id: str) -> bool:
        success = await self.db.switch_session(chat_id=chat_id, session_id=session_id)
        if success:
            await self.audit.log_event(
                actor=f"user:{chat_id}",
                action="SESSION_SWITCHED",
                tier=0,
                detail={"session_id": session_id},
            )
        return success

    async def delete_session(self, chat_id: int, session_id: str) -> bool:
        success = await self.db.delete_session(chat_id=chat_id, session_id=session_id)
        if success:
            await self.audit.log_event(
                actor=f"user:{chat_id}",
                action="SESSION_DELETED",
                tier=1,
                detail={"session_id": session_id},
            )
        return success

    async def record_user_message(
        self, session_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        return await self.db.add_message(
            session_id=session_id,
            role="user",
            content=content,
            metadata=metadata,
        )

    async def record_assistant_message(
        self, session_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        return await self.db.add_message(
            session_id=session_id,
            role="assistant",
            content=content,
            metadata=metadata,
        )

    async def get_session_history(
        self, session_id: str, limit: int = 30
    ) -> List[Dict[str, Any]]:
        return await self.db.get_messages(session_id=session_id, limit=limit)
