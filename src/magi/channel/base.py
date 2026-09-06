from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel


class InlineButton(BaseModel):
    text: str
    callback_data: str


class ChannelAdapter(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Inicializa el adapter de comunicación."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Detiene de forma limpia el adapter."""
        pass

    @abstractmethod
    async def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[InlineButton]]] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> Optional[int]:
        """Envía un mensaje y retorna su ID numérico."""
        pass

    @abstractmethod
    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: Optional[List[List[InlineButton]]] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> bool:
        """Edita un mensaje existente."""
        pass

    @abstractmethod
    async def send_file(
        self,
        chat_id: int,
        path: str,
        caption: Optional[str] = None,
    ) -> bool:
        """Envía un archivo del sistema al chat."""
        pass
