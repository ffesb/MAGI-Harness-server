from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: Optional[str] = None
    tier: int = 0


class BaseTool(ABC):
    name: str
    description: str
    tier: int
    parameters: Dict[str, Any]

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        pass

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convierte la herramienta al formato estándar de OpenAI function/tool calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
