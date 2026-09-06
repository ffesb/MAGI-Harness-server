import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SOCKET_PATH = PROJECT_ROOT / "data" / "magi.sock"
DB_PATH = PROJECT_ROOT / "data" / "magi.db"


class MindBox(Static):
    """Caja de pod para cada una de las tres supercomputadoras MAGI."""
    mind_id: reactive[str] = reactive("MIND")
    mind_title: reactive[str] = reactive("MIND • 0")
    vote_state: reactive[str] = reactive("DEFAULT")  # DEFAULT, APPROVE, REJECT, DELIBERATING
    model: reactive[str] = reactive("claude-3.5-sonnet")
    reasoning: reactive[str] = reactive("")
    confidence: reactive[str] = reactive("")
    vote_time: Optional[float] = None

    def watch_vote_state(self, old_val: str, new_val: str) -> None:
        self.remove_class("state-default", "state-approve", "state-reject", "state-deliberating")
        self.add_class(f"state-{new_val.lower()}")

    def reset_to_default(self) -> None:
        self.vote_state = "DEFAULT"
        self.vote_time = None
        self.reasoning = ""
        self.confidence = ""

    def render(self) -> str:
        # En la imagen original de Evangelion el texto es negro audaz sobre fondo cian brillante.
        # Si el voto es de rechazo (fondo rojo), usamos texto blanco de alto contraste.
        is_reject = self.vote_state.upper() == "REJECT"
        fg = "white" if is_reject else "black"

        lines = [
            "",
            f"[bold {fg}]{self.mind_title}[/bold {fg}]",
            f"[{fg}][dim]MODEL:[/dim] {self.model}[/{fg}]",
        ]

        if self.confidence and self.confidence != "---":
            lines.append(f"[{fg}][dim]CONF:[/dim] {self.confidence}[/{fg}]")

        if self.reasoning:
            short_reason = self.reasoning.strip()
            if len(short_reason) > 75:
                short_reason = short_reason[:72] + "..."
            lines.append(f"[italic {fg}]\"{short_reason}\"[/]")
        else:
            lines.append(f"[dim {fg}]● ONLINE[/dim {fg}]")

        return "\n".join(lines)


class NervCodeCard(Static):
    """Panel de código táctico NERV / Evangelion (Esquina superior izquierda de la foto)."""
    def render(self) -> str:
        return (
            "[bold #ff5500]CODE : 666[/]\n"
            "[bold #ff8800]FILE : B_DANANG[/]\n"
            "[#ff9900]EXTENTION : 4096[/]\n"
            "[#ff9900]EX_MODE : DRIVE[/]\n"
            "[#ff9900]PRIORITY : +++[/]\n"
            "[bold #00ff41]═════════════════════[/]"
        )


class KanjiTacticalCard(Static):
    """Insignias tácticas NERV en Kanji (Esquina superior derecha de la foto)."""
    deliberating: reactive[bool] = reactive(False)

    def render(self) -> str:
        repair_border = "#ffaa00" if self.deliberating else "#00ff41"
        repair_text = "#ffaa00" if self.deliberating else "#00ff41"
        return (
            "[bold #ff6600]╔═══════════════╗[/]\n"
            "[bold #ff7700]║   防壁展開    ║[/]\n"
            "[bold #ff6600]╚═══════════════╝[/]\n"
            f"[bold {repair_border}]╔═══════════════╗[/]\n"
            f"[bold {repair_text}]║  修復作業中   ║[/]\n"
            f"[bold {repair_border}]╚═══════════════╝[/]"
        )


class MagiJunction(Static):
    """Nodo central MAGI con líneas de bus naranja que conectan los 3 pods."""
    def render(self) -> str:
        return (
            "[bold #ff6600]───◈───────────────────────[/] "
            "[bold #ff9900 on #1c0a00]   M A G I   [/] "
            "[bold #ff6600]───────────────────────◈───[/]"
        )


class ConsensusBanner(Static):
    status_text: reactive[str] = reactive("🏛️ SISTEMA MAGI NOMINAL — EN ESPERA")

    def render(self) -> str:
        return f"[bold #ff9900 on #100600]═══ {self.status_text} ═══[/]"


class TelemetryPanel(Static):
    cpu: reactive[float] = reactive(0.0)
    ram_pct: reactive[float] = reactive(0.0)
    ram_used_gb: reactive[float] = reactive(0.0)
    ram_total_gb: reactive[float] = reactive(0.0)
    uptime: reactive[str] = reactive("--")
    session_id: reactive[str] = reactive("--")

    def render(self) -> str:
        return (
            f"[bold #ff7700]TELEMETRÍA HOST:[/bold #ff7700]  "
            f"CPU: [bold #00ff41]{self.cpu:.1f}%[/]  │  "
            f"RAM: [bold #00ff41]{self.ram_pct:.1f}%[/] [dim #00ff41]({self.ram_used_gb:.1f}/{self.ram_total_gb:.1f} GB)[/]  │  "
            f"UPTIME: [bold #00ff41]{self.uptime}[/]  │  "
            f"SESIÓN: [bold #00d2ff]{self.session_id}[/]"
        )


class DiskSpacePanel(Static):
    disks_data: reactive[list] = reactive([])

    def render(self) -> str:
        if not self.disks_data:
            return "[bold #ff7700]DISCOS [ESPACIO LIBRE]:[/] [dim]Analizando unidades...[/dim]"

        disk_entries = []
        for d in self.disks_data:
            mount = d["mount"]
            free_gb = d["free_gb"]
            total_gb = d["total_gb"]
            free_pct = d["free_pct"]
            dev = d.get("device", "")

            # Formato claro: MB si es menor a 1GB, GB si es mayor
            free_str = f"{free_gb * 1024:.0f} MB" if free_gb < 1.0 else f"{free_gb:.1f} GB"
            tot_str = f"{total_gb * 1024:.0f} MB" if total_gb < 1.0 else f"{total_gb:.1f} GB"

            pct_color = "#00ff41" if free_pct > 20 else ("#ffaa00" if free_pct > 10 else "#ff1744")

            disk_entries.append(
                f"• [bold #ffffff]{mount}[/] [dim]({dev})[/dim]: "
                f"[{pct_color}]{free_str} LIBRES[/] de {tot_str} ([{pct_color}]{free_pct:.1f}% libre[/])"
            )

        disks_str = "    ".join(disk_entries)
        return f"[bold #ff7700]DISCOS [ESPACIO LIBRE]:[/]  {disks_str}"


class MagiTuiApp(App):
    CSS = """
    Screen {
        background: #020205;
        color: #e0e0e0;
        overflow-y: auto;
    }
    #top_header {
        height: 2;
        dock: top;
        content-align: center middle;
        background: #0d0600;
        color: #ff8800;
        text-style: bold;
        border-bottom: heavy #ff5500;
    }
    #main_content {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    #top_row {
        width: 100%;
        height: 10;
        margin: 1 0 0 0;
    }
    #code_card {
        width: 25;
        height: 100%;
        padding: 0 1;
        border: heavy #ff5500;
        background: #070300;
    }
    #balthasar_container {
        width: 1fr;
        height: 100%;
        padding: 0 1;
    }
    #kanji_card {
        width: 21;
        height: 100%;
        padding: 0 1;
        border: heavy #ff5500;
        background: #070300;
        content-align: center middle;
        text-align: center;
    }
    #magi_junction {
        width: 100%;
        height: 3;
        content-align: center middle;
        text-align: center;
        margin: 0;
    }
    #bottom_row {
        width: 100%;
        height: 10;
        margin: 0 0 1 0;
    }
    .bottom_pod_container {
        width: 1fr;
        height: 100%;
        padding: 0 1;
    }
    MindBox {
        width: 100%;
        height: 100%;
        border: heavy #ff5500;
        background: #00c0f0;
        color: #000000;
        text-align: center;
        content-align: center middle;
        padding: 0 1;
    }
    MindBox.state-default {
        background: #00c0f0;
        color: #000000;
    }
    MindBox.state-approve {
        background: #00e676;
        color: #000000;
    }
    MindBox.state-reject {
        background: #ff1744;
        color: #ffffff;
    }
    MindBox.state-deliberating {
        background: #ffaa00;
        color: #000000;
    }
    #consensus_banner {
        width: 100%;
        height: 3;
        content-align: center middle;
        text-align: center;
        margin: 0 0 1 0;
        border: round #ff5500;
        background: #0a0400;
    }
    #telemetry_panel {
        width: 100%;
        height: 3;
        padding: 0 1;
        border: round #ff5500;
        background: #080300;
        content-align: left middle;
        margin: 0 0 1 0;
    }
    #disk_panel {
        width: 100%;
        height: 3;
        padding: 0 1;
        border: round #ff5500;
        background: #080300;
        content-align: left middle;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, socket_path: Path = SOCKET_PATH):
        super().__init__()
        self.socket_path = socket_path
        self._current_session_id: str = "--"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("SUPERORDENADOR MAGI 3.0 — SUPERVISIÓN AUTÓNOMA DEL SERVIDOR", id="top_header")
        with Vertical(id="main_content"):
            with Horizontal(id="top_row"):
                yield NervCodeCard(id="code_card")
                with Container(id="balthasar_container"):
                    yield MindBox(id="mind_balthasar")
                yield KanjiTacticalCard(id="kanji_card")

            yield MagiJunction(id="magi_junction")

            with Horizontal(id="bottom_row"):
                with Container(classes="bottom_pod_container"):
                    yield MindBox(id="mind_casper")
                with Container(classes="bottom_pod_container"):
                    yield MindBox(id="mind_melchior")

            yield ConsensusBanner(id="consensus_banner")
            yield TelemetryPanel(id="telemetry_panel")
            yield DiskSpacePanel(id="disk_panel")
        yield Footer()

    async def on_mount(self) -> None:
        balthasar = self.query_one("#mind_balthasar", MindBox)
        balthasar.mind_id = "BALTHASAR"
        balthasar.mind_title = "BALTHASAR • 2"

        casper = self.query_one("#mind_casper", MindBox)
        casper.mind_id = "CASPER"
        casper.mind_title = "CASPER • 3"

        melchior = self.query_one("#mind_melchior", MindBox)
        melchior.mind_id = "MELCHIOR"
        melchior.mind_title = "MELCHIOR • 1"

        try:
            from magi.core.config import load_settings
            settings = load_settings()
            if "BALTHASAR" in settings.minds:
                balthasar.model = settings.minds["BALTHASAR"].model
            if "CASPER" in settings.minds:
                casper.model = settings.minds["CASPER"].model
            if "MELCHIOR" in settings.minds:
                melchior.model = settings.minds["MELCHIOR"].model
        except Exception:
            pass

        # Lectura inicial de sesión real de la BD
        self._check_db_session()

        # Actualizar telemetría local de inmediato y programar cada segundo
        self._refresh_local_metrics()
        self.set_interval(1.0, self._refresh_local_metrics)

        # Worker de conexión IPC persistente
        self.run_worker(self._listen_ipc(), exclusive=True)

    def _check_db_session(self) -> None:
        """Consulta la base de datos de forma segura en modo sólo lectura."""
        try:
            if DB_PATH.exists():
                with sqlite3.connect(f"file:{DB_PATH.resolve()}?mode=ro", uri=True) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM sessions WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        new_sess = row[0]
                        if self._current_session_id != new_sess and self._current_session_id != "--":
                            self._reset_all_minds()
                        self._current_session_id = new_sess
                        telem = self.query_one("#telemetry_panel", TelemetryPanel)
                        telem.session_id = new_sess
        except Exception:
            pass

    def _reset_all_minds(self) -> None:
        """Reinicia todos los pods al color default (#00c0f0)."""
        for m_id in ["#mind_balthasar", "#mind_casper", "#mind_melchior"]:
            try:
                box = self.query_one(m_id, MindBox)
                box.reset_to_default()
            except Exception:
                pass
        try:
            kanji = self.query_one("#kanji_card", KanjiTacticalCard)
            kanji.deliberating = False
        except Exception:
            pass

    def _refresh_local_metrics(self) -> None:
        """Lee datos reales del host usando psutil y gestiona el temporizador de 3 minutos."""
        now = time.time()
        minds = [
            self.query_one("#mind_balthasar", MindBox),
            self.query_one("#mind_casper", MindBox),
            self.query_one("#mind_melchior", MindBox),
        ]

        # 1. Temporizador de 3 minutos (180s) para volver al color default
        for box in minds:
            if box.vote_state in ("APPROVE", "REJECT") and box.vote_time is not None:
                if (now - box.vote_time) >= 180:
                    box.reset_to_default()

        # 2. Verificar sesión activa periódicamente
        self._check_db_session()

        # 3. Telemetría de host en tiempo real
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            mem_used_gb = mem.used / (1024 ** 3)
            mem_total_gb = mem.total / (1024 ** 3)

            boot_t = psutil.boot_time()
            uptime_s = int(now - boot_t)
            h, r = divmod(uptime_s, 3600)
            m, s = divmod(r, 60)
            uptime_str = f"{h}h {m}m {s}s"

            telem = self.query_one("#telemetry_panel", TelemetryPanel)
            telem.cpu = cpu
            telem.ram_pct = mem.percent
            telem.ram_used_gb = mem_used_gb
            telem.ram_total_gb = mem_total_gb
            telem.uptime = uptime_str
            if self._current_session_id != "--":
                telem.session_id = self._current_session_id
        except Exception:
            pass

        # 4. Apartado de Discos (Espacio Libre real)
        try:
            disks_by_dev = {}
            for part in psutil.disk_partitions(all=False):
                if part.fstype in ("", "squashfs", "tmpfs", "overlay"):
                    continue
                if part.device not in disks_by_dev:
                    try:
                        u = psutil.disk_usage(part.mountpoint)
                        disks_by_dev[part.device] = {
                            "mount": part.mountpoint,
                            "device": part.device,
                            "free_gb": u.free / (1024 ** 3),
                            "total_gb": u.total / (1024 ** 3),
                            "free_pct": (u.free / u.total) * 100 if u.total > 0 else 0,
                        }
                    except Exception:
                        pass
            disk_panel = self.query_one("#disk_panel", DiskSpacePanel)
            disk_panel.disks_data = list(disks_by_dev.values())
        except Exception:
            pass

    async def _listen_ipc(self) -> None:
        consensus = self.query_one("#consensus_banner", ConsensusBanner)
        kanji = self.query_one("#kanji_card", KanjiTacticalCard)
        balthasar = self.query_one("#mind_balthasar", MindBox)
        casper = self.query_one("#mind_casper", MindBox)
        melchior = self.query_one("#mind_melchior", MindBox)

        minds_map = {
            "BALTHASAR": balthasar,
            "CASPER": casper,
            "MELCHIOR": melchior,
        }

        while True:
            try:
                if not self.socket_path.exists():
                    consensus.status_text = "⚠️ DAEMON DESCONECTADO (Socket no disponible en data/magi.sock)"
                    await asyncio.sleep(2)
                    continue

                reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
                consensus.status_text = "🟢 CONECTADO AL DAEMON (Modo Solo Lectura)"

                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode("utf-8"))
                        e_type = event.get("type")
                        e_data = event.get("data", {})

                        if e_type == "minds_sync":
                            for m_name, m_model in e_data.items():
                                k = m_name.upper()
                                if k in minds_map:
                                    minds_map[k].model = m_model

                        elif e_type == "session_new":
                            # Nueva sesión iniciada: restablecer de inmediato cuadros al color default
                            new_sid = e_data.get("session_id", "--")
                            self._current_session_id = new_sid
                            self._reset_all_minds()
                            consensus.status_text = f"✨ NUEVA SESIÓN INICIADA ({new_sid})"

                        elif e_type == "telemetry":
                            sess = e_data.get("session_id")
                            if sess and sess != "--" and sess != "activa":
                                if self._current_session_id != sess and self._current_session_id != "--":
                                    self._reset_all_minds()
                                self._current_session_id = sess

                        elif e_type == "mind_state":
                            m_name = e_data.get("mind", "").upper()
                            if m_name in minds_map:
                                box = minds_map[m_name]
                                raw_state = e_data.get("state", "IDLE").upper()
                                if raw_state in ("APPROVE", "REJECT"):
                                    box.vote_state = raw_state
                                    box.vote_time = time.time()
                                elif raw_state == "DELIBERATING":
                                    box.vote_state = "DELIBERATING"
                                    kanji.deliberating = True
                                else:
                                    box.vote_state = "DEFAULT"

                                if "model" in e_data:
                                    box.model = e_data["model"]
                                if "reasoning" in e_data:
                                    box.reasoning = e_data["reasoning"]
                                if "confidence" in e_data:
                                    box.confidence = f"{int(float(e_data['confidence']) * 100)}%"

                        elif e_type == "consensus":
                            c_text = e_data.get("text", "DELIBERACIÓN FINALIZADA")
                            consensus.status_text = c_text
                            kanji.deliberating = False

                    except Exception:
                        pass
            except Exception:
                consensus.status_text = "⏳ CONECTANDO AL DAEMON DE MAGI..."
                await asyncio.sleep(2)


def main():
    app = MagiTuiApp()
    app.run()


if __name__ == "__main__":
    main()
