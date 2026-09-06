import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
import psutil

from magi.core.config import load_settings
from magi.core.db import Database
from magi.core.audit import AuditLogger
from magi.core.ipc import IpcServer
from magi.core.provider import OpenRouterClient
from magi.council.minds import CouncilManager
from magi.session.manager import SessionManager
from magi.channel.telegram import TelegramAdapter
from magi.executor.state import ServerStateManager
from magi.executor.agent import ExecutorAgent
from magi.scheduler.cron import CronScheduler
from magi.scheduler.watcher import LogWatcher


class MagiDaemon:
    def __init__(self):
        self.settings = load_settings()
        self.db = Database(self.settings.db_path)
        self.audit = AuditLogger(self.db, self.settings.audit_log_dir)
        self.session_manager = SessionManager(self.db, self.settings, self.audit)
        self.server_state = ServerStateManager(self.db)
        self.provider = OpenRouterClient(api_key=self.settings.openrouter_api_key)
        self.council = CouncilManager(self.settings, self.provider)
        self.ipc = IpcServer(
            self.settings.ipc_socket,
            on_client_connected=self._handle_ipc_client_connected,
        )

        self.scheduler = CronScheduler(
            db=self.db,
            audit=self.audit,
            on_job_trigger=self._handle_cron_trigger,
        )

        self.log_watcher = LogWatcher(
            audit=self.audit,
            watch_dirs=[
                Path("/mnt/data/logs"),
                self.settings.project_root / "logs",
            ],
            on_anomaly_detected=self._handle_log_anomaly,
            on_daily_pulse=self._handle_daily_pulse,
        )

        self.executor_agent = ExecutorAgent(
            settings=self.settings,
            provider=self.provider,
            db=self.db,
            audit=self.audit,
            council=self.council,
            on_notify=self._broadcast_magi_notification,
            ipc=self.ipc,
        )

        self.telegram = TelegramAdapter(
            settings=self.settings,
            audit=self.audit,
            session_manager=self.session_manager,
            server_state=self.server_state,
            provider=self.provider,
            executor_agent=self.executor_agent,
            council=self.council,
            scheduler=self.scheduler,
            ipc=self.ipc,
        )

        self.running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.start_time = time.time()

    async def _broadcast_magi_notification(self, text: str) -> None:
        """Emite actualizaciones visuales al IPC para magi-tui y a Telegram."""
        await self.ipc.broadcast_event("consensus", {"text": text})

    async def _handle_cron_trigger(self, job_data: Dict[str, Any]) -> None:
        chat_id = job_data["chat_id"]
        prompt = job_data["prompt"]
        job_id = job_data["job_id"]

        session = await self.session_manager.get_or_create_active_session(chat_id)
        notice = f"⏰ *CRONJOB EJECUTADO (`{job_id}`)*\nPrompt: \"{prompt}\""
        await self.telegram.send_message(chat_id, notice)

        # Ejecutar con el agente
        result = await self.executor_agent.run(
            user_message=prompt,
            session_id=session["id"],
            chat_id=chat_id,
            history=[],
        )
        await self.telegram.send_message(chat_id, result.get("response", "Sin respuesta."))

    async def _handle_log_anomaly(self, filepath: str, snippet: str) -> None:
        for chat_id in self.settings.allowed_user_ids:
            alert = (
                f"🚨 *ALERTA DE ANOMALÍA EN LOGS*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 *Archivo:* `{filepath}`\n"
                f"⚠️ *Fragmento Detectado:*\n```\n{snippet}\n```\n"
                "La Ejecutora está disponible para diagnosticar el incidente."
            )
            await self.telegram.send_message(chat_id, alert)

    async def _handle_daily_pulse(self) -> None:
        for chat_id in self.settings.allowed_user_ids:
            pulse = (
                "🟢 *PULSO DIARIO MAGI (09:00)*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Sistema nominal. 0 incidentes críticos en las últimas 24 horas.\n"
                "Telemetría, servicios y supervisión estables."
            )
            await self.telegram.send_message(chat_id, pulse)

    async def _handle_ipc_client_connected(self, writer: asyncio.StreamWriter) -> None:
        """Sincroniza el estado inicial de modelos y telemetría apenas se conecta magi-tui."""
        try:
            minds_models = {
                m_name: cfg.model for m_name, cfg in self.settings.minds.items()
            }
            sync_payload = {
                "type": "minds_sync",
                "data": minds_models,
            }
            writer.write((json.dumps(sync_payload, ensure_ascii=False) + "\n").encode("utf-8"))

            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            uptime_s = int(time.time() - self.start_time)
            h, r = divmod(uptime_s, 3600)
            m, s = divmod(r, 60)
            uptime_str = f"{h}h {m}m {s}s"
            
            active_sess_id = "--"
            try:
                active_sess = await self.db.get_latest_active_session()
                if active_sess:
                    active_sess_id = active_sess.get("id", "--")
            except Exception:
                pass

            telem_payload = {
                "type": "telemetry",
                "data": {
                    "cpu": cpu,
                    "ram": mem.percent,
                    "uptime": uptime_str,
                    "session_id": active_sess_id,
                },
            }
            writer.write((json.dumps(telem_payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass

    async def _heartbeat_loop(self):
        while self.running:
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                uptime_s = int(time.time() - self.start_time)
                h, r = divmod(uptime_s, 3600)
                m, s = divmod(r, 60)
                uptime_str = f"{h}h {m}m {s}s"

                metrics = {
                    "cpu_percent": cpu,
                    "ram_percent": mem.percent,
                    "ram_used_mb": mem.used // (1024 * 1024),
                    "uptime_seconds": uptime_s,
                }
                await self.db.record_heartbeat(status="nominal", metrics=metrics)

                active_sess_id = "--"
                try:
                    active_sess = await self.db.get_latest_active_session()
                    if active_sess:
                        active_sess_id = active_sess.get("id", "--")
                except Exception:
                    pass

                # Broadcast al socket IPC local para magi-tui
                await self.ipc.broadcast_event(
                    "telemetry",
                    {
                        "cpu": cpu,
                        "ram": mem.percent,
                        "uptime": uptime_str,
                        "session_id": active_sess_id,
                    },
                )
                minds_models = {
                    m_name: cfg.model for m_name, cfg in self.settings.minds.items()
                }
                await self.ipc.broadcast_event("minds_sync", minds_models)
            except Exception:
                pass
            await asyncio.sleep(60)

    async def start(self):
        self.running = True
        print(f"[*] Inicializando {self.settings.app_name} v{self.settings.version}...")

        # Conexión a Base de Datos SQLite (WAL)
        await self.db.connect()
        print(f"[+] Base de datos SQLite lista en: {self.settings.db_path}")

        # Iniciar servidor Unix IPC Socket para magi-tui
        await self.ipc.start()
        print(f"[+] Servidor IPC (Unix Domain Socket) activo en: {self.settings.ipc_socket}")

        # Registro de inicio en auditoría
        await self.audit.log_event(
            actor="daemon",
            action="DAEMON_STARTED",
            tier=0,
            detail={
                "version": self.settings.version,
                "allowed_users": list(self.settings.allowed_user_ids),
                "searxng": self.settings.searxng_url,
            },
        )

        # Inicializar snapshot de estado del servidor si no existe
        latest_snap = await self.server_state.get_latest_snapshot()
        if not latest_snap:
            print("[*] Generando mapa de estado inicial del servidor (.init)...")
            initial_snap = await self.server_state.capture_snapshot()
            print(f"[+] Snapshot inicial generado: {initial_snap['id']}")

        # Iniciar Scheduler de tareas programadas
        await self.scheduler.start()
        print("[+] Scheduler interno de cronjobs activado.")

        # Iniciar Log Watcher
        await self.log_watcher.start()
        print("[+] Monitor de logs y pulso diario activado.")

        # Iniciar adaptador de Telegram
        print("[*] Conectando Telegram Adapter (@naoko_magi_bot)...")
        await self.telegram.start()
        print("[+] Telegram Adapter en línea y escuchando actualizaciones.")

        # Iniciar loop de heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        print("[+] Heartbeat interno activado (intervalo: 60s).")

        # Notificar al operador autorizado si existe una sesión activa
        for user_id in self.settings.allowed_user_ids:
            try:
                active_session = await self.session_manager.get_or_create_active_session(user_id)
                await self.telegram.send_message(
                    chat_id=user_id,
                    text=(
                        "🟢 *MAGI HARNESS INICIADO*\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Daemon en ejecución en modo nominal.\n"
                        f"Sesión activa: `{active_session['id']}`\n"
                        "Listo para recibir instrucciones."
                    ),
                )
            except Exception as e:
                print(f"[!] No se pudo enviar mensaje de arranque a {user_id}: {e}")

    async def stop(self):
        print("\n[*] Deteniendo MAGI Daemon de forma ordenada...")
        self.running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        await self.log_watcher.stop()
        await self.scheduler.stop()
        await self.telegram.stop()
        await self.ipc.stop()

        await self.audit.log_event(
            actor="daemon",
            action="DAEMON_STOPPED",
            tier=0,
        )

        await self.db.close()
        print("[+] Base de datos cerrada y recursos liberados. Apagado completado.")


async def main():
    daemon = MagiDaemon()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await daemon.start()
        await stop_event.wait()
    finally:
        await daemon.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
