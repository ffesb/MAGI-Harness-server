import asyncio
import json
import time
from typing import Any, Dict, List, Optional
import psutil
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from magi.core.config import (
    TOLERANCE_DEFAULT,
    TOLERANCE_SECURITY,
    get_effective_system_prompt,
    update_mind_model,
    update_mind_system_prompt,
    update_mind_tolerance,
    update_openrouter_api_key,
)

api_router = APIRouter(prefix="/api")


# --- PYDANTIC SCHEMAS ---

class MindUpdateRequest(BaseModel):
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    tolerance_level: Optional[str] = None
    custom_tolerance_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ProviderUpdateRequest(BaseModel):
    api_key: str


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class ChatMessageRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class CronJobCreateRequest(BaseModel):
    expression: str
    prompt: str


# --- HELPER FUNCTIONS ---

def get_daemon_state(request: Request):
    return request.app.state.magi


def mask_key(key: str) -> str:
    if not key:
        return "No configurada"
    if len(key) <= 8:
        return "••••••••"
    return f"{key[:7]}••••••••{key[-4:]}"


# --- SYSTEM STATUS & TELEMETRY ---

@api_router.get("/status")
async def get_system_status(request: Request):
    magi = get_daemon_state(request)
    now = time.time()
    uptime_s = int(now - getattr(magi, "start_time", now))
    h, r = divmod(uptime_s, 3600)
    m, s = divmod(r, 60)
    uptime_str = f"{h}h {m}m {s}s"

    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()

    # Discos
    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("", "squashfs", "tmpfs", "overlay"):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "device": part.device,
                "free_gb": round(u.free / (1024 ** 3), 2),
                "total_gb": round(u.total / (1024 ** 3), 2),
                "free_pct": round((u.free / u.total) * 100, 1) if u.total > 0 else 0,
            })
        except Exception:
            pass

    # Sesión activa
    active_session = None
    try:
        active_session = await magi.db.get_latest_active_session()
    except Exception:
        pass

    return {
        "app_name": magi.settings.app_name,
        "version": magi.settings.version,
        "uptime": uptime_str,
        "uptime_seconds": uptime_s,
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "disks": disks,
        "active_session": active_session,
        "magi_enabled": magi.council.magi_enabled if magi.council else True,
        "provider_configured": bool(magi.settings.openrouter_api_key),
    }


# --- MINDS MANAGEMENT & TOLERANCE ---

@api_router.get("/minds")
async def list_minds(request: Request):
    magi = get_daemon_state(request)
    result = []
    for m_name, cfg in magi.settings.minds.items():
        eff_prompt = get_effective_system_prompt(cfg)
        result.append({
            "name": cfg.name,
            "display_name": cfg.display_name,
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "enabled": cfg.enabled,
            "tolerance_level": getattr(cfg, "tolerance_level", "default"),
            "custom_tolerance_prompt": getattr(cfg, "custom_tolerance_prompt", ""),
            "system_prompt": cfg.system_prompt,
            "effective_system_prompt": eff_prompt,
            "tolerance_defaults": {
                "default": TOLERANCE_DEFAULT,
                "seguridad": TOLERANCE_SECURITY,
            },
        })
    return result


@api_router.post("/minds/{mind_name}/config")
async def update_mind_config(mind_name: str, payload: MindUpdateRequest, request: Request):
    magi = get_daemon_state(request)
    key = mind_name.upper().strip()
    if key not in magi.settings.minds:
        raise HTTPException(status_code=404, detail=f"Mente '{mind_name}' no encontrada")

    mind_cfg = magi.settings.minds[key]

    # Modelo
    if payload.model:
        update_mind_model(magi.settings, key, payload.model)

    # System prompt
    if payload.system_prompt is not None:
        update_mind_system_prompt(magi.settings, key, payload.system_prompt)

    # Tolerancia
    if payload.tolerance_level is not None:
        update_mind_tolerance(
            magi.settings,
            key,
            payload.tolerance_level,
            payload.custom_tolerance_prompt,
        )

    # Temperature / Max tokens
    if payload.temperature is not None:
        mind_cfg.temperature = payload.temperature
    if payload.max_tokens is not None:
        mind_cfg.max_tokens = payload.max_tokens

    # Sincronizar vía IPC y WebSockets
    if magi.ipc:
        try:
            minds_models = {m: c.model for m, c in magi.settings.minds.items()}
            await magi.ipc.broadcast_event("minds_sync", minds_models)
            await magi.ipc.broadcast_event(
                "mind_tolerance_sync",
                {"mind": key, "level": mind_cfg.tolerance_level},
            )
        except Exception:
            pass

    await magi.audit.log_event(
        actor="web_gui",
        action="MIND_CONFIG_UPDATED",
        tier=1,
        detail={
            "mind": key,
            "model": mind_cfg.model,
            "tolerance": mind_cfg.tolerance_level,
        },
    )

    return {
        "success": True,
        "mind": key,
        "model": mind_cfg.model,
        "tolerance_level": mind_cfg.tolerance_level,
        "effective_prompt": get_effective_system_prompt(mind_cfg),
    }


# --- PROVIDER CONFIGURATION ---

@api_router.get("/provider")
async def get_provider_config(request: Request):
    magi = get_daemon_state(request)
    raw_key = magi.settings.openrouter_api_key
    return {
        "api_key_masked": mask_key(raw_key),
        "is_configured": bool(raw_key),
        "base_url": magi.settings.openrouter_base_url,
    }


@api_router.post("/provider")
async def set_provider_config(payload: ProviderUpdateRequest, request: Request):
    magi = get_daemon_state(request)
    new_key = payload.api_key.strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="La API Key no puede estar vacía")

    update_openrouter_api_key(magi.settings, new_key)
    if magi.provider:
        magi.provider.api_key = new_key

    await magi.audit.log_event(
        actor="web_gui",
        action="PROVIDER_KEY_UPDATED",
        tier=2,
        detail={"masked": mask_key(new_key)},
    )

    return {"success": True, "masked": mask_key(new_key)}


@api_router.post("/provider/test")
async def test_provider_connection(request: Request):
    magi = get_daemon_state(request)
    if not magi.settings.openrouter_api_key:
        raise HTTPException(status_code=400, detail="API Key no configurada")

    try:
        resp = await magi.provider.chat_completion(
            model="anthropic/claude-3.5-sonnet",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        return {"success": True, "message": "Conexión a OpenRouter exitosa", "response": resp}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- SESSIONS MANAGEMENT ---

@api_router.get("/sessions")
async def list_sessions(request: Request):
    magi = get_daemon_state(request)
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347
    sessions = await magi.session_manager.list_sessions(chat_id=user_id, limit=50)
    return sessions


@api_router.post("/sessions")
async def create_session(payload: SessionCreateRequest, request: Request):
    magi = get_daemon_state(request)
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347
    session = await magi.session_manager.create_new_session(chat_id=user_id, title=payload.title)
    if magi.ipc:
        try:
            await magi.ipc.broadcast_event("session_new", {"session_id": session["id"]})
        except Exception:
            pass
    return session


@api_router.post("/sessions/{session_id}/switch")
async def switch_session(session_id: str, request: Request):
    magi = get_daemon_state(request)
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347
    success = await magi.session_manager.switch_session(chat_id=user_id, session_id=session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if magi.ipc:
        try:
            await magi.ipc.broadcast_event("session_new", {"session_id": session_id})
        except Exception:
            pass
    return {"success": True, "session_id": session_id}


@api_router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    magi = get_daemon_state(request)
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347
    success = await magi.session_manager.delete_session(chat_id=user_id, session_id=session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    active = await magi.session_manager.get_or_create_active_session(chat_id=user_id)
    return {"success": True, "active_session": active}


@api_router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, request: Request):
    magi = get_daemon_state(request)
    messages = await magi.db.get_messages(session_id=session_id, limit=100)

    # Obtener también las acciones y votos de la sesión
    actions_cursor = await magi.db.conn.execute(
        "SELECT * FROM actions WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
    )
    action_rows = await actions_cursor.fetchall()
    actions = []
    for a in action_rows:
        ad = dict(a)
        votes = await magi.db.get_votes_for_action(ad["id"])
        ad["votes"] = votes
        actions.append(ad)

    return {"messages": messages, "actions": actions}


# --- CHAT & ACTIONS EXECUTION ---

@api_router.post("/chat")
async def send_chat_message(payload: ChatMessageRequest, request: Request):
    magi = get_daemon_state(request)
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347

    session = None
    if payload.session_id:
        cursor = await magi.db.conn.execute("SELECT * FROM sessions WHERE id = ?", (payload.session_id,))
        row = await cursor.fetchone()
        if row:
            session = dict(row)
    if not session:
        session = await magi.session_manager.get_or_create_active_session(chat_id=user_id)

    raw_text = payload.message.strip()

    # Manejo de Comandos si empieza por '/'
    if raw_text.startswith("/"):
        parts = raw_text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/new",):
            title = " ".join(args) if args else None
            new_s = await magi.session_manager.create_new_session(user_id, title)
            return {"type": "command_result", "session_id": new_s["id"], "response": f"✨ Nueva sesión creada: `{new_s['id']}`"}

        elif cmd in ("/status",):
            uptime_s = int(time.time() - getattr(magi, "start_time", time.time()))
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            resp = f"🟢 **MAGI STATUS**: Host Nominal\n• CPU: {cpu:.1f}%\n• RAM: {mem.percent}%\n• Uptime: {uptime_s}s\n• Sesión: `{session['id']}`"
            return {"type": "command_result", "session_id": session["id"], "response": resp}

        elif cmd in ("/tolerance", "/tolerancia"):
            if not args:
                return {"type": "command_result", "session_id": session["id"], "response": "Uso: `/tolerance <mind> <level>`"}
            mind_key = args[0].upper()
            level = args[1].lower() if len(args) > 1 else "default"
            custom = " ".join(args[2:]).strip() if len(args) > 2 else None
            success = update_mind_tolerance(magi.settings, mind_key, level, custom)
            if success:
                return {"type": "command_result", "session_id": session["id"], "response": f"✅ Tolerancia de **{mind_key}** establecida en: `{level.upper()}`"}
            return {"type": "command_result", "session_id": session["id"], "response": f"❌ Error configurando tolerancia para {mind_key}"}

        elif cmd.startswith(("/tolerance_", "/tolerancia_")):
            mind_key = cmd.split("_", 1)[1].upper()
            level = args[0].lower() if args else "default"
            custom = " ".join(args[1:]).strip() if len(args) > 1 else None
            success = update_mind_tolerance(magi.settings, mind_key, level, custom)
            if success:
                return {"type": "command_result", "session_id": session["id"], "response": f"✅ Tolerancia de **{mind_key}** establecida en: `{level.upper()}`"}
            return {"type": "command_result", "session_id": session["id"], "response": f"❌ Error configurando tolerancia para {mind_key}"}

        elif cmd.startswith(("/model_", "/model-")):
            mind_key = cmd.replace("/model_", "").replace("/model-", "").upper()
            if args:
                update_mind_model(magi.settings, mind_key, args[0])
                return {"type": "command_result", "session_id": session["id"], "response": f"✅ Modelo de **{mind_key}** actualizado a `{args[0]}`"}

        elif cmd in ("/model",):
            if len(args) >= 2:
                mind_key = args[0].upper()
                update_mind_model(magi.settings, mind_key, args[1])
                return {"type": "command_result", "session_id": session["id"], "response": f"✅ Modelo de **{mind_key}** actualizado a `{args[1]}`"}

        elif cmd in ("/help", "/ayuda"):
            help_msg = (
                "🏛️ **COMANDOS DISPONIBLES DE MAGI**\n\n"
                "• `/new [titulo]` — Inicializar nueva sesión\n"
                "• `/sessions` — Listar sesiones pasadas\n"
                "• `/status` — Telemetría del servidor\n"
                "• `/tolerance <mind> <level>` — Cambiar tolerancia (default, seguridad, solo_personalidad, personalizado)\n"
                "• `/model <mind> <model>` — Cambiar modelo LLM\n"
                "• `/provider <key>` — Actualizar API Key\n"
                "• `/cronjobs` — Ver tareas programadas\n"
                "• `/cancel` — Cancelar acción pendiente"
            )
            return {"type": "command_result", "session_id": session["id"], "response": help_msg}

    # Registrar mensaje de usuario
    await magi.session_manager.record_user_message(session["id"], raw_text)

    # Verificar si es confirmación explícita
    if raw_text.upper() in ("CONFIRMO", "CONFIRMAR"):
        cursor = await magi.db.conn.execute(
            "SELECT id, tool_name FROM actions WHERE session_id = ? AND status = 'awaiting_human_confirmation' ORDER BY created_at DESC LIMIT 1",
            (session["id"],),
        )
        pending_row = await cursor.fetchone()
        if pending_row:
            act_id = pending_row["id"]
            action = await magi.db.get_action(act_id)
            plan = json.loads(action.get("plan", "{}"))
            args = plan.get("arguments", {})
            tool_name = action["tool_name"]

            result = await magi.executor_agent.execute_tool_direct(tool_name, args)
            st = "executed" if result.success else "failed"
            await magi.db.update_action_status(act_id, status=st, result={"data": result.data} if result.success else None, error=result.error)

            out_text = f"✅ **Acción `{act_id}` ejecutada exitosamente**: `{tool_name}`" if result.success else f"❌ **Error al ejecutar `{act_id}`**: {result.error}"
            await magi.session_manager.record_assistant_message(session["id"], out_text)
            return {
                "type": "action_executed",
                "session_id": session["id"],
                "response": out_text,
                "action_id": act_id,
                "success": result.success,
            }

    # Ejecutar con el Agente Ejecutor
    history = await magi.session_manager.get_session_history(session["id"], limit=15)
    server_snap = None
    if magi.server_state:
        snap_record = await magi.server_state.get_latest_snapshot()
        if snap_record and "data" in snap_record:
            try:
                server_snap = json.loads(snap_record["data"])
            except Exception:
                pass

    run_result = await magi.executor_agent.run(
        user_message=raw_text,
        session_id=session["id"],
        chat_id=user_id,
        history=history,
        server_snapshot=server_snap,
    )

    response_text = run_result.get("response", "Sin respuesta.")
    await magi.session_manager.record_assistant_message(session["id"], response_text)

    # Acciones pendientes de confirmación
    pending_confirmations = []
    for tool_info in run_result.get("tools_used", []):
        if tool_info.get("status") == "awaiting_human_confirmation":
            pending_confirmations.append(tool_info)

    return {
        "type": "chat_response",
        "session_id": session["id"],
        "response": response_text,
        "tools_used": run_result.get("tools_used", []),
        "pending_confirmations": pending_confirmations,
    }


@api_router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: str, request: Request):
    magi = get_daemon_state(request)
    action = await magi.db.get_action(action_id)
    if not action or action.get("status") != "awaiting_human_confirmation":
        raise HTTPException(status_code=400, detail="La acción no está pendiente de confirmación")

    tool_name = action["tool_name"]
    plan = json.loads(action.get("plan", "{}"))
    args = plan.get("arguments", {})

    result = await magi.executor_agent.execute_tool_direct(tool_name, args)
    status_str = "executed" if result.success else "failed"

    await magi.db.update_action_status(
        action_id,
        status=status_str,
        result={"data": result.data} if result.success else None,
        error=result.error,
    )

    await magi.audit.log_event(
        actor="web_gui",
        action="ACTION_CONFIRMED_AND_EXECUTED",
        tier=action.get("tier", 2),
        detail={"action_id": action_id, "tool": tool_name, "success": result.success},
    )

    return {
        "success": result.success,
        "action_id": action_id,
        "data": result.data,
        "error": result.error,
    }


@api_router.post("/actions/{action_id}/cancel")
async def cancel_action(action_id: str, request: Request):
    magi = get_daemon_state(request)
    await magi.db.update_action_status(action_id, status="cancelled_by_human")
    await magi.audit.log_event(
        actor="web_gui",
        action="ACTION_CANCELLED_BY_HUMAN",
        tier=2,
        detail={"action_id": action_id},
    )
    return {"success": True, "action_id": action_id}


# --- CRONJOBS MANAGEMENT ---

@api_router.get("/cronjobs")
async def list_cronjobs(request: Request):
    magi = get_daemon_state(request)
    if not magi.scheduler:
        return []
    return await magi.scheduler.list_jobs()


@api_router.post("/cronjobs")
async def create_cronjob(payload: CronJobCreateRequest, request: Request):
    magi = get_daemon_state(request)
    if not magi.scheduler:
        raise HTTPException(status_code=500, detail="Scheduler no disponible")
    user_id = list(magi.settings.allowed_user_ids)[0] if magi.settings.allowed_user_ids else 7223503347
    try:
        job = await magi.scheduler.add_job(payload.expression, payload.prompt, user_id)
        return job
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_router.delete("/cronjobs/{job_id}")
async def delete_cronjob(job_id: str, request: Request):
    magi = get_daemon_state(request)
    if not magi.scheduler:
        raise HTTPException(status_code=500, detail="Scheduler no disponible")
    success = await magi.scheduler.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tarea programada no encontrada")
    return {"success": True, "job_id": job_id}


@api_router.post("/cronjobs/{job_id}/run")
async def run_cronjob_now(job_id: str, request: Request):
    magi = get_daemon_state(request)
    if not magi.scheduler:
        raise HTTPException(status_code=500, detail="Scheduler no disponible")
    cursor = await magi.db.conn.execute("SELECT * FROM cronjobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Cronjob no encontrado")
    d = dict(row)
    tpl = json.loads(d.get("action_template", "{}"))
    prompt = tpl.get("prompt", "")

    # Ejecutar en segundo plano
    if magi.scheduler.on_job_trigger:
        asyncio.create_task(
            magi.scheduler.on_job_trigger({
                "job_id": job_id,
                "prompt": prompt,
                "chat_id": d["chat_id"],
            })
        )
    return {"success": True, "message": f"Ejecución disparada para {job_id}"}


# --- AUDIT LOGS ---

@api_router.get("/audit")
async def get_audit_logs(request: Request, limit: int = 50, offset: int = 0):
    magi = get_daemon_state(request)
    return await magi.db.get_audit_logs(limit=limit, offset=offset)


# --- AUTOCOMPLETE COMMANDS CATALOG ---

@api_router.get("/commands")
async def get_available_commands():
    return [
        {"command": "/new", "args": "[titulo]", "description": "Inicializar nueva sesión limpia de trabajo", "category": "Sesión"},
        {"command": "/sessions", "args": "", "description": "Ver y alternar entre sesiones de trabajo", "category": "Sesión"},
        {"command": "/status", "args": "", "description": "Ver telemetría del servidor y estado de MAGI", "category": "Sistema"},
        {"command": "/tolerance", "args": "<mind> <level>", "description": "Ajustar criterio de tolerancia (default, seguridad, solo_personalidad, personalizado)", "category": "MAGI"},
        {"command": "/model", "args": "<mind> <model_id>", "description": "Configurar el modelo de una mente o Ejecutor", "category": "MAGI"},
        {"command": "/cronjobs", "args": "", "description": "Listar tareas autónomas programadas", "category": "Tareas"},
        {"command": "/provider", "args": "<openrouter_key>", "description": "Actualizar credencial de OpenRouter", "category": "Configuración"},
        {"command": "/cancel", "args": "", "description": "Cancelar acción o deliberación pendiente", "category": "Sistema"},
        {"command": "/help", "args": "", "description": "Manual operativo y referencia rápida de comandos", "category": "Ayuda"},
    ]
