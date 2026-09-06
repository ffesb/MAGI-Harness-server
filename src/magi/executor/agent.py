import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from magi.core.config import Settings, get_effective_system_prompt
from magi.core.db import Database
from magi.core.audit import AuditLogger
from magi.core.provider import OpenRouterClient
from magi.council.minds import CouncilManager
from magi.council.policy import PolicyEngine
from magi.executor.tools.base import BaseTool, ToolResult
from magi.executor.tools.readonly import (
    ReadFileTool,
    ListDirectoryTool,
    InspectDockerTool,
    InspectSystemdTool,
    ServerMetricsTool,
)
from magi.executor.tools.search import WebSearchTool
from magi.executor.tools.mutant import (
    DockerRestartTool,
    DockerStartTool,
    DockerStopTool,
    DockerRemoveTool,
    DockerPruneTool,
    PackageInstallTool,
    PackageRemoveTool,
    WriteConfigFileTool,
    SystemRebootTool,
    SystemPoweroffTool,
)


class ExecutorAgent:
    def __init__(
        self,
        settings: Settings,
        provider: OpenRouterClient,
        db: Optional[Database] = None,
        audit: Optional[AuditLogger] = None,
        council: Optional[CouncilManager] = None,
        on_notify: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
        ipc: Optional[Any] = None,
    ):
        self.settings = settings
        self.provider = provider
        self.db = db
        self.audit = audit
        self.council = council
        self.on_notify = on_notify
        self.ipc = ipc

        # Catálogo unificado de herramientas (Solo lectura + Mutantes)
        self.tools: Dict[str, BaseTool] = {
            # Tier 0 (Read-only)
            "read_file": ReadFileTool(),
            "list_directory": ListDirectoryTool(),
            "inspect_docker": InspectDockerTool(),
            "inspect_systemd": InspectSystemdTool(),
            "get_server_metrics": ServerMetricsTool(),
            "web_search": WebSearchTool(base_url=settings.searxng_url),
            # Tier 1 (Low impact)
            "docker_container_restart": DockerRestartTool(),
            "docker_container_start": DockerStartTool(),
            # Tier 2 (Critical)
            "docker_container_stop": DockerStopTool(),
            "docker_container_remove": DockerRemoveTool(),
            "docker_system_prune": DockerPruneTool(),
            "package_install": PackageInstallTool(),
            "package_remove": PackageRemoveTool(),
            "write_config_file": WriteConfigFileTool(),
            # Tier 3 (Irreversible)
            "system_reboot": SystemRebootTool(),
            "system_poweroff": SystemPoweroffTool(),
        }

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self.tools.values()]

    async def execute_tool_direct(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(success=False, error=f"Herramienta '{name}' no existe en el catálogo.", tier=2)
        return await tool.execute(**arguments)

    async def run(
        self,
        user_message: str,
        session_id: str,
        chat_id: int,
        history: List[Dict[str, Any]],
        server_snapshot: Optional[Dict[str, Any]] = None,
        max_tool_iterations: int = 5,
    ) -> Dict[str, Any]:
        """Ciclo principal de razonamiento, auditoría de MAGI y ejecución."""
        if not self.settings.openrouter_api_key:
            return {
                "response": (
                    "⚠️ *OPENROUTER NO CONFIGURADO*\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    "Para activar el procesamiento cognitivo de la Ejecutora y MAGI, "
                    "configura tu API Key con el comando:\n\n"
                    "`/provider <tu_openrouter_api_key>`\n\n"
                    "El token se almacenará de forma segura en `config/magi.env`."
                ),
                "tools_used": [],
            }

        mind_cfg = self.settings.minds.get("EXECUTOR")
        system_prompt = (
            get_effective_system_prompt(mind_cfg)
            if mind_cfg
            else "Eres la AI Ejecutadora de MAGI Harness."
        )

        if server_snapshot:
            system_prompt += (
                f"\n\n[ESTADO ACTUAL DEL SERVIDOR (.init snapshot {server_snapshot.get('id')}]:\n"
                f"{json.dumps(server_snapshot, indent=2, ensure_ascii=False)[:3000]}"
            )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        for msg in history[-10:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})

        tools_used = []
        model_name = mind_cfg.model if mind_cfg else "anthropic/claude-3.5-sonnet"

        for iteration in range(max_tool_iterations):
            try:
                resp = await self.provider.chat_completion(
                    model=model_name,
                    messages=messages,
                    tools=self.get_tool_schemas(),
                    temperature=mind_cfg.temperature if mind_cfg else 0.2,
                    max_tokens=mind_cfg.max_tokens if mind_cfg else 3000,
                )
            except Exception as e:
                return {
                    "response": f"❌ *Error de comunicación con OpenRouter:* {str(e)}",
                    "tools_used": tools_used,
                }

            choices = resp.get("choices", [])
            if not choices:
                return {"response": "⚠️ No se recibió respuesta del modelo.", "tools_used": tools_used}

            choice = choices[0]
            msg_obj = choice.get("message", {})
            messages.append(msg_obj)

            tool_calls = msg_obj.get("tool_calls", [])
            if not tool_calls:
                return {"response": msg_obj.get("content", ""), "tools_used": tools_used}

            # Procesar cada herramienta solicitada
            for tool_call in tool_calls:
                fn = tool_call.get("function", {})
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}

                tier = self.settings.get_tool_tier(tool_name)
                action_id = f"act_{uuid.uuid4().hex[:8]}"

                # Si es Tier 0 (solo lectura): se ejecuta inmediatamente sin requerir voto de MAGI
                if tier == 0:
                    result = await self.execute_tool_direct(tool_name, args)
                    tools_used.append({"tool": tool_name, "args": args, "tier": 0, "status": "executed"})
                else:
                    # Acción Mutante (Tier 1, 2 o 3): PIPELINE DE RIESGO Y DELIBERACIÓN MAGI
                    if self.db:
                        await self.db.create_action(
                            action_id=action_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            tier=tier,
                            status="deliberating",
                            plan={"arguments": args},
                        )

                    # Notificar deliberación si hay callback
                    if self.on_notify:
                        await self.on_notify(
                            f"🏛️ *MAGI DELIBERANDO:* `{tool_name}` (Tier {tier})\n"
                            "MELCHIOR-1 | BALTHASAR-2 | CASPER-3 analizando la propuesta..."
                        )

                    if self.ipc:
                        try:
                            for m in ["BALTHASAR", "CASPER", "MELCHIOR"]:
                                await self.ipc.broadcast_event(
                                    "mind_state",
                                    {"mind": m, "state": "DELIBERATING"}
                                )
                        except Exception:
                            pass

                    # Ejecutar deliberación en paralelo con las 3 mentes
                    magi_enabled = self.council.magi_enabled if self.council else True
                    verdicts = {}
                    if self.council:
                        verdicts = await self.council.deliberate_in_parallel(
                            action_id=action_id,
                            tool_name=tool_name,
                            plan=args,
                            diagnosis=user_message,
                            server_snapshot=server_snapshot,
                        )

                    # Persistir votos en base de datos y auditoría
                    for m_name, verdict in verdicts.items():
                        if self.db:
                            await self.db.add_vote(
                                action_id=action_id,
                                mind_name=m_name,
                                vote=verdict.vote,
                                confidence=verdict.confidence,
                                reasoning=verdict.reasoning,
                                concerns=verdict.concerns,
                                model_used=verdict.model_used,
                                latency_ms=verdict.latency_ms,
                            )
                        if self.audit:
                            await self.audit.log_event(
                                actor=f"mind:{m_name}",
                                action="MAGI_VOTE_CAST",
                                tier=tier,
                                detail={
                                    "action_id": action_id,
                                    "tool": tool_name,
                                    "vote": verdict.vote,
                                    "confidence": verdict.confidence,
                                    "reasoning": verdict.reasoning,
                                },
                            )
                        if self.ipc:
                            try:
                                await self.ipc.broadcast_event(
                                    "mind_state",
                                    {
                                        "mind": m_name,
                                        "state": verdict.vote.upper(),
                                        "confidence": verdict.confidence,
                                        "reasoning": verdict.reasoning,
                                        "model": verdict.model_used,
                                    }
                                )
                            except Exception:
                                pass

                    # Evaluar resultado con el Motor de Políticas
                    outcome = PolicyEngine.evaluate(
                        action_id=action_id,
                        tool_name=tool_name,
                        tier=tier,
                        magi_enabled=magi_enabled,
                        verdicts=verdicts,
                    )

                    if self.ipc:
                        try:
                            await self.ipc.broadcast_event(
                                "consensus",
                                {
                                    "text": f"{outcome.consensus_type} — {'APROBADO' if outcome.passed else 'RECHAZADO'}",
                                    "passed": outcome.passed,
                                    "action_id": action_id,
                                }
                            )
                        except Exception:
                            pass

                    if not outcome.passed:
                        # Rechazado o Bloqueado por desacuerdo
                        if self.db:
                            await self.db.update_action_status(action_id, status="rejected")
                        if self.audit:
                            await self.audit.log_event(
                                actor="council",
                                action="ACTION_REJECTED",
                                tier=tier,
                                detail={"action_id": action_id, "consensus": outcome.consensus_type},
                            )

                        result = ToolResult(
                            success=False,
                            error=(
                                f"Acción BLOQUEADA/RECHAZADA por el consejo MAGI ({outcome.consensus_type}). "
                                f"Detalle: {outcome.explanation}"
                            ),
                            tier=tier,
                        )
                        tools_used.append({"tool": tool_name, "args": args, "tier": tier, "status": "rejected"})

                    elif outcome.requires_human_confirmation:
                        # Requiere confirmación humana obligatoria (Tier 2-3)
                        if self.db:
                            await self.db.update_action_status(action_id, status="awaiting_human_confirmation")
                        if self.audit:
                            await self.audit.log_event(
                                actor="council",
                                action="AWAITING_HUMAN_CONFIRMATION",
                                tier=tier,
                                detail={"action_id": action_id, "tool": tool_name, "args": args},
                            )

                        tools_used.append({
                            "tool": tool_name,
                            "args": args,
                            "tier": tier,
                            "status": "awaiting_human_confirmation",
                            "action_id": action_id,
                        })

                        result = ToolResult(
                            success=False,
                            error=(
                                f"ACCIÓN PAUSADA POR PROTOCOLO DE SEGURIDAD. "
                                f"El consejo MAGI aprobó la propuesta ({outcome.consensus_type}), "
                                f"pero por ser Tier {tier} ({self.settings.get_tier_policy(tier).name}) "
                                f"requiere CONFIRMACIÓN HUMANA EXPLÍCITA del operador en Telegram para ejecutarse. "
                                f"ID de Acción: {action_id}."
                            ),
                            tier=tier,
                        )
                    else:
                        # Aprobado para ejecución directa (Tier 1 por mayoría simple o MAGI deshabilitado)
                        if self.db:
                            await self.db.update_action_status(action_id, status="approved")

                        result = await self.execute_tool_direct(tool_name, args)
                        status_str = "executed" if result.success else "failed"

                        if self.db:
                            await self.db.update_action_status(
                                action_id,
                                status=status_str,
                                result={"data": result.data} if result.success else None,
                                error=result.error,
                            )
                        if self.audit:
                            await self.audit.log_event(
                                actor="executor",
                                action="ACTION_EXECUTED",
                                tier=tier,
                                detail={"action_id": action_id, "tool": tool_name, "success": result.success},
                            )
                        tools_used.append({"tool": tool_name, "args": args, "tier": tier, "status": status_str})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "name": tool_name,
                        "content": json.dumps(
                            {"success": result.success, "data": result.data, "error": result.error},
                            ensure_ascii=False,
                        ),
                    }
                )

        return {
            "response": "⚠️ Se alcanzó el límite de llamadas a herramientas sin respuesta final.",
            "tools_used": tools_used,
        }
