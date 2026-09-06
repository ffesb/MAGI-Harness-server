import asyncio
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from magi.core.config import load_settings
from magi.core.db import Database
from magi.core.audit import AuditLogger
from magi.session.manager import SessionManager
from magi.scheduler.cron import CronScheduler
from magi.council.minds import CouncilManager
from magi.web.server import create_app


class MockDaemon:
    def __init__(self, db_path: Path, audit_dir: Path):
        self.settings = load_settings()
        self.db = Database(db_path)
        self.audit = AuditLogger(self.db, audit_dir)
        self.session_manager = SessionManager(self.db, self.settings, self.audit)
        self.scheduler = CronScheduler(self.db, self.audit)
        self.council = CouncilManager(self.settings, None)
        self.provider = None
        self.server_state = None
        self.executor_agent = None
        self.ipc = None
        self.start_time = 1000.0


def create_test_setup():
    tmp_dir = tempfile.mkdtemp()
    tmp_path = Path(tmp_dir)
    db_path = tmp_path / "test.db"
    audit_dir = tmp_path / "logs"

    daemon = MockDaemon(db_path, audit_dir)
    asyncio.run(daemon.db.connect())

    app = create_app(daemon)
    client = TestClient(app)
    return client, daemon


def test_serve_index():
    client, daemon = create_test_setup()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "MAGI HARNESS" in resp.text
    assert "NERV CENTRAL TACTICAL MONITOR" in resp.text
    asyncio.run(daemon.db.close())


def test_get_status():
    client, daemon = create_test_setup()
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "app_name" in data
    assert "version" in data
    assert "cpu_percent" in data
    assert "ram_percent" in data
    assert "uptime" in data
    asyncio.run(daemon.db.close())


def test_get_commands():
    client, daemon = create_test_setup()
    resp = client.get("/api/commands")
    assert resp.status_code == 200
    cmds = resp.json()
    assert any(c["command"] == "/tolerance" for c in cmds)
    assert any(c["command"] == "/model" for c in cmds)
    assert any(c["command"] == "/new" for c in cmds)
    asyncio.run(daemon.db.close())


def test_minds_and_tolerance():
    client, daemon = create_test_setup()
    resp = client.get("/api/minds")
    assert resp.status_code == 200
    minds = resp.json()
    assert len(minds) >= 3

    # Verify tolerance levels are present
    melchior = next(m for m in minds if m["name"] == "MELCHIOR")
    assert "tolerance_level" in melchior
    assert "effective_system_prompt" in melchior
    assert "PRIORIDAD: CRITERIO DE TOLERANCIA" in melchior["effective_system_prompt"]

    # Update tolerance level to seguridad
    update_resp = client.post(
        "/api/minds/MELCHIOR/config",
        json={"tolerance_level": "seguridad"},
    )
    assert update_resp.status_code == 200
    up_data = update_resp.json()
    assert up_data["tolerance_level"] == "seguridad"
    assert "SEGURIDAD MÁXIMA" in up_data["effective_prompt"]

    # Reset back to default
    client.post(
        "/api/minds/MELCHIOR/config",
        json={"tolerance_level": "default"},
    )
    asyncio.run(daemon.db.close())


def test_sessions_crud():
    client, daemon = create_test_setup()
    # Create session
    create_resp = client.post("/api/sessions", json={"title": "Test Operation Alpha"})
    assert create_resp.status_code == 200
    s_data = create_resp.json()
    s_id = s_data["id"]
    assert s_data["title"] == "Test Operation Alpha"

    # List sessions
    list_resp = client.get("/api/sessions")
    assert list_resp.status_code == 200
    sessions = list_resp.json()
    assert any(s["id"] == s_id for s in sessions)

    # Delete session
    del_resp = client.delete(f"/api/sessions/{s_id}")
    assert del_resp.status_code == 200
    asyncio.run(daemon.db.close())


def test_cronjobs_crud():
    client, daemon = create_test_setup()
    # Create cronjob
    create_resp = client.post(
        "/api/cronjobs",
        json={"expression": "0 9 * * *", "prompt": "Daily pulse check"},
    )
    assert create_resp.status_code == 200
    job = create_resp.json()
    job_id = job["id"]

    # List cronjobs
    list_resp = client.get("/api/cronjobs")
    assert list_resp.status_code == 200
    jobs = list_resp.json()
    assert any(j["id"] == job_id for j in jobs)

    # Delete cronjob
    del_resp = client.delete(f"/api/cronjobs/{job_id}")
    assert del_resp.status_code == 200
    asyncio.run(daemon.db.close())


def test_chat_commands():
    client, daemon = create_test_setup()
    # Test /status command
    status_resp = client.post("/api/chat", json={"message": "/status"})
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["type"] == "command_result"
    assert "MAGI STATUS" in data["response"]

    # Test /tolerance command via chat
    tol_resp = client.post("/api/chat", json={"message": "/tolerance MELCHIOR seguridad"})
    assert tol_resp.status_code == 200
    data = tol_resp.json()
    assert data["type"] == "command_result"
    assert "SEGURIDAD" in data["response"]
    assert daemon.settings.minds["MELCHIOR"].tolerance_level == "seguridad"

    # Reset
    client.post("/api/chat", json={"message": "/tolerance MELCHIOR default"})
    asyncio.run(daemon.db.close())


def test_provider_api():
    client, daemon = create_test_setup()
    # Get provider
    get_resp = client.get("/api/provider")
    assert get_resp.status_code == 200
    assert "api_key_masked" in get_resp.json()

    # Update provider key
    post_resp = client.post("/api/provider", json={"api_key": "sk-or-v1-testkey1234567890"})
    assert post_resp.status_code == 200
    assert post_resp.json()["success"] is True
    assert daemon.settings.openrouter_api_key == "sk-or-v1-testkey1234567890"

    asyncio.run(daemon.db.close())


def test_audit_api():
    client, daemon = create_test_setup()
    resp = client.get("/api/audit")
    assert resp.status_code == 200
    logs = resp.json()
    assert isinstance(logs, list)
    asyncio.run(daemon.db.close())
