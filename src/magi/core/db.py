import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS server_state_snapshots (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    prompts_snapshot TEXT NOT NULL,
    server_snapshot_id TEXT,
    FOREIGN KEY(server_snapshot_id) REFERENCES server_state_snapshots(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL,
    plan TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS magi_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    mind_name TEXT NOT NULL,
    vote TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT NOT NULL,
    concerns TEXT,
    model_used TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES actions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cronjobs (
    id TEXT PRIMARY KEY,
    expression TEXT NOT NULL,
    action_template TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run TEXT,
    next_run TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    tier INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mind_configs (
    mind_name TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    temperature REAL NOT NULL,
    max_tokens INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    metrics TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON sessions(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_session_id ON actions(session_id);
CREATE INDEX IF NOT EXISTS idx_magi_votes_action_id ON magi_votes(action_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected. Call await connect() first.")
        return self._conn

    # --- SESSIONS ---
    async def create_session(
        self,
        session_id: str,
        title: str,
        chat_id: int,
        prompts_snapshot: Dict[str, Any],
        server_snapshot_id: Optional[str] = None,
    ) -> None:
        now = now_iso()
        # Deactivate any other active session for this chat_id
        await self.conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE chat_id = ?", (chat_id,)
        )
        await self.conn.execute(
            """
            INSERT INTO sessions (id, title, chat_id, created_at, updated_at, is_active, prompts_snapshot, server_snapshot_id)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                session_id,
                title,
                chat_id,
                now,
                now,
                json.dumps(prompts_snapshot),
                server_snapshot_id,
            ),
        )
        await self.conn.commit()

    async def get_active_session(self, chat_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE chat_id = ? AND is_active = 1 LIMIT 1",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_latest_active_session(self) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_sessions(self, chat_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE chat_id = ? ORDER BY updated_at DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def switch_session(self, chat_id: int, session_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND chat_id = ?",
            (session_id, chat_id),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        await self.conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE chat_id = ?", (chat_id,)
        )
        await self.conn.execute(
            "UPDATE sessions SET is_active = 1, updated_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )
        await self.conn.commit()
        return True

    async def delete_session(self, chat_id: int, session_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT is_active FROM sessions WHERE id = ? AND chat_id = ?",
            (session_id, chat_id),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        was_active = bool(row["is_active"])
        await self.conn.execute(
            "DELETE FROM sessions WHERE id = ? AND chat_id = ?", (session_id, chat_id)
        )
        if was_active:
            # Set the most recent remaining session as active if any
            next_cursor = await self.conn.execute(
                "SELECT id FROM sessions WHERE chat_id = ? ORDER BY updated_at DESC LIMIT 1",
                (chat_id,),
            )
            next_row = await next_cursor.fetchone()
            if next_row:
                await self.conn.execute(
                    "UPDATE sessions SET is_active = 1 WHERE id = ?",
                    (next_row["id"],),
                )
        await self.conn.commit()
        return True

    # --- MESSAGES ---
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = now_iso()
        cursor = await self.conn.execute(
            """
            INSERT INTO messages (session_id, role, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                json.dumps(metadata) if metadata else None,
                now,
            ),
        )
        await self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_messages(
        self, session_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- AUDIT LOG ---
    async def add_audit_entry(
        self,
        actor: str,
        action: str,
        tier: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = now_iso()
        cursor = await self.conn.execute(
            """
            INSERT INTO audit_log (actor, action, tier, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                tier,
                json.dumps(detail) if detail else None,
                now,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_audit_logs(
        self, limit: int = 30, offset: int = 0
    ) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- ACTIONS & VOTES ---
    async def create_action(
        self,
        action_id: str,
        session_id: str,
        tool_name: str,
        tier: int,
        status: str,
        plan: Dict[str, Any],
    ) -> None:
        now = now_iso()
        await self.conn.execute(
            """
            INSERT INTO actions (id, session_id, tool_name, tier, status, plan, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                session_id,
                tool_name,
                tier,
                status,
                json.dumps(plan),
                now,
                now,
            ),
        )
        await self.conn.commit()

    async def update_action_status(
        self,
        action_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        now = now_iso()
        await self.conn.execute(
            """
            UPDATE actions
            SET status = ?, result = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(result) if result else None,
                error,
                now,
                action_id,
            ),
        )
        await self.conn.commit()

    async def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def add_vote(
        self,
        action_id: str,
        mind_name: str,
        vote: str,
        confidence: float,
        reasoning: str,
        concerns: List[str],
        model_used: str,
        latency_ms: int,
    ) -> int:
        now = now_iso()
        cursor = await self.conn.execute(
            """
            INSERT INTO magi_votes (action_id, mind_name, vote, confidence, reasoning, concerns, model_used, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                mind_name,
                vote,
                confidence,
                reasoning,
                json.dumps(concerns),
                model_used,
                latency_ms,
                now,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_votes_for_action(self, action_id: str) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM magi_votes WHERE action_id = ? ORDER BY id ASC",
            (action_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- SERVER STATE SNAPSHOTS ---
    async def save_server_snapshot(
        self, snapshot_id: str, data: Dict[str, Any]
    ) -> None:
        now = now_iso()
        await self.conn.execute(
            "INSERT INTO server_state_snapshots (id, data, created_at) VALUES (?, ?, ?)",
            (snapshot_id, json.dumps(data), now),
        )
        await self.conn.commit()

    async def get_latest_server_snapshot(self) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM server_state_snapshots ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- HEARTBEATS ---
    async def record_heartbeat(
        self, status: str, metrics: Optional[Dict[str, Any]] = None
    ) -> None:
        now = now_iso()
        await self.conn.execute(
            "INSERT INTO heartbeats (status, metrics, created_at) VALUES (?, ?, ?)",
            (status, json.dumps(metrics) if metrics else None, now),
        )
        await self.conn.commit()

    async def get_latest_heartbeat(self) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
