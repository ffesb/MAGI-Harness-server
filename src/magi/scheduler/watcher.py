import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from magi.core.audit import AuditLogger


class LogWatcher:
    def __init__(
        self,
        audit: AuditLogger,
        watch_dirs: List[Path],
        on_anomaly_detected: Optional[
            Callable[[str, str], Coroutine[Any, Any, None]]
        ] = None,
        on_daily_pulse: Optional[
            Callable[[], Coroutine[Any, Any, None]]
        ] = None,
    ):
        self.audit = audit
        self.watch_dirs = watch_dirs
        self.on_anomaly_detected = on_anomaly_detected
        self.on_daily_pulse = on_daily_pulse
        self._file_positions: Dict[str, int] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_pulse_day: Optional[str] = None

    def _discover_log_files(self) -> List[Path]:
        files = []
        for d in self.watch_dirs:
            if d.exists() and d.is_dir():
                for f in d.glob("**/*"):
                    if f.is_file() and (f.suffix in (".log", ".txt") or "log" in f.name.lower()):
                        files.append(f)
        return files

    async def start(self) -> None:
        self._running = True
        # Inicializar posiciones de los archivos existentes para no alertar de logs históricos
        for f in self._discover_log_files():
            try:
                self._file_positions[str(f)] = f.stat().st_size
            except Exception:
                pass
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._check_logs()
                await self._check_daily_pulse()
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _check_logs(self) -> None:
        current_files = self._discover_log_files()
        for f in current_files:
            p_str = str(f)
            prev_pos = self._file_positions.get(p_str, 0)
            try:
                curr_size = f.stat().st_size
                if curr_size > prev_pos:
                    # Nuevas líneas escritas
                    with open(f, "r", encoding="utf-8", errors="replace") as fp:
                        fp.seek(prev_pos)
                        new_content = fp.read()
                    self._file_positions[p_str] = curr_size

                    # Buscar patrones de severidad
                    lower = new_content.lower()
                    anomalies = []
                    keywords = ("fatal", "panic", "critical", "oom-killer", "segfault")
                    for line in new_content.splitlines():
                        if any(k in line.lower() for k in keywords):
                            anomalies.append(line.strip())

                    if anomalies and self.on_anomaly_detected:
                        snippet = "\n".join(anomalies[:5])
                        await self.on_anomaly_detected(p_str, snippet)
                        await self.audit.log_event(
                            actor="log_watcher",
                            action="LOG_ANOMALY_DETECTED",
                            tier=1,
                            detail={"file": p_str, "sample": snippet[:300]},
                        )
                elif curr_size < prev_pos:
                    # El log fue rotado
                    self._file_positions[p_str] = curr_size
            except Exception:
                pass

    async def _check_daily_pulse(self) -> None:
        now = datetime.now()
        # Verificar si son las 09:00 AM en hora local
        if now.hour == 9 and now.minute <= 2:
            today_str = now.strftime("%Y-%m-%d")
            if self._last_pulse_day != today_str:
                self._last_pulse_day = today_str
                if self.on_daily_pulse:
                    try:
                        await self.on_daily_pulse()
                    except Exception:
                        pass
