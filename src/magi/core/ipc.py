import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set


class IpcServer:
    def __init__(
        self,
        socket_path: Path,
        on_client_connected: Optional[
            Callable[[asyncio.StreamWriter], Coroutine[Any, Any, None]]
        ] = None,
    ):
        self.socket_path = socket_path
        self.on_client_connected = on_client_connected
        self._server: Optional[asyncio.Server] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._event_listeners: List[
            Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]
        ] = []
        self._running = False

    def add_event_listener(
        self,
        callback: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._event_listeners.append(callback)

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Limpiar socket previo si existía
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        # Restringir permisos del socket
        try:
            os.chmod(self.socket_path, 0o600)
        except Exception:
            pass

        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._clients):
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        self._clients.clear()
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.add(writer)
        if self.on_client_connected:
            try:
                await self.on_client_connected(writer)
            except Exception:
                pass
        try:
            # Mantener conexión abierta mientras el cliente esté conectado
            while self._running:
                data = await reader.readline()
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> None:
        # Notificar a listeners internos (WebSockets, monitores en proceso)
        for listener in list(self._event_listeners):
            try:
                res = listener(event_type, data)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                pass

        if not self._clients:
            return
        payload = {"type": event_type, "data": data}
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        for writer in list(self._clients):
            try:
                writer.write(line)
                await writer.drain()
            except Exception:
                self._clients.discard(writer)
