import logging
import re

from fastapi import WebSocket

logger = logging.getLogger(__name__)

AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB


def validate_agent_id(agent_id: str) -> bool:
    return bool(AGENT_ID_PATTERN.match(agent_id))


class ConnectionManager:
    """Tracks WebSocket connections for agents on this server instance."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, agent_id: str, ws: WebSocket) -> bool:
        """Accept WS and register. Returns False if agent_id already connected."""
        if agent_id in self._connections:
            await ws.accept()
            await ws.close(code=4409, reason="agent_id already connected")
            logger.warning("rejected duplicate connection for %s", agent_id)
            return False
        await ws.accept()
        self._connections[agent_id] = ws
        logger.info("agent %s connected", agent_id)
        return True

    def disconnect(self, agent_id: str):
        self._connections.pop(agent_id, None)
        logger.info("agent %s disconnected", agent_id)

    def get(self, agent_id: str) -> WebSocket | None:
        return self._connections.get(agent_id)

    def is_local(self, agent_id: str) -> bool:
        return agent_id in self._connections

    @property
    def connected_agents(self) -> list[str]:
        return list(self._connections.keys())

    async def send_json(self, agent_id: str, data: dict) -> bool:
        ws = self._connections.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            logger.exception("failed to send to %s", agent_id)
            self.disconnect(agent_id)
            return False
