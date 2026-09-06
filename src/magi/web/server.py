import asyncio
import logging
from pathlib import Path
from typing import Any, Optional
import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from magi.web.routes.api import api_router
from magi.web.routes.ws import ws_manager, ws_router

logger = logging.getLogger("magi.web.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(magi_daemon: Any) -> FastAPI:
    app = FastAPI(
        title="MAGI Harness Web Console",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Guardar referencia al estado de MAGI
    app.state.magi = magi_daemon
    app.state.ws_manager = ws_manager

    # Routers
    app.include_router(api_router)
    app.include_router(ws_router)

    # Static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def serve_index():
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse("<h1>MAGI Harness Web Console</h1><p>Frontend static files not found.</p>")

    return app


class MagiWebServer:
    def __init__(self, daemon: Any):
        self.daemon = daemon
        self.settings = daemon.settings
        self.host = getattr(self.settings, "web_host", "127.0.0.1")
        self.port = getattr(self.settings, "web_port", 8080)
        self.app = create_app(daemon)
        self._server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None
        self._telemetry_task: Optional[asyncio.Task] = None
        self._running = False

    async def _relay_ipc_event(self, event_type: str, data: Any) -> None:
        """Puente para reenviar eventos del IPC hacia los WebSockets en tiempo real."""
        try:
            await ws_manager.broadcast(event_type, data)
        except Exception:
            pass

    async def _telemetry_broadcast_loop(self) -> None:
        """Transmite telemetría en vivo vía WebSockets cada 2 segundos a clientes conectados."""
        while self._running:
            try:
                if ws_manager.active_connections:
                    cpu = psutil.cpu_percent(interval=None)
                    mem = psutil.virtual_memory()
                    now = asyncio.get_event_loop().time()
                    active_sess_id = "--"
                    try:
                        active_sess = await self.daemon.db.get_latest_active_session()
                        if active_sess:
                            active_sess_id = active_sess.get("id", "--")
                    except Exception:
                        pass

                    telem_data = {
                        "cpu": cpu,
                        "ram": mem.percent,
                        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
                        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
                        "session_id": active_sess_id,
                    }
                    await ws_manager.broadcast("telemetry_live", telem_data)
            except Exception:
                pass
            await asyncio.sleep(2)

    async def start(self) -> None:
        self._running = True

        # Registrar bridge de eventos con el IPC
        if hasattr(self.daemon, "ipc") and self.daemon.ipc:
            self.daemon.ipc.add_event_listener(self._relay_ipc_event)

        # Iniciar Uvicorn
        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        self._telemetry_task = asyncio.create_task(self._telemetry_broadcast_loop())
        print(f"[+] MAGI Web Console en línea: http://{self.host}:{self.port}")

    async def stop(self) -> None:
        self._running = False
        if self._telemetry_task:
            self._telemetry_task.cancel()
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        print("[+] MAGI Web Console detenida.")


def main():
    """Punto de entrada para CLI: magi-web."""
    from magi.daemon import MagiDaemon
    daemon = MagiDaemon()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def runner():
        await daemon.start()
        # El servidor web ya se habrá iniciado en daemon.start() si web_enabled es True
        # Mantener proceso vivo
        stop_evt = asyncio.Event()
        try:
            await stop_evt.wait()
        finally:
            await daemon.stop()

    try:
        loop.run_until_complete(runner())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
