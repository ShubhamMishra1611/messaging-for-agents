import asyncio
import json
import logging
import os
import uuid
from typing import Callable, Awaitable
from urllib.parse import urlencode

import httpx
import websockets

logger = logging.getLogger(__name__)

DEFAULT_SERVER_URL = os.getenv("AGENT_SERVER_URL", "ws://localhost:8000")
DEFAULT_HTTP_URL = os.getenv("AGENT_HTTP_URL", "http://localhost:8000")
DEFAULT_TOKEN = os.getenv("API_SECRET", "dev-secret-change-me")


class BaseAgent:
    """WebSocket client base class for agents."""

    def __init__(
        self,
        agent_id: str,
        server_url: str | None = None,
        capabilities: list[str] | None = None,
        groups: list[str] | None = None,
        token: str | None = None,
    ):
        self.agent_id = agent_id
        self.server_url = server_url or DEFAULT_SERVER_URL
        self.capabilities = capabilities or []
        self.groups = groups or []
        self.token = token or DEFAULT_TOKEN
        self._ws = None
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._running = False

    @staticmethod
    async def register(agent_id: str, capabilities: list[str] | None = None,
                       http_url: str | None = None, admin_key: str | None = None) -> str:
        """Register agent with server, returns per-agent API key."""
        url = (http_url or DEFAULT_HTTP_URL) + "/agents/register"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={
                "agent_id": agent_id,
                "capabilities": capabilities or [],
            }, headers={"Authorization": f"Bearer {admin_key or DEFAULT_TOKEN}"})
            if resp.status_code == 409:
                raise ValueError(f"agent '{agent_id}' already registered")
            resp.raise_for_status()
            return resp.json()["api_key"]

    async def connect(self):
        params = {
            "token": self.token,
            "capabilities": ",".join(self.capabilities),
            "groups": ",".join(self.groups),
        }
        uri = f"{self.server_url}/ws/{self.agent_id}?{urlencode(params)}"
        self._ws = await websockets.connect(uri, max_size=1024 * 1024)
        self._running = True
        logger.info("[%s] connected to %s", self.agent_id, self.server_url)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("[%s] disconnected", self.agent_id)

    async def send_dm(self, to: str, content: dict) -> str:
        msg_id = str(uuid.uuid4())
        await self._send({
            "action": "send",
            "payload": {"id": msg_id, "to": to, "type": "dm", "content": content},
        })
        return msg_id

    async def send_group(self, group_id: str, content: dict) -> str:
        msg_id = str(uuid.uuid4())
        await self._send({
            "action": "send",
            "payload": {"id": msg_id, "to": group_id, "type": "group", "content": content},
        })
        return msg_id

    async def set_status(self, status: str):
        await self._send({"action": "status", "status": status})

    async def ack(self, message_id: str):
        await self._send({"action": "ack", "message_id": message_id})

    def on_message(self, handler: Callable[[dict], Awaitable[None]]):
        self._handlers["message"] = handler

    async def run(self):
        """Connect and listen with automatic reconnect (loop, not recursion)."""
        max_retries = 5
        retries = 0
        while self._running or retries == 0:
            try:
                await self.connect()
                retries = 0  # reset on successful connect
                await self._listen()
            except websockets.ConnectionClosed:
                if not self._running:
                    break
                retries += 1
                if retries > max_retries:
                    logger.error("[%s] gave up reconnecting after %d attempts", self.agent_id, max_retries)
                    break
                delay = min(2 ** retries, 30)
                logger.warning("[%s] connection lost, reconnecting in %ds (attempt %d)", self.agent_id, delay, retries)
                await asyncio.sleep(delay)
            except Exception:
                logger.exception("[%s] unexpected error", self.agent_id)
                break

    async def _listen(self):
        while self._running:
            raw = await self._ws.recv()
            data = json.loads(raw)

            # auto-ack
            if "id" in data:
                await self.ack(data["id"])

            handler = self._handlers.get("message")
            if handler:
                await handler(data)
            else:
                logger.info("[%s] received: %s", self.agent_id, data)

    async def _send(self, data: dict):
        await self._ws.send(json.dumps(data))
