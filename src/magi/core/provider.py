import json
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel


class ModelInfo(BaseModel):
    id: str
    name: str
    context_length: Optional[int] = None
    description: Optional[str] = None


class OpenRouterClient:
    def __init__(self, api_key: str = "", base_url: str = "https://openrouter.ai/api/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/evangelion-magi-harness",
            "X-Title": "MAGI Harness",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def list_models(self) -> List[ModelInfo]:
        """Consulta en vivo los modelos disponibles en OpenRouter."""
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY no está configurada. Usa /provider para añadirla.")

        url = f"{self.base_url}/models"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise RuntimeError(
                    f"OpenRouter respondió con error HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = resp.json()
            models = []
            for item in data.get("data", []):
                models.append(
                    ModelInfo(
                        id=item.get("id", ""),
                        name=item.get("name", item.get("id", "")),
                        context_length=item.get("context_length"),
                        description=item.get("description", "")[:100] if item.get("description") else None,
                    )
                )
            return sorted(models, key=lambda m: m.name.lower())

    async def chat_completion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Envía solicitud de chat completion a OpenRouter."""
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY no configurada. Usa /provider para configurarla.")

        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=self._get_headers(), json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Error de OpenRouter ({resp.status_code}): {resp.text[:300]}"
                )
            return resp.json()
