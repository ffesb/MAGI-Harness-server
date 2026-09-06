import asyncio
import json
import time
from typing import Any, Dict, List, Optional
from magi.core.config import Settings
from magi.core.provider import OpenRouterClient
from magi.council.voter import MagiVote, MindVerdict
from magi.executor.tools.base import BaseTool, ToolResult
from magi.executor.tools.readonly import (
    ReadFileTool,
    ListDirectoryTool,
    InspectDockerTool,
    InspectSystemdTool,
    ServerMetricsTool,
)


class MindWorker:
    def __init__(
        self,
        mind_name: str,
        settings: Settings,
        provider: OpenRouterClient,
        tools: Dict[str, BaseTool],
    ):
        self.mind_name = mind_name.upper()
        self.settings = settings
        self.provider = provider
        self.tools = tools

    def _get_tools_schema(self) -> List[Dict[str, Any]]:
        return [t.to_openai_schema() for t in self.tools.values()]

    async def deliberate(
        self,
        action_id: str,
        tool_name: str,
        plan: Dict[str, Any],
        diagnosis: str,
        server_snapshot: Optional[Dict[str, Any]] = None,
        max_tool_rounds: int = 3,
    ) -> MindVerdict:
        start_t = time.time()
        mind_cfg = self.settings.minds.get(self.mind_name)

        model = mind_cfg.model if mind_cfg else "anthropic/claude-3.5-sonnet"
        temp = mind_cfg.temperature if mind_cfg else 0.1
        sys_prompt = mind_cfg.system_prompt if mind_cfg else f"Eres {self.mind_name} de MAGI."

        audit_prompt = (
            f"PROPUESTA A AUDITAR Y VOTAR:\n"
            f"• Identificador de Acción: `{action_id}`\n"
            f"• Herramienta Solicitada: `{tool_name}`\n"
            f"• Parámetros del Plan: {json.dumps(plan, ensure_ascii=False)}\n"
            f"• Diagnóstico de la Ejecutora: {diagnosis}\n\n"
            "INSTRUCCIONES DE AUDITORÍA:\n"
            "1. Tienes herramientas de solo lectura para inspeccionar el servidor si necesitas comprobar los hechos por tu cuenta.\n"
            "2. Evalúa la seguridad, pertinencia y proporcionalidad de la acción desde tu perspectiva.\n"
            "3. Emite tu veredicto obligatoriamente en formato JSON válido:\n"
            "{\n"
            '  "vote": "approve" | "reject" | "abstain",\n'
            '  "confidence": 0.0 a 1.0,\n'
            '  "reasoning": "...",\n'
            '  "concerns": ["..."]\n'
            "}"
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": audit_prompt},
        ]

        # En caso de que no haya API key configurada
        if not self.settings.openrouter_api_key:
            return MindVerdict(
                mind_name=self.mind_name,
                vote="abstain",
                confidence=0.0,
                reasoning="OpenRouter API Key no configurada.",
                concerns=["Falta credencial de proveedor LLM"],
                model_used="none",
                latency_ms=0,
            )

        for _ in range(max_tool_rounds):
            try:
                resp = await self.provider.chat_completion(
                    model=model,
                    messages=messages,
                    tools=self._get_tools_schema(),
                    temperature=temp,
                    max_tokens=1000,
                )
            except Exception as e:
                latency = int((time.time() - start_t) * 1000)
                return MindVerdict(
                    mind_name=self.mind_name,
                    vote="abstain",
                    confidence=0.0,
                    reasoning=f"Fallo en llamada al modelo: {str(e)}",
                    concerns=[str(e)],
                    model_used=model,
                    latency_ms=latency,
                )

            choices = resp.get("choices", [])
            if not choices:
                break

            msg_obj = choices[0].get("message", {})
            messages.append(msg_obj)
            tool_calls = msg_obj.get("tool_calls", [])

            if not tool_calls:
                # El modelo respondió con texto final
                content = msg_obj.get("content", "").strip()
                latency = int((time.time() - start_t) * 1000)

                # Intentar extraer JSON del contenido
                try:
                    # Limpiar delimitadores markdown ```json ... ``` si existen
                    json_str = content
                    if "```json" in json_str:
                        json_str = json_str.split("```json")[1].split("```")[0].strip()
                    elif "```" in json_str:
                        json_str = json_str.split("```")[1].split("```")[0].strip()

                    parsed = json.loads(json_str)
                    vote_data = MagiVote(**parsed)
                    return MindVerdict(
                        mind_name=self.mind_name,
                        vote=vote_data.vote,
                        confidence=vote_data.confidence,
                        reasoning=vote_data.reasoning,
                        concerns=vote_data.concerns,
                        model_used=model,
                        latency_ms=latency,
                    )
                except Exception:
                    # Si no es JSON limpio, inferir aprobación o rechazo
                    low = content.lower()
                    vote = "approve" if "approve" in low or "apruebo" in low else ("reject" if "reject" in low or "rechazo" in low else "abstain")
                    return MindVerdict(
                        mind_name=self.mind_name,
                        vote=vote,
                        confidence=0.7,
                        reasoning=content[:200],
                        concerns=[],
                        model_used=model,
                        latency_ms=latency,
                    )

            # Ejecutar herramientas de diagnóstico invocadas por la mente
            for tc in tool_calls:
                fn = tc.get("function", {})
                t_name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}

                tool = self.tools.get(t_name)
                if tool:
                    res = await tool.execute(**args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": t_name,
                            "content": json.dumps(
                                {"success": res.success, "data": res.data, "error": res.error},
                                ensure_ascii=False,
                            ),
                        }
                    )

        latency = int((time.time() - start_t) * 1000)
        return MindVerdict(
            mind_name=self.mind_name,
            vote="abstain",
            confidence=0.0,
            reasoning="Se excedieron las rondas de deliberación sin veredicto.",
            concerns=["Exceso de iteraciones"],
            model_used=model,
            latency_ms=latency,
        )


class CouncilManager:
    def __init__(self, settings: Settings, provider: OpenRouterClient):
        self.settings = settings
        self.provider = provider
        self.magi_enabled = True

        # Herramientas de solo lectura compartidas para verificación independiente
        self.readonly_tools = {
            "read_file": ReadFileTool(),
            "list_directory": ListDirectoryTool(),
            "inspect_docker": InspectDockerTool(),
            "inspect_systemd": InspectSystemdTool(),
            "get_server_metrics": ServerMetricsTool(),
        }

        self.workers = {
            "MELCHIOR": MindWorker("MELCHIOR", settings, provider, self.readonly_tools),
            "BALTHASAR": MindWorker("BALTHASAR", settings, provider, self.readonly_tools),
            "CASPER": MindWorker("CASPER", settings, provider, self.readonly_tools),
        }

    def set_magi_enabled(self, enabled: bool) -> None:
        self.magi_enabled = enabled

    async def deliberate_in_parallel(
        self,
        action_id: str,
        tool_name: str,
        plan: Dict[str, Any],
        diagnosis: str,
        server_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, MindVerdict]:
        """Ejecuta la deliberación independiente de las tres mentes en paralelo."""
        tasks = [
            self.workers["MELCHIOR"].deliberate(
                action_id, tool_name, plan, diagnosis, server_snapshot
            ),
            self.workers["BALTHASAR"].deliberate(
                action_id, tool_name, plan, diagnosis, server_snapshot
            ),
            self.workers["CASPER"].deliberate(
                action_id, tool_name, plan, diagnosis, server_snapshot
            ),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        verdicts = {}
        names = ["MELCHIOR", "BALTHASAR", "CASPER"]
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                verdicts[name] = MindVerdict(
                    mind_name=name,
                    vote="abstain",
                    confidence=0.0,
                    reasoning=f"Excepción en deliberación: {str(res)}",
                    concerns=[str(res)],
                    model_used="error",
                    latency_ms=0,
                )
            else:
                verdicts[name] = res

        return verdicts
