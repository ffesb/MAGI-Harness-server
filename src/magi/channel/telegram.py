import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from magi.channel.base import ChannelAdapter, InlineButton
from magi.core.config import (
    Settings,
    update_mind_model,
    update_mind_system_prompt,
    update_openrouter_api_key,
)
from magi.core.audit import AuditLogger
from magi.core.provider import OpenRouterClient
from magi.council.minds import CouncilManager
from magi.session.manager import SessionManager
from magi.executor.state import ServerStateManager
from magi.scheduler.cron import CronScheduler


class RateLimiter:
    def __init__(self, max_requests: int = 15, window_seconds: float = 10.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[int, List[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        timestamps = self._requests.setdefault(user_id, [])
        # Filtrar timestamps dentro de la ventana
        self._requests[user_id] = [t for t in timestamps if now - t < self.window_seconds]
        if len(self._requests[user_id]) >= self.max_requests:
            return False
        self._requests[user_id].append(now)
        return True


class TelegramAdapter(ChannelAdapter):
    def __init__(
        self,
        settings: Settings,
        audit: AuditLogger,
        session_manager: SessionManager,
        server_state: Optional[ServerStateManager] = None,
        provider: Optional[OpenRouterClient] = None,
        executor_agent: Optional[Any] = None,
        council: Optional[CouncilManager] = None,
        scheduler: Optional[CronScheduler] = None,
        ipc: Optional[Any] = None,
        on_user_message: Optional[
            Callable[[int, str, Dict[str, Any]], Coroutine[Any, Any, None]]
        ] = None,
    ):
        self.settings = settings
        self.audit = audit
        self.session_manager = session_manager
        self.server_state = server_state
        self.provider = provider
        self.executor_agent = executor_agent
        self.council = council
        self.scheduler = scheduler
        self.ipc = ipc
        self.on_user_message = on_user_message
        self.rate_limiter = RateLimiter(max_requests=20, window_seconds=10.0)
        self.start_time = time.time()
        self.app: Optional[Application] = None
        self._is_running = False
        self._pending_personality: Dict[int, str] = {}

    def _convert_buttons(
        self, buttons: Optional[List[List[InlineButton]]]
    ) -> Optional[InlineKeyboardMarkup]:
        if not buttons:
            return None
        keyboard = []
        for row in buttons:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=btn.text, callback_data=btn.callback_data
                    )
                    for btn in row
                ]
            )
        return InlineKeyboardMarkup(keyboard)

    def _check_security(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False

        user_id = user.id
        if not self.settings.is_user_allowed(user_id):
            # Silencio absoluto al exterior, registro en auditoría de seguridad
            text_preview = ""
            if update.message and update.message.text:
                text_preview = update.message.text[:100]
            elif update.callback_query and update.callback_query.data:
                text_preview = update.callback_query.data

            asyncio.create_task(
                self.audit.log_event(
                    actor=f"unauthorized_user:{user_id}",
                    action="UNAUTHORIZED_ACCESS_ATTEMPT",
                    tier=3,
                    detail={
                        "user_id": user_id,
                        "username": user.username,
                        "first_name": user.first_name,
                        "content_preview": text_preview,
                    },
                )
            )
            return False

        if not self.rate_limiter.is_allowed(user_id):
            # Rate limit excedido
            asyncio.create_task(
                self.audit.log_event(
                    actor=f"user:{user_id}",
                    action="RATE_LIMIT_EXCEEDED",
                    tier=1,
                    detail={"user_id": user_id},
                )
            )
            return False

        return True

    async def start(self) -> None:
        if not self.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN no está configurado.")

        self.app = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .build()
        )

        # Comandos en inglés (principales) y aliases
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("sessiones", self._cmd_sessions))
        self.app.add_handler(CommandHandler("del", self._cmd_del))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("audit", self._cmd_audit))
        self.app.add_handler(CommandHandler("auditoria", self._cmd_audit))
        self.app.add_handler(CommandHandler("rescan", self._cmd_rescan))
        self.app.add_handler(CommandHandler("provider", self._cmd_provider))
        self.app.add_handler(CommandHandler("model_executor", self._cmd_model_executor))
        self.app.add_handler(CommandHandler("model_melchior", self._cmd_model_melchior))
        self.app.add_handler(CommandHandler("model_balthasar", self._cmd_model_balthasar))
        self.app.add_handler(CommandHandler("model_casper", self._cmd_model_casper))
        self.app.add_handler(CommandHandler("personality_melchior", self._cmd_personality_melchior))
        self.app.add_handler(CommandHandler("personality_balthasar", self._cmd_personality_balthasar))
        self.app.add_handler(CommandHandler("personality_casper", self._cmd_personality_casper))
        self.app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        self.app.add_handler(CommandHandler("disable", self._cmd_disable))
        self.app.add_handler(CommandHandler("enable", self._cmd_enable))
        self.app.add_handler(CommandHandler("log", self._cmd_log))
        self.app.add_handler(CommandHandler("cronjobs", self._cmd_cronjobs))
        self.app.add_handler(CommandHandler("cronjob", self._cmd_cronjob))

        # Callback queries (botones interactivos)
        self.app.add_handler(CallbackQueryHandler(self._handle_callback_query))

        # Mensajes de texto libre
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)
        )

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        self._is_running = True

        await self.audit.log_event(
            actor="daemon",
            action="TELEGRAM_ADAPTER_STARTED",
            tier=0,
            detail={"bot": "naoko_magi_bot"},
        )

    async def stop(self) -> None:
        if self.app and self._is_running:
            self._is_running = False
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            await self.audit.log_event(
                actor="daemon",
                action="TELEGRAM_ADAPTER_STOPPED",
                tier=0,
            )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[InlineButton]]] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> Optional[int]:
        if not self.app or not self.app.bot:
            return None
        reply_markup = self._convert_buttons(buttons)
        try:
            msg = await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return msg.message_id
        except BadRequest as e:
            # Fallback sin markdown si falla el parseo
            msg = await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=None,
            )
            return msg.message_id
        except Exception as e:
            return None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: Optional[List[List[InlineButton]]] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> bool:
        if not self.app or not self.app.bot:
            return False
        reply_markup = self._convert_buttons(buttons)
        try:
            await self.app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return True
        except BadRequest:
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=None,
                )
                return True
            except Exception:
                return False
        except Exception:
            return False

    async def send_file(
        self,
        chat_id: int,
        path: str,
        caption: Optional[str] = None,
    ) -> bool:
        if not self.app or not self.app.bot:
            return False
        try:
            with open(path, "rb") as f:
                await self.app.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )
            return True
        except Exception:
            return False

    # --- HANDLERS DE COMANDOS ---

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        session = await self.session_manager.get_or_create_active_session(chat_id)

        welcome_text = (
            "🏛️ *MAGI SYSTEM — NERV SERVER HARNESS*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Sistema en línea y operando en modo nominal.\n\n"
            f"🔹 *Sesión Activa:* `{session['id']}`\n"
            f"🔹 *Título:* {session['title']}\n"
            f"🔹 *Núcleos MAGI:* MELCHIOR-1 | BALTHASAR-2 | CASPER-3\n\n"
            "Usa `/help` para consultar el manual de operaciones y comandos."
        )
        await self.send_message(chat_id, welcome_text)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        help_text = (
            "🏛️ *MAGI SYSTEM COMMAND MANUAL*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 *Session Management*\n"
            "• `/start` — Welcome status and active session info\n"
            "• `/new [title]` — Create and switch to a fresh session (freezes prompts)\n"
            "• `/sessions` — Interactive list to switch between sessions\n"
            "• `/del <session_id>` — Delete a session and its history\n\n"
            "🔹 *System Telemetry & State*\n"
            "• `/status` — Live host telemetry (CPU, RAM, Disk, Uptime, MAGI status)\n"
            "• `/rescan` — Regenerate server state snapshot (`.init`) on demand\n"
            "• `/audit` — View recent security events, actions, and audit trail\n"
            "• `/log <action_id>` — Detailed MAGI voting breakdown and mind reasoning\n\n"
            "🔹 *MAGI Oversight & Policies*\n"
            "• `/disable` — Bypass MAGI council for Tier 0-1 (Tiers 2-3 remain protected)\n"
            "• `/enable` — Reactivate full MAGI council oversight\n"
            "• Send `CONFIRMO` — Human authorization for pending Tier 2 & 3 critical actions\n\n"
            "🔹 *AI Provider & Models*\n"
            "• `/provider [key]` — Configure or verify OpenRouter API Key\n"
            "• `/model_executor [model]` — Set model for AI Executor\n"
            "• `/model_melchior [model]` — Set model for MELCHIOR-1 (Scientist)\n"
            "• `/model_balthasar [model]` — Set model for BALTHASAR-2 (Mother)\n"
            "• `/model_casper [model]` — Set model for CASPER-3 (Woman)\n"
            "  _(Aliases `/model-executor`, `/model-melchior`, `/model-balthasar`, `/model-casper` supported)_\n\n"
            "🔹 *Mind Personalities & Priorities*\n"
            "• `/personality_melchior` — Set permanent system prompt for MELCHIOR-1\n"
            "• `/personality_balthasar` — Set permanent system prompt for BALTHASAR-2\n"
            "• `/personality_casper` — Set permanent system prompt for CASPER-3\n"
            "• `/cancel` — Cancel pending prompt input or confirmation\n"
            "  _(Aliases `/personality-melchior`, `/personality-balthasar`, `/personality-casper` supported)_\n\n"
            "🔹 *Scheduled Tasks (Cron)*\n"
            "• `/cronjobs` — List all active internal scheduled tasks\n"
            "• `/cronjob add <cron_expr> <prompt>` — Schedule an autonomous periodic task\n"
            "• `/cronjob del <job_id>` — Delete a scheduled task\n\n"
            "💡 _Send any free-form text message to dispatch a diagnostic or execution task to the AI Executor._"
        )
        await self.send_message(chat_id, help_text)

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        custom_title = " ".join(context.args) if context.args else None
        session = await self.session_manager.create_new_session(
            chat_id=chat_id, title=custom_title
        )
        if self.ipc:
            try:
                asyncio.create_task(
                    self.ipc.broadcast_event("session_new", {"session_id": session["id"]})
                )
            except Exception:
                pass
        text = (
            "✨ *NUEVA SESIÓN INICIALIZADA*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *ID:* `{session['id']}`\n"
            f"🏷️ *Título:* {session['title']}\n"
            "🔒 *Prompts:* Congelados y preservados para auditoría.\n\n"
            "Lista para recibir instrucciones."
        )
        await self.send_message(chat_id, text)

    async def _cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        sessions = await self.session_manager.list_sessions(chat_id, limit=8)
        if not sessions:
            await self.send_message(chat_id, "No existen sesiones activas. Crea una con `/new`.")
            return

        text = "🗂️ *REGISTRO DE SESIONES*\n━━━━━━━━━━━━━━━━━━━━━\n"
        buttons = []

        for s in sessions:
            status_emoji = "🟢 [ACTIVA]" if s["is_active"] else "⚪"
            text += f"{status_emoji} `{s['id']}` — *{s['title']}*\n"
            row = []
            if not s["is_active"]:
                row.append(
                    InlineButton(
                        text=f"Activar {s['id']}",
                        callback_data=f"sess_switch:{s['id']}",
                    )
                )
            row.append(
                InlineButton(
                    text=f"🗑️ Borrar",
                    callback_data=f"sess_del_ask:{s['id']}",
                )
            )
            buttons.append(row)

        await self.send_message(chat_id, text, buttons=buttons)

    async def _cmd_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if not context.args:
            await self.send_message(chat_id, "Uso: `/del <id_sesion>` (ej. `/del sess_a1b2c3d4`)")
            return
        session_id = context.args[0].strip()
        confirm_buttons = [
            [
                InlineButton(
                    text="⚠️ CONFIRMAR BORRADO",
                    callback_data=f"sess_del_confirm:{session_id}",
                ),
                InlineButton(
                    text="Cancelar",
                    callback_data="sess_del_cancel",
                ),
            ]
        ]
        await self.send_message(
            chat_id,
            f"¿Deseas eliminar permanentemente la sesión `{session_id}` y todo su historial?",
            buttons=confirm_buttons,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        session = await self.session_manager.get_or_create_active_session(chat_id)

        uptime_secs = int(time.time() - self.start_time)
        hours, rem = divmod(uptime_secs, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours}h {mins}m {secs}s"

        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        status_text = (
            "⚙️ *ESTADO DEL SISTEMA MAGI*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *Uptime Daemon:* `{uptime_str}`\n"
            f"💻 *CPU:* `{cpu_pct}%`\n"
            f"🧠 *RAM:* `{mem.percent}%` ({mem.used // (1024*1024)}MB / {mem.total // (1024*1024)}MB)\n"
            f"💾 *Disco (/):* `{disk.percent}%` libre: {disk.free // (1024*1024*1024)}GB\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 *Sesión Activa:* `{session['id']}`\n"
            "🏛️ *Comité MAGI:* Habilitado (MELCHIOR / BALTHASAR / CASPER)\n"
            "🛡️ *Allowlist:* Activa (1 operador autorizado)\n"
            "🔍 *Motor de Búsqueda:* SearXNG conectado"
        )
        await self.send_message(chat_id, status_text)

    async def _cmd_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        logs = await self.audit.get_recent_logs(limit=8)
        if not logs:
            await self.send_message(chat_id, "No hay eventos en el registro de auditoría aún.")
            return

        text = "📜 *ÚLTIMOS EVENTOS DE AUDITORÍA*\n━━━━━━━━━━━━━━━━━━━━━\n"
        for log in logs:
            created_dt = log["created_at"].split("T")
            time_part = created_dt[1][:8] if len(created_dt) > 1 else log["created_at"]
            tier_str = f" [T{log['tier']}]" if log["tier"] is not None else ""
            text += f"`{time_part}` *{log['action']}*{tier_str}\nActor: `{log['actor']}`\n\n"

        await self.send_message(chat_id, text)

    async def _cmd_rescan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if not self.server_state:
            await self.send_message(chat_id, "❌ Módulo de estado del servidor no inicializado.")
            return

        wait_msg_id = await self.send_message(chat_id, "🔄 *MAGI:* Escaneando topología del sistema operativo y servicios...")
        snapshot = await self.server_state.capture_snapshot()
        summary = self.server_state.format_summary(snapshot)

        text = (
            "📡 *MAPA DE ESTADO ACTUALIZADO (.init)*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Snapshot:* `{snapshot['id']}`\n\n"
            f"{summary}\n\n"
            "✅ Snapshot persistido y vinculado para las decisiones de la Ejecutora y MAGI."
        )

        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="SERVER_RESCANNED",
            tier=0,
            detail={"snapshot_id": snapshot["id"]},
        )

        if wait_msg_id:
            await self.edit_message(chat_id, wait_msg_id, text)
        else:
            await self.send_message(chat_id, text)

    # --- COMANDOS DE MODELOS Y PROVIDER ---

    async def _cmd_provider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id

        if not context.args:
            current_status = "🟢 Conectado" if self.settings.openrouter_api_key else "🔴 No configurado"
            key_preview = (
                f"`{self.settings.openrouter_api_key[:8]}...{self.settings.openrouter_api_key[-4:]}`"
                if self.settings.openrouter_api_key
                else "_Ninguna_"
            )
            text = (
                "🔌 *ESTADO DEL PROVEEDOR LLM*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "• *Proveedor:* OpenRouter\n"
                f"• *Estado:* {current_status}\n"
                f"• *API Key:* {key_preview}\n\n"
                "Para actualizar o añadir tu key:\n"
                "`/provider <tu_openrouter_api_key>`"
            )
            await self.send_message(chat_id, text)
            return

        new_key = context.args[0].strip()
        update_openrouter_api_key(self.settings, new_key)
        if self.provider:
            self.provider.set_api_key(new_key)

        # Probar conexión con OpenRouter
        wait_msg = await self.send_message(chat_id, "⏳ Verificando credenciales con OpenRouter...")
        try:
            if self.provider:
                models = await self.provider.list_models()
                success_text = (
                    "✅ *OPENROUTER CONFIGURADO CON ÉXITO*\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• Conexión establecida.\n"
                    f"• {len(models)} modelos disponibles en la plataforma.\n\n"
                    "Ahora puedes seleccionar modelos con:\n"
                    "• `/model_executor`\n"
                    "• `/model_melchior`\n"
                    "• `/model_balthasar`\n"
                    "• `/model_casper`"
                )
                if wait_msg:
                    await self.edit_message(chat_id, wait_msg, success_text)
                else:
                    await self.send_message(chat_id, success_text)
                await self.audit.log_event(
                    actor=f"user:{chat_id}",
                    action="PROVIDER_KEY_UPDATED",
                    tier=3,
                    detail={"status": "verified"},
                )
        except Exception as e:
            err_text = f"⚠️ Key guardada pero falló la prueba de conexión: {str(e)}"
            if wait_msg:
                await self.edit_message(chat_id, wait_msg, err_text)
            else:
                await self.send_message(chat_id, err_text)

    async def _prompt_model_selection(
        self, chat_id: int, mind_name: str, custom_model: Optional[str] = None
    ) -> None:
        mind_key = mind_name.upper()
        if custom_model:
            # Asignación directa por comando
            success = update_mind_model(self.settings, mind_key, custom_model)
            if success:
                if self.ipc:
                    try:
                        minds_models = {
                            m_name: cfg.model for m_name, cfg in self.settings.minds.items()
                        }
                        asyncio.create_task(self.ipc.broadcast_event("minds_sync", minds_models))
                        asyncio.create_task(
                            self.ipc.broadcast_event(
                                "mind_state",
                                {"mind": mind_key, "model": custom_model, "state": "READY"},
                            )
                        )
                    except Exception:
                        pass

                await self.send_message(
                    chat_id,
                    f"✅ *{mind_key}* configurado para usar: `{custom_model}`",
                )
                await self.audit.log_event(
                    actor=f"user:{chat_id}",
                    action="MIND_MODEL_UPDATED",
                    tier=1,
                    detail={"mind": mind_key, "model": custom_model},
                )
            else:
                await self.send_message(chat_id, f"❌ Mente '{mind_key}' no reconocida.")
            return

        current_model = (
            self.settings.minds[mind_key].model
            if mind_key in self.settings.minds
            else "no configurado"
        )

        curated = [
            ("Claude 3.5 Sonnet", "anthropic/claude-3.5-sonnet"),
            ("GPT-4o", "openai/gpt-4o"),
            ("Gemini 2.5 Pro", "google/gemini-2.5-pro"),
            ("DeepSeek V3", "deepseek/deepseek-chat"),
            ("DeepSeek R1", "deepseek/deepseek-r1"),
            ("Llama 3.3 70B", "meta-llama/llama-3.3-70b-instruct"),
        ]

        text = (
            f"🧠 *SELECCIÓN DE MODELO PARA {mind_key}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Modelo Actual:* `{current_model}`\n\n"
            "Elige una opción recomendada o escribe:\n"
            f"`/model_{mind_key.lower()} <nombre_del_modelo>`"
        )

        buttons = []
        for label, m_id in curated:
            buttons.append(
                [
                    InlineButton(
                        text=label,
                        callback_data=f"mdl_set:{mind_key}:{m_id}",
                    )
                ]
            )

        await self.send_message(chat_id, text, buttons=buttons)

    async def _cmd_model_executor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].strip() if context.args else None
        await self._prompt_model_selection(chat_id, "EXECUTOR", arg)

    async def _cmd_model_melchior(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].strip() if context.args else None
        await self._prompt_model_selection(chat_id, "MELCHIOR", arg)

    async def _cmd_model_balthasar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].strip() if context.args else None
        await self._prompt_model_selection(chat_id, "BALTHASAR", arg)

    async def _cmd_model_casper(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        arg = context.args[0].strip() if context.args else None
        await self._prompt_model_selection(chat_id, "CASPER", arg)

    # --- PERSONALIDAD Y SYSTEM PROMPTS DE MENTES ---

    async def _cmd_personality_melchior(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        await self._start_personality_edit(update.effective_chat.id, "MELCHIOR")

    async def _cmd_personality_balthasar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        await self._start_personality_edit(update.effective_chat.id, "BALTHASAR")

    async def _cmd_personality_casper(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        await self._start_personality_edit(update.effective_chat.id, "CASPER")

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if chat_id in self._pending_personality:
            mind = self._pending_personality.pop(chat_id)
            await self.send_message(chat_id, f"❌ Personality edit for *{mind}* cancelled.")
        else:
            await self.send_message(chat_id, "ℹ️ No pending action to cancel.")

    async def _start_personality_edit(self, chat_id: int, mind_name: str) -> None:
        mind_key = mind_name.upper()
        if mind_key not in ("MELCHIOR", "BALTHASAR", "CASPER"):
            await self.send_message(chat_id, f"❌ Invalid mind: `{mind_key}`. Choose MELCHIOR, BALTHASAR, or CASPER.")
            return

        mind_cfg = self.settings.minds.get(mind_key)
        d_name = mind_cfg.display_name if mind_cfg else mind_key
        curr_prompt = mind_cfg.system_prompt if mind_cfg else "(empty)"

        self._pending_personality[chat_id] = mind_key

        preview = curr_prompt[:320] + ("..." if len(curr_prompt) > 320 else "")
        msg = (
            f"🧠 *CONFIGURE PERSONALITY & SYSTEM PROMPT: {d_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current prompt snippet:\n"
            f"```\n{preview}\n```\n\n"
            "👉 *Send the new system prompt and decision priorities in your NEXT message.*\n"
            "This change is permanent, saved to disk, and persists across sessions.\n\n"
            "💡 Type `/cancel` to abort."
        )
        await self.send_message(chat_id, msg)

    # --- CONTROL DE MAGI Y POLÍTICAS ---

    async def _cmd_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if self.council:
            self.council.set_magi_enabled(False)
        await self.send_message(
            chat_id,
            "⏸️ *COMITÉ MAGI DESACTIVADO (/disable)*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "• Las acciones *Tier 0 y Tier 1* se ejecutarán sin votación previa.\n"
            "• ⚠️ *IMPORTANTE:* Las acciones *Tier 2 y Tier 3* (críticas e irreversibles) "
            "*seguirán exigiendo confirmación humana explícita* por política inviolable.",
        )
        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="MAGI_DISABLED",
            tier=1,
            detail={"mode": "bypassed_tier_0_1"},
        )

    async def _cmd_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if self.council:
            self.council.set_magi_enabled(True)
        await self.send_message(
            chat_id,
            "▶️ *COMITÉ MAGI REACTIVADO (/enable)*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "MELCHIOR-1, BALTHASAR-2 y CASPER-3 auditarán y votarán todas las operaciones mutantes.",
        )
        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="MAGI_ENABLED",
            tier=0,
            detail={"mode": "full_deliberation"},
        )

    async def _cmd_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if not context.args:
            await self.send_message(chat_id, "Uso: `/log <id_de_accion>` (ej. `/log act_a1b2c3d4`)")
            return

        action_id = context.args[0].strip()
        detail = await self.audit.get_action_detail(action_id)
        if not detail:
            await self.send_message(chat_id, f"❌ No se encontró registro para la acción `{action_id}`.")
            return

        tool_name = detail.get("tool_name", "desconocida")
        tier = detail.get("tier", 0)
        status = detail.get("status", "desconocido")
        votes = detail.get("votes", [])

        status_emoji = {
            "approved": "✅ APROBADA",
            "executed": "🚀 EJECUTADA",
            "rejected": "❌ RECHAZADA",
            "awaiting_human_confirmation": "⏳ ESPERANDO CONFIRMACIÓN HUMANA",
            "deliberating": "🏛️ DELIBERANDO",
            "failed": "💥 FALLIDA",
        }.get(status, status.upper())

        text = (
            f"🏛️ *EXPEDIENTE DE DELIBERACIÓN MAGI*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Acción:* `{action_id}`\n"
            f"🛠️ *Herramienta:* `{tool_name}` (Tier {tier})\n"
            f"📊 *Estado:* {status_emoji}\n\n"
        )

        if not votes:
            text += "_No se registraron votos de MAGI para esta acción (ej. Tier 0 o MAGI deshabilitado)._"
        else:
            vote_emojis = {"approve": "🟢 APROBADO", "reject": "🔴 RECHAZADO", "abstain": "⚪ ABSTENCIÓN"}
            for v in votes:
                m_name = v.get("mind_name", "")
                m_vote = v.get("vote", "abstain")
                v_emoji = vote_emojis.get(m_vote, m_vote.upper())
                conf = int(v.get("confidence", 0) * 100)
                reason = v.get("reasoning", "")
                lat = v.get("latency_ms", 0)
                text += (
                    f"*{m_name}* ➔ {v_emoji} ({conf}% certeza, {lat}ms)\n"
                    f"_{reason}_\n\n"
                )

        await self.send_message(chat_id, text)

    # --- TAREAS PROGRAMADAS (CRONJOBS) ---

    async def _cmd_cronjobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if not self.scheduler:
            await self.send_message(chat_id, "❌ Scheduler de tareas no disponible.")
            return

        jobs = await self.scheduler.list_jobs(chat_id)
        if not jobs:
            await self.send_message(
                chat_id,
                "ℹ️ No hay tareas programadas activas.\n"
                "Para crear una:\n`/cronjob add <expresión_cron> <instrucción>`\n"
                "Ej: `/cronjob add 0 9 * * * Reporte de salud del servidor`",
            )
            return

        text = "⏰ *TAREAS PROGRAMADAS ACTIVAS*\n━━━━━━━━━━━━━━━━━━━━━\n"
        buttons = []
        for j in jobs:
            j_id = j["id"]
            expr = j["expression"]
            prompt = j.get("prompt", "")[:40]
            nxt = j.get("next_run", "desconocido")
            nxt_part = nxt.split("T")[1][:5] if "T" in nxt else nxt
            text += f"• `{j_id}` | `{expr}`\n  Prompt: \"{prompt}\"\n  Próxima ejec: `{nxt_part}`\n\n"
            buttons.append([InlineButton(text=f"🗑️ Eliminar {j_id}", callback_data=f"cron_del:{j_id}")])

        await self.send_message(chat_id, text, buttons=buttons)

    async def _cmd_cronjob(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        if not self.scheduler:
            await self.send_message(chat_id, "❌ Scheduler no inicializado.")
            return

        if not context.args or len(context.args) < 2:
            await self.send_message(
                chat_id,
                "Uso de `/cronjob`:\n"
                "• `/cronjob del <id>` — Elimina la tarea programada\n"
                "• `/cronjob add <m h dom mon dow> <instrucción>` — Crea una nueva tarea\n\n"
                "Ej: `/cronjob add 0 12 * * * Diagnóstico de memoria RAM`",
            )
            return

        subcmd = context.args[0].lower()
        if subcmd == "del":
            job_id = context.args[1].strip()
            success = await self.scheduler.delete_job(job_id, chat_id)
            if success:
                await self.send_message(chat_id, f"🗑️ Tarea programada `{job_id}` eliminada.")
            else:
                await self.send_message(chat_id, f"❌ No se encontró la tarea `{job_id}`.")

        elif subcmd == "add":
            if len(context.args) < 7:
                await self.send_message(
                    chat_id,
                    "⚠️ La expresión cron requiere 5 campos seguidos del prompt.\n"
                    "Formato: `/cronjob add <min> <hora> <dia_mes> <mes> <dia_sem> <instrucción>`\n"
                    "Ejemplo: `/cronjob add 0 */6 * * * Chequeo de contenedores`",
                )
                return
            expr = " ".join(context.args[1:6])
            prompt = " ".join(context.args[6:])
            try:
                job = await self.scheduler.add_job(expr, prompt, chat_id)
                await self.send_message(
                    chat_id,
                    f"✅ *TAREA PROGRAMADA CREADA*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 *ID:* `{job['id']}`\n"
                    f"⏱️ *Expresión:* `{job['expression']}`\n"
                    f"📋 *Prompt:* \"{prompt}\"\n"
                    f"📅 *Próxima ejecución:* `{job['next_run']}`",
                )
            except Exception as e:
                await self.send_message(chat_id, f"❌ Error al crear tarea: {str(e)}")

    # --- CALLBACK QUERIES ---

    async def _handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_security(update):
            return
        query = update.callback_query
        if not query or not query.data:
            return

        data = query.data
        chat_id = update.effective_chat.id
        await query.answer()

        if data.startswith("sess_switch:"):
            session_id = data.split(":", 1)[1]
            success = await self.session_manager.switch_session(chat_id, session_id)
            if success:
                if self.ipc:
                    try:
                        asyncio.create_task(
                            self.ipc.broadcast_event("session_new", {"session_id": session_id})
                        )
                    except Exception:
                        pass
                await query.edit_message_text(
                    f"✅ Sesión cambiada a: `{session_id}`", parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ No se pudo cambiar de sesión.")

        elif data.startswith("sess_del_ask:"):
            session_id = data.split(":", 1)[1]
            confirm_buttons = [
                [
                    InlineButton(
                        text="⚠️ CONFIRMAR BORRADO",
                        callback_data=f"sess_del_confirm:{session_id}",
                    ),
                    InlineButton(
                        text="Cancelar",
                        callback_data="sess_del_cancel",
                    ),
                ]
            ]
            await query.edit_message_text(
                f"¿Deseas eliminar permanentemente la sesión `{session_id}`?",
                reply_markup=self._convert_buttons(confirm_buttons),
                parse_mode="Markdown",
            )

        elif data.startswith("sess_del_confirm:"):
            session_id = data.split(":", 1)[1]
            success = await self.session_manager.delete_session(chat_id, session_id)
            if success:
                active = await self.session_manager.get_or_create_active_session(chat_id)
                await query.edit_message_text(
                    f"🗑️ Sesión `{session_id}` eliminada.\nSesión activa actual: `{active['id']}`",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text("❌ No se pudo eliminar la sesión.")

        elif data == "sess_del_cancel":
            await query.edit_message_text("Operación cancelada.")

        elif data.startswith("mdl_set:"):
            _, mind_key, model_id = data.split(":", 2)
            success = update_mind_model(self.settings, mind_key, model_id)
            if success:
                if self.ipc:
                    try:
                        minds_models = {
                            m_name: cfg.model for m_name, cfg in self.settings.minds.items()
                        }
                        asyncio.create_task(self.ipc.broadcast_event("minds_sync", minds_models))
                        asyncio.create_task(
                            self.ipc.broadcast_event(
                                "mind_state",
                                {"mind": mind_key, "model": model_id, "state": "READY"},
                            )
                        )
                    except Exception:
                        pass

                await query.edit_message_text(
                    f"✅ *{mind_key}* configurado con: `{model_id}`",
                    parse_mode="Markdown",
                )
                await self.audit.log_event(
                    actor=f"user:{chat_id}",
                    action="MIND_MODEL_UPDATED",
                    tier=1,
                    detail={"mind": mind_key, "model": model_id},
                )
            else:
                await query.edit_message_text("❌ Error al actualizar configuración de modelo.")

        elif data.startswith("cron_del:"):
            job_id = data.split(":", 1)[1]
            if self.scheduler:
                success = await self.scheduler.delete_job(job_id, chat_id)
                if success:
                    await query.edit_message_text(f"🗑️ Tarea programada `{job_id}` eliminada.", parse_mode="Markdown")
                else:
                    await query.edit_message_text("❌ No se pudo eliminar la tarea programada.")

        elif data.startswith("act_conf:"):
            action_id = data.split(":", 1)[1]
            await self._execute_confirmed_action(chat_id, action_id, query)

        elif data.startswith("act_cancel:"):
            action_id = data.split(":", 1)[1]
            if self.session_manager.db:
                await self.session_manager.db.update_action_status(action_id, status="cancelled_by_human")
            await query.edit_message_text(
                f"🛑 Acción `{action_id}` cancelada por el operador humano.",
                parse_mode="Markdown",
            )
            await self.audit.log_event(
                actor=f"user:{chat_id}",
                action="ACTION_CANCELLED_BY_HUMAN",
                tier=2,
                detail={"action_id": action_id},
            )

    async def _execute_confirmed_action(
        self, chat_id: int, action_id: str, query: Optional[Any] = None
    ) -> None:
        db = self.session_manager.db
        action = await db.get_action(action_id)
        if not action or action.get("status") != "awaiting_human_confirmation":
            msg = f"⚠️ La acción `{action_id}` no está pendiente de confirmación."
            if query:
                await query.edit_message_text(msg, parse_mode="Markdown")
            else:
                await self.send_message(chat_id, msg)
            return

        tool_name = action["tool_name"]
        try:
            import json
            plan = json.loads(action.get("plan", "{}"))
            args = plan.get("arguments", {})
        except Exception:
            args = {}

        progress_msg = f"⚡ *EJECUTANDO ACCIÓN CONFIRMADA:* `{tool_name}` (`{action_id}`)..."
        if query:
            await query.edit_message_text(progress_msg, parse_mode="Markdown")
        else:
            await self.send_message(chat_id, progress_msg)

        if not self.executor_agent:
            return

        result = await self.executor_agent.execute_tool_direct(tool_name, args)
        final_status = "executed" if result.success else "failed"

        await db.update_action_status(
            action_id,
            status=final_status,
            result={"data": result.data} if result.success else None,
            error=result.error,
        )

        await self.audit.log_event(
            actor=f"user:{chat_id}",
            action="ACTION_CONFIRMED_AND_EXECUTED",
            tier=action.get("tier", 2),
            detail={"action_id": action_id, "tool": tool_name, "success": result.success},
        )

        outcome_text = (
            f"✅ *ACCIÓN COMPLETADA EXITOSAMENTE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *Acción:* `{action_id}`\n"
            f"🛠️ *Herramienta:* `{tool_name}`\n"
            f"📄 *Resultado:*\n```\n{str(result.data)[:1500]}\n```"
            if result.success
            else (
                f"❌ *FALLO EN LA EJECUCIÓN*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 *Acción:* `{action_id}`\n"
                f"⚠️ *Error:*\n```\n{result.error}\n```"
            )
        )
        await self.send_message(chat_id, outcome_text)

    # --- MENSAJES DE TEXTO ---

    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_security(update):
            return
        chat_id = update.effective_chat.id
        user_text = (update.message.text or "").strip()

        # Chequear comando de cancelación
        if user_text.lower() == "/cancel":
            await self._cmd_cancel(update, context)
            return

        # Chequear si estamos esperando el nuevo System Prompt para una mente
        if chat_id in self._pending_personality:
            mind_key = self._pending_personality.pop(chat_id)
            success = update_mind_system_prompt(self.settings, mind_key, user_text)
            if success:
                d_name = self.settings.minds[mind_key].display_name
                await self.send_message(
                    chat_id,
                    (
                        f"✅ *SYSTEM PROMPT UPDATED: {d_name}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"New prompt saved permanently to disk (`config/minds/{mind_key.lower()}.yaml`).\n"
                        f"All subsequent deliberations for {mind_key} will use this updated prompt."
                    ),
                )
                await self.audit.log_event(
                    actor=f"user:{chat_id}",
                    action="MIND_PERSONALITY_UPDATED",
                    tier=1,
                    detail={"mind": mind_key, "length": len(user_text)},
                )
            else:
                await self.send_message(chat_id, f"❌ Failed to update system prompt for `{mind_key}`.")
            return

        # Soporte para comandos de personalidad (/personality-melchior, /personality_casper, etc.)
        if user_text.startswith("/personality-") or user_text.startswith("/personality_"):
            parts = user_text.split()
            cmd = parts[0][1:].replace("-", "_")
            mind_name = cmd.replace("personality_", "").upper()
            await self._start_personality_edit(chat_id, mind_name)
            return

        # Soporte para alias con guión medio (/model-executor, etc.)
        if user_text.startswith("/model-"):
            parts = user_text.split()
            cmd = parts[0][1:].replace("-", "_")
            arg = parts[1] if len(parts) > 1 else None
            mind_name = cmd.replace("model_", "").upper()
            await self._prompt_model_selection(chat_id, mind_name, arg)
            return

        session = await self.session_manager.get_or_create_active_session(chat_id)
        await self.session_manager.record_user_message(session["id"], user_text)

        # Chequear si es una confirmación textual explícita ("CONFIRMO")
        if user_text.upper() in ("CONFIRMO", "CONFIRMAR"):
            cursor = await self.session_manager.db.conn.execute(
                "SELECT id, tool_name FROM actions WHERE session_id = ? AND status = 'awaiting_human_confirmation' ORDER BY created_at DESC LIMIT 1",
                (session["id"],),
            )
            pending_row = await cursor.fetchone()
            if pending_row:
                await self._execute_confirmed_action(chat_id, pending_row["id"])
                return
            else:
                await self.send_message(chat_id, "ℹ️ No hay acciones críticas pendientes de confirmación en esta sesión.")
                return

        # Si el agente ejecutor está activo, procesar la consulta con herramientas
        if self.executor_agent:
            await self.app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            history = await self.session_manager.get_session_history(session["id"], limit=15)
            server_snap = None
            if self.server_state:
                snap_record = await self.server_state.get_latest_snapshot()
                if snap_record and "data" in snap_record:
                    try:
                        import json
                        server_snap = json.loads(snap_record["data"])
                    except Exception:
                        pass

            result = await self.executor_agent.run(
                user_message=user_text,
                session_id=session["id"],
                chat_id=chat_id,
                history=history,
                server_snapshot=server_snap,
            )
            response_text = result.get("response", "Sin respuesta.")
            await self.send_message(chat_id, response_text)
            await self.session_manager.record_assistant_message(session["id"], response_text)

            # Si alguna herramienta quedó esperando confirmación humana, enviar la tarjeta interactiva
            for tool_info in result.get("tools_used", []):
                if tool_info.get("status") == "awaiting_human_confirmation":
                    act_id = tool_info.get("action_id", "")
                    t_name = tool_info.get("tool", "")
                    t_tier = tool_info.get("tier", 2)
                    args_str = json.dumps(tool_info.get("args", {}), ensure_ascii=False)

                    card_text = (
                        "🚨 *AUTORIZACIÓN HUMANA REQUERIDA (TIER CRÍTICO)*\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 *Acción:* `{act_id}`\n"
                        f"🛠️ *Herramienta:* `{t_name}` (Tier {t_tier})\n"
                        f"📦 *Parámetros:* `{args_str}`\n\n"
                        "El consejo MAGI aprobó la propuesta por unanimidad.\n"
                        "Como operador del servidor, confirma o cancela:"
                    )
                    buttons = [
                        [
                            InlineButton(text="⚠️ CONFIRMAR (Ejecutar)", callback_data=f"act_conf:{act_id}"),
                            InlineButton(text="❌ CANCELAR", callback_data=f"act_cancel:{act_id}"),
                        ]
                    ]
                    await self.send_message(chat_id, card_text, buttons=buttons)

            await self.audit.log_event(
                actor=f"user:{chat_id}",
                action="EXECUTOR_PROCESSED_QUERY",
                tier=0,
                detail={"tools_used": result.get("tools_used", [])},
            )
        elif self.on_user_message:
            await self.on_user_message(chat_id, user_text, session)
        else:
            reply = (
                f"📡 *MAGI HARNESS — ECO OPERACIONAL*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Sesión: `{session['id']}`\n"
                f"• Recibido: \"{user_text}\"\n"
                f"• Estado: Agente ejecutor inicializándose..."
            )
            await self.send_message(chat_id, reply)
            await self.session_manager.record_assistant_message(session["id"], reply)
