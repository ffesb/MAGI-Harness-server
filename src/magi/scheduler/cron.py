import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional
from croniter import croniter

from magi.core.db import Database
from magi.core.audit import AuditLogger


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


class CronScheduler:
    def __init__(
        self,
        db: Database,
        audit: AuditLogger,
        on_job_trigger: Optional[
            Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]
        ] = None,
    ):
        self.db = db
        self.audit = audit
        self.on_job_trigger = on_job_trigger
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def validate_expression(self, expression: str) -> bool:
        return croniter.is_valid(expression)

    def compute_next_run(self, expression: str, base_dt: Optional[datetime] = None) -> str:
        base = base_dt or now_dt()
        itr = croniter(expression, base)
        nxt = itr.get_next(datetime)
        return nxt.isoformat()

    async def add_job(
        self, expression: str, action_template: str, chat_id: int
    ) -> Dict[str, Any]:
        if not self.validate_expression(expression):
            raise ValueError(f"Expresión cron inválida: '{expression}'. Formato esperado: '* * * * *'")

        job_id = f"cron_{uuid.uuid4().hex[:6]}"
        now_str = now_dt().isoformat()
        next_run = self.compute_next_run(expression)

        template_data = {"prompt": action_template}

        await self.db.conn.execute(
            """
            INSERT INTO cronjobs (id, expression, action_template, chat_id, is_active, created_at, next_run)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (job_id, expression, json.dumps(template_data), chat_id, now_str, next_run),
        )
        await self.db.conn.commit()

        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="CRONJOB_CREATED",
            tier=1,
            detail={"job_id": job_id, "expression": expression, "template": action_template},
        )

        return {
            "id": job_id,
            "expression": expression,
            "action_template": action_template,
            "chat_id": chat_id,
            "next_run": next_run,
        }

    async def list_jobs(self, chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if chat_id:
            cursor = await self.db.conn.execute(
                "SELECT * FROM cronjobs WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,)
            )
        else:
            cursor = await self.db.conn.execute(
                "SELECT * FROM cronjobs ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        jobs = []
        for r in rows:
            d = dict(r)
            try:
                t = json.loads(d.get("action_template", "{}"))
                d["prompt"] = t.get("prompt", "")
            except Exception:
                d["prompt"] = d.get("action_template", "")
            jobs.append(d)
        return jobs

    async def delete_job(self, job_id: str, chat_id: Optional[int] = None) -> bool:
        if chat_id:
            cursor = await self.db.conn.execute(
                "DELETE FROM cronjobs WHERE id = ? AND chat_id = ?", (job_id, chat_id)
            )
        else:
            cursor = await self.db.conn.execute(
                "DELETE FROM cronjobs WHERE id = ?", (job_id,)
            )
        await self.db.conn.commit()
        deleted = cursor.rowcount > 0
        if deleted and chat_id:
            await self.audit.log_event(
                actor=f"user:{chat_id}",
                action="CRONJOB_DELETED",
                tier=1,
                detail={"job_id": job_id},
            )
        return deleted

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_pending_jobs()
            except Exception:
                pass
            await asyncio.sleep(20)

    async def _check_pending_jobs(self) -> None:
        now_iso_str = now_dt().isoformat()
        cursor = await self.db.conn.execute(
            "SELECT * FROM cronjobs WHERE is_active = 1 AND next_run <= ?", (now_iso_str,)
        )
        due_jobs = await cursor.fetchall()

        for row in due_jobs:
            job = dict(row)
            job_id = job["id"]
            expr = job["expression"]
            chat_id = job["chat_id"]

            try:
                tpl = json.loads(job.get("action_template", "{}"))
                prompt = tpl.get("prompt", "")
            except Exception:
                prompt = job.get("action_template", "")

            # Actualizar próximo disparo para evitar doble ejecución
            next_run = self.compute_next_run(expr)
            await self.db.conn.execute(
                "UPDATE cronjobs SET last_run = ?, next_run = ? WHERE id = ?",
                (now_iso_str, next_run, job_id),
            )
            await self.db.conn.commit()

            await self.audit.log_event(
                actor="scheduler",
                action="CRONJOB_TRIGGERED",
                tier=1,
                detail={"job_id": job_id, "prompt": prompt, "chat_id": chat_id},
            )

            # Disparar callback
            if self.on_job_trigger:
                try:
                    await self.on_job_trigger({"job_id": job_id, "prompt": prompt, "chat_id": chat_id})
                except Exception as e:
                    await self.audit.log_event(
                        actor="scheduler",
                        action="CRONJOB_EXECUTION_ERROR",
                        tier=1,
                        detail={"job_id": job_id, "error": str(e)},
                    )
