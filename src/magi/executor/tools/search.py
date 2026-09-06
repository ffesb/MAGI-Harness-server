import urllib.parse
from typing import Any, Dict, List, Optional
import httpx

from magi.executor.tools.base import BaseTool, ToolResult


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Busca información en internet a través de SearXNG y devuelve resultados con fuentes citadas."
    tier = 0
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Término de búsqueda o consulta específica",
            },
            "max_results": {
                "type": "integer",
                "description": "Cantidad máxima de resultados a retornar (por defecto 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def __init__(self, base_url: str = "https://fe-sv.tail7345d6.ts.net:4000/"):
        self.base_url = base_url.rstrip("/")

    async def execute(self, query: str, max_results: int = 5, **kwargs) -> ToolResult:
        if not self.base_url:
            return ToolResult(
                success=False,
                error="URL de SearXNG no configurada.",
                tier=0,
            )

        endpoint = f"{self.base_url}/search"
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=12.0) as client:
                response = await client.get(endpoint, params=params)
                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"SearXNG respondió con código HTTP {response.status_code}",
                        tier=0,
                    )

                data = response.json()
                raw_results = data.get("results", [])

                if not raw_results:
                    # Fallback sin categories
                    fallback_resp = await client.get(endpoint, params={"q": query, "format": "json"})
                    if fallback_resp.status_code == 200:
                        raw_results = fallback_resp.json().get("results", [])

                clean_results = []
                for idx, r in enumerate(raw_results[:max_results], start=1):
                    clean_results.append(
                        {
                            "index": idx,
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("content", ""),
                            "engine": r.get("engine", "searxng"),
                        }
                    )

                return ToolResult(
                    success=True,
                    data={
                        "query": query,
                        "count": len(clean_results),
                        "results": clean_results,
                    },
                    tier=0,
                )
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                error=f"Tiempo de espera agotado al conectar a SearXNG en {self.base_url}",
                tier=0,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error al ejecutar búsqueda web: {str(e)}",
                tier=0,
            )
