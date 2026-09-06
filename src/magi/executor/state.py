import asyncio
import os
import platform
import shutil
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import psutil

from magi.core.db import Database


async def run_cmd_async(cmd: List[str], timeout: float = 5.0) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip()
        return None
    except Exception:
        return None


class ServerStateManager:
    def __init__(self, db: Database):
        self.db = db

    async def capture_snapshot(self) -> Dict[str, Any]:
        snapshot_id = f"snap_{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()

        # 1. OS & Kernel
        os_info = {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
            "uptime_seconds": int(time.time() - psutil.boot_time()),
        }

        # Intentar leer /etc/os-release
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        os_info["distribution"] = line.split("=", 1)[1].strip().strip('"')

        # 2. CPU & Memoria
        vmem = psutil.virtual_memory()
        hardware_info = {
            "cpu_cores_logical": psutil.cpu_count(logical=True),
            "cpu_cores_physical": psutil.cpu_count(logical=False),
            "ram_total_mb": vmem.total // (1024 * 1024),
            "ram_available_mb": vmem.available // (1024 * 1024),
            "ram_used_percent": vmem.percent,
        }

        # 3. Discos y particiones
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append(
                    {
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024**3), 2),
                        "used_gb": round(usage.used / (1024**3), 2),
                        "free_gb": round(usage.free / (1024**3), 2),
                        "percent": usage.percent,
                    }
                )
            except Exception:
                pass

        # 4. Servicios systemd activos y fallidos
        services = {"running": [], "failed": []}
        sys_out = await run_cmd_async(
            ["systemctl", "list-units", "--type=service", "--state=running,failed", "--no-legend", "--no-pager"]
        )
        if sys_out:
            for line in sys_out.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    unit_name = parts[0]
                    active_state = parts[2]
                    sub_state = parts[3]
                    if active_state == "failed" or sub_state == "failed":
                        services["failed"].append(unit_name)
                    elif active_state == "active":
                        services["running"].append(unit_name)

        # 5. Docker (si está presente)
        docker_info = {"installed": False, "containers": [], "images": []}
        if shutil.which("docker"):
            docker_info["installed"] = True
            ps_out = await run_cmd_async(["docker", "ps", "-a", "--format", "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"])
            if ps_out:
                for line in ps_out.splitlines():
                    p = line.split("|")
                    if len(p) >= 4:
                        docker_info["containers"].append(
                            {"id": p[0], "name": p[1], "status": p[2], "image": p[3]}
                        )

        # 6. Puertos escuchando (Listening Ports)
        listening_ports = []
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "LISTEN":
                    listening_ports.append(
                        {
                            "ip": conn.laddr.ip,
                            "port": conn.laddr.port,
                            "pid": conn.pid,
                        }
                    )
        except Exception:
            # Si permisos de lectura de conexiones fallan, fallback con ss
            ss_out = await run_cmd_async(["ss", "-tuln"])
            if ss_out:
                for line in ss_out.splitlines()[1:]:
                    listening_ports.append({"raw": line.strip()})

        # 7. Usuarios humanos del sistema
        system_users = []
        if os.path.exists("/etc/passwd"):
            with open("/etc/passwd", "r", encoding="utf-8") as f:
                for line in f:
                    fields = line.strip().split(":")
                    if len(fields) >= 7:
                        uid = int(fields[2]) if fields[2].isdigit() else -1
                        shell = fields[6]
                        if uid >= 1000 and "nologin" not in shell and "false" not in shell:
                            system_users.append({"username": fields[0], "uid": uid, "shell": shell})

        snapshot_data = {
            "id": snapshot_id,
            "timestamp": timestamp,
            "os": os_info,
            "hardware": hardware_info,
            "disks": disks,
            "services": services,
            "docker": docker_info,
            "listening_ports": listening_ports[:30],
            "users": system_users,
        }

        # Guardar en base de datos
        await self.db.save_server_snapshot(snapshot_id, snapshot_data)

        return snapshot_data

    async def get_latest_snapshot(self) -> Optional[Dict[str, Any]]:
        return await self.db.get_latest_server_snapshot()

    def format_summary(self, snapshot: Dict[str, Any]) -> str:
        os_info = snapshot.get("os", {})
        hw = snapshot.get("hardware", {})
        disks = snapshot.get("disks", [])
        services = snapshot.get("services", {})
        docker = snapshot.get("docker", {})

        distro = os_info.get("distribution", os_info.get("system", "Linux"))
        kernel = os_info.get("release", "unknown")
        ram_pct = hw.get("ram_used_percent", 0)
        failed_svcs = len(services.get("failed", []))
        running_svcs = len(services.get("running", []))

        disk_summary = ""
        for d in disks[:2]:
            disk_summary += f"{d['mountpoint']}: {d['percent']}% ({d['free_gb']}GB libres) "

        docker_summary = "No disponible"
        if docker.get("installed"):
            docker_summary = f"{len(docker.get('containers', []))} contenedores"

        return (
            f"🖥️ *SO:* {distro} (Kernel {kernel})\n"
            f"🧠 *RAM:* {ram_pct}% en uso | ⚙️ *Servicios:* {running_svcs} activos, {failed_svcs} fallidos\n"
            f"💾 *Discos:* {disk_summary}\n"
            f"🐳 *Docker:* {docker_summary}"
        )
