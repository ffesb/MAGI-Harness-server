import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
import psutil

from magi.executor.tools.base import BaseTool, ToolResult


async def run_cmd_async(cmd: List[str], timeout: float = 10.0) -> ToolResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            return ToolResult(success=True, data=out_str, tier=0)
        return ToolResult(
            success=False,
            error=err_str or f"Comando falló con código {proc.returncode}",
            data=out_str,
            tier=0,
        )
    except asyncio.TimeoutError:
        return ToolResult(success=False, error="Comando excedió el tiempo límite (timeout)", tier=0)
    except Exception as e:
        return ToolResult(success=False, error=str(e), tier=0)


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Lee de forma segura el contenido de un archivo de texto o log del sistema."
    tier = 0
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta absoluta del archivo a leer"},
            "max_lines": {
                "type": "integer",
                "description": "Cantidad máxima de líneas a devolver (por defecto 150)",
                "default": 150,
            },
            "offset": {
                "type": "integer",
                "description": "Línea inicial (1-indexed)",
                "default": 1,
            },
        },
        "required": ["path"],
    }

    async def execute(self, path: str, max_lines: int = 150, offset: int = 1, **kwargs) -> ToolResult:
        file_path = Path(path).resolve()
        if not file_path.exists():
            return ToolResult(success=False, error=f"El archivo '{path}' no existe.", tier=0)
        if not file_path.is_file():
            return ToolResult(success=False, error=f"'{path}' no es un archivo regular.", tier=0)

        # Prevenir lecturas peligrosas de dispositivos virtuales infinitos
        blocked_prefixes = ("/proc/kcore", "/dev/zero", "/dev/urandom", "/dev/random")
        if str(file_path).startswith(blocked_prefixes):
            return ToolResult(success=False, error="Acceso denegado a archivo de dispositivo virtual.", tier=0)

        try:
            lines = []
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if idx < offset:
                        continue
                    if len(lines) >= max_lines:
                        break
                    lines.append(f"{idx}: {line.rstrip()}")

            return ToolResult(
                success=True,
                data={
                    "path": str(file_path),
                    "total_lines_read": len(lines),
                    "content": "\n".join(lines),
                },
                tier=0,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Error al leer archivo: {str(e)}", tier=0)


class ListDirectoryTool(BaseTool):
    name = "list_directory"
    description = "Lista los archivos y subdirectorios de una ruta del sistema."
    tier = 0
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta absoluta del directorio"},
            "max_items": {"type": "integer", "description": "Límite de elementos a listar", "default": 50},
        },
        "required": ["path"],
    }

    async def execute(self, path: str, max_items: int = 50, **kwargs) -> ToolResult:
        dir_path = Path(path).resolve()
        if not dir_path.exists():
            return ToolResult(success=False, error=f"El directorio '{path}' no existe.", tier=0)
        if not dir_path.is_dir():
            return ToolResult(success=False, error=f"'{path}' no es un directorio.", tier=0)

        try:
            items = []
            for entry in dir_path.iterdir():
                try:
                    stat = entry.stat()
                    items.append(
                        {
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size_bytes": stat.st_size if entry.is_file() else None,
                        }
                    )
                except Exception:
                    continue
                if len(items) >= max_items:
                    break

            return ToolResult(
                success=True,
                data={"path": str(dir_path), "count": len(items), "items": items},
                tier=0,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Error al listar directorio: {str(e)}", tier=0)


class InspectDockerTool(BaseTool):
    name = "inspect_docker"
    description = "Inspecciona contenedores Docker, su estado y los logs recientes."
    tier = 0
    parameters = {
        "type": "object",
        "properties": {
            "container_name": {
                "type": "string",
                "description": "Nombre o ID del contenedor a inspeccionar (opcional)",
            },
            "show_logs": {
                "type": "boolean",
                "description": "Si es True, extrae las últimas 60 líneas de logs del contenedor",
                "default": False,
            },
        },
    }

    async def execute(self, container_name: Optional[str] = None, show_logs: bool = False, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(
                success=False,
                error="El binario 'docker' no está instalado o no se encuentra en el PATH del servidor.",
                tier=0,
            )

        if not container_name:
            # Listar todos los contenedores
            cmd = ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"]
            res = await run_cmd_async(cmd)
            if not res.success:
                return res
            containers = []
            if res.data:
                for line in res.data.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        containers.append(
                            {
                                "id": parts[0],
                                "name": parts[1],
                                "status": parts[2],
                                "image": parts[3],
                                "ports": parts[4] if len(parts) > 4 else "",
                            }
                        )
            return ToolResult(success=True, data={"containers": containers}, tier=0)

        # Inspeccionar contenedor específico
        if show_logs:
            cmd = ["docker", "logs", "--tail", "60", container_name]
            return await run_cmd_async(cmd)
        else:
            cmd = ["docker", "inspect", container_name]
            return await run_cmd_async(cmd)


class InspectSystemdTool(BaseTool):
    name = "inspect_systemd"
    description = "Inspecciona el estado de servicios systemd o consulta los logs de journald."
    tier = 0
    parameters = {
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": "Nombre del servicio (ej. 'nginx', 'ssh', 'docker'). Si no se indica, lista los fallidos y principales.",
            },
            "show_journal": {
                "type": "boolean",
                "description": "Si es True, extrae los últimos logs de journalctl para el servicio",
                "default": False,
            },
            "lines": {
                "type": "integer",
                "description": "Cantidad de líneas de journalctl a retornar",
                "default": 50,
            },
        },
    }

    async def execute(
        self,
        service_name: Optional[str] = None,
        show_journal: bool = False,
        lines: int = 50,
        **kwargs,
    ) -> ToolResult:
        if not service_name:
            # Listar servicios fallidos o estado general
            cmd = ["systemctl", "list-units", "--type=service", "--state=failed", "--no-pager"]
            return await run_cmd_async(cmd)

        if show_journal:
            cmd = ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"]
            return await run_cmd_async(cmd)
        else:
            cmd = ["systemctl", "status", service_name, "--no-pager"]
            return await run_cmd_async(cmd)


class ServerMetricsTool(BaseTool):
    name = "get_server_metrics"
    description = "Obtiene telemetría en vivo del servidor: CPU, memoria, swap, disco y red."
    tier = 0
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu_times = psutil.cpu_percent(interval=0.5, percpu=True)
        net_io = psutil.net_io_counters()

        data = {
            "cpu": {
                "total_percent": psutil.cpu_percent(interval=None),
                "per_core_percent": cpu_times,
                "logical_cores": psutil.cpu_count(logical=True),
            },
            "memory": {
                "total_mb": vmem.total // (1024 * 1024),
                "available_mb": vmem.available // (1024 * 1024),
                "used_percent": vmem.percent,
            },
            "swap": {
                "total_mb": swap.total // (1024 * 1024),
                "used_mb": swap.used // (1024 * 1024),
                "percent": swap.percent,
            },
            "network": {
                "bytes_sent_mb": round(net_io.bytes_sent / (1024 * 1024), 2),
                "bytes_recv_mb": round(net_io.bytes_recv / (1024 * 1024), 2),
            },
        }
        return ToolResult(success=True, data=data, tier=0)
