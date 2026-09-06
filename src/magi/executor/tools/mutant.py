import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from magi.executor.tools.base import BaseTool, ToolResult


async def run_cmd_async(cmd: List[str], timeout: float = 20.0) -> ToolResult:
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
            return ToolResult(success=True, data=out_str)
        return ToolResult(
            success=False,
            error=err_str or f"Falló con código de salida {proc.returncode}",
            data=out_str,
        )
    except asyncio.TimeoutError:
        return ToolResult(success=False, error="Comando excedió el tiempo límite (timeout)")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# --- TIER 1: BAJO IMPACTO / REVERSIBLE ---

class DockerRestartTool(BaseTool):
    name = "docker_container_restart"
    description = "Reinicia un contenedor Docker existente."
    tier = 1
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nombre o ID del contenedor Docker a reiniciar"}
        },
        "required": ["name"],
    }

    async def execute(self, name: str, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(success=False, error="Docker no está disponible en este servidor.", tier=1)
        res = await run_cmd_async(["docker", "restart", name])
        res.tier = 1
        return res


class DockerStartTool(BaseTool):
    name = "docker_container_start"
    description = "Inicia un contenedor Docker que se encuentra detenido."
    tier = 1
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nombre o ID del contenedor a iniciar"}
        },
        "required": ["name"],
    }

    async def execute(self, name: str, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(success=False, error="Docker no está disponible en este servidor.", tier=1)
        res = await run_cmd_async(["docker", "start", name])
        res.tier = 1
        return res


# --- TIER 2: CRÍTICO / DIFÍCIL DE REVERTIR ---

class DockerStopTool(BaseTool):
    name = "docker_container_stop"
    description = "Detiene un contenedor Docker en ejecución."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nombre o ID del contenedor a detener"}
        },
        "required": ["name"],
    }

    async def execute(self, name: str, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(success=False, error="Docker no está disponible.", tier=2)
        res = await run_cmd_async(["docker", "stop", name])
        res.tier = 2
        return res


class DockerRemoveTool(BaseTool):
    name = "docker_container_remove"
    description = "Elimina permanentemente un contenedor Docker."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nombre o ID del contenedor a eliminar"},
            "force": {"type": "boolean", "description": "Forzar detención y borrado", "default": False},
        },
        "required": ["name"],
    }

    async def execute(self, name: str, force: bool = False, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(success=False, error="Docker no está disponible.", tier=2)
        cmd = ["docker", "rm", "-f" if force else name]
        if force:
            cmd.append(name)
        res = await run_cmd_async(cmd)
        res.tier = 2
        return res


class DockerPruneTool(BaseTool):
    name = "docker_system_prune"
    description = "Ejecuta docker system prune para eliminar recursos huérfanos."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "all": {"type": "boolean", "description": "Eliminar imágenes no usadas también", "default": False},
            "volumes": {"type": "boolean", "description": "Eliminar volúmenes huérfanos", "default": False},
        },
    }

    async def execute(self, all: bool = False, volumes: bool = False, **kwargs) -> ToolResult:
        if not shutil.which("docker"):
            return ToolResult(success=False, error="Docker no está disponible.", tier=2)
        cmd = ["docker", "system", "prune", "-f"]
        if all:
            cmd.append("-a")
        if volumes:
            cmd.append("--volumes")
        res = await run_cmd_async(cmd, timeout=60.0)
        res.tier = 2
        return res


class PackageInstallTool(BaseTool):
    name = "package_install"
    description = "Instala un paquete en el sistema operativo mediante el gestor oficial (pacman / apt)."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Nombre del paquete a instalar"}
        },
        "required": ["package"],
    }

    async def execute(self, package: str, **kwargs) -> ToolResult:
        pkg_clean = package.strip()
        # Detección del gestor de paquetes del sistema
        if shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", pkg_clean]
        elif shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", pkg_clean]
        else:
            return ToolResult(success=False, error="Gestor de paquetes no soportado.", tier=2)

        res = await run_cmd_async(cmd, timeout=120.0)
        res.tier = 2
        return res


class PackageRemoveTool(BaseTool):
    name = "package_remove"
    description = "Desinstala un paquete del sistema operativo."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Nombre del paquete a desinstalar"}
        },
        "required": ["package"],
    }

    async def execute(self, package: str, **kwargs) -> ToolResult:
        pkg_clean = package.strip()
        if shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-R", "--noconfirm", pkg_clean]
        elif shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "remove", "-y", pkg_clean]
        else:
            return ToolResult(success=False, error="Gestor de paquetes no soportado.", tier=2)

        res = await run_cmd_async(cmd, timeout=120.0)
        res.tier = 2
        return res


class WriteConfigFileTool(BaseTool):
    name = "write_config_file"
    description = "Escribe o actualiza un archivo de configuración del sistema creando un backup automático previo (.bak)."
    tier = 2
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta absoluta del archivo"},
            "content": {"type": "string", "description": "Contenido a escribir"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, path: str, content: str, **kwargs) -> ToolResult:
        file_path = Path(path).resolve()
        try:
            # Si el archivo existe, hacer backup automático previo (.bak)
            if file_path.exists():
                bak_path = file_path.with_suffix(file_path.suffix + ".bak")
                shutil.copy2(file_path, bak_path)

            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return ToolResult(
                success=True,
                data={"path": str(file_path), "bytes_written": len(content)},
                tier=2,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Error escribiendo archivo: {str(e)}", tier=2)


# --- TIER 3: IRREVERSIBLE O AUTO-REFERENCIAL ---

class SystemRebootTool(BaseTool):
    name = "system_reboot"
    description = "Reinicia el servidor del sistema."
    tier = 3
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        # Comando de reinicio
        res = await run_cmd_async(["sudo", "systemctl", "reboot"])
        res.tier = 3
        return res


class SystemPoweroffTool(BaseTool):
    name = "system_poweroff"
    description = "Apaga el servidor."
    tier = 3
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        res = await run_cmd_async(["sudo", "systemctl", "poweroff"])
        res.tier = 3
        return res
