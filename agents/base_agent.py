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

REQUEST_TIMEOUT = 60.0  # seconds to wait for a reply


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
        # request/reply: correlation_id -> Future
        self._pending_replies: dict[str, asyncio.Future] = {}
        # current thread context (set by orchestrators to link messages in a workflow)
        self.current_thread_id: str | None = None

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

    def start_thread(self) -> str:
        """Create a new thread ID and set it as current context."""
        self.current_thread_id = str(uuid.uuid4())
        return self.current_thread_id

    async def send_dm(self, to: str, content: dict, thread_id: str | None = None,
                      parent_message_id: str | None = None) -> str:
        msg_id = str(uuid.uuid4())
        await self._send({
            "action": "send",
            "payload": {
                "id": msg_id,
                "to": to,
                "type": "dm",
                "content": content,
                "thread_id": thread_id or self.current_thread_id,
                "parent_message_id": parent_message_id,
            },
        })
        return msg_id

    async def send_group(self, group_id: str, content: dict, thread_id: str | None = None,
                         required_capabilities: list[str] | None = None) -> str:
        msg_id = str(uuid.uuid4())
        await self._send({
            "action": "send",
            "payload": {
                "id": msg_id,
                "to": group_id,
                "type": "group",
                "content": content,
                "thread_id": thread_id or self.current_thread_id,
                "required_capabilities": required_capabilities,
            },
        })
        return msg_id

    async def request(
        self, to: str, content: dict,
        timeout: float = REQUEST_TIMEOUT,
        required_capabilities: list[str] | None = None,
        group: bool = False,
    ) -> dict:
        """Send a message and await the correlated reply. Returns reply content."""
        correlation_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_replies[correlation_id] = fut

        payload = {
            "id": str(uuid.uuid4()),
            "to": to,
            "type": "group" if group else "dm",
            "content": content,
            "reply_to": self.agent_id,
            "correlation_id": correlation_id,
            "thread_id": self.current_thread_id,
            "required_capabilities": required_capabilities,
        }
        await self._send({"action": "send", "payload": payload})

        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
            return reply
        except asyncio.TimeoutError:
            raise TimeoutError(f"no reply from {to} within {timeout}s")
        finally:
            self._pending_replies.pop(correlation_id, None)

    async def reply(self, original: dict, content: dict):
        """Reply to a request message, preserving correlation_id and thread."""
        if not original.get("reply_to"):
            raise ValueError("original message has no reply_to")
        msg_id = str(uuid.uuid4())
        await self._send({
            "action": "send",
            "payload": {
                "id": msg_id,
                "to": original["reply_to"],
                "type": "dm",
                "content": content,
                "correlation_id": original.get("correlation_id"),
                "thread_id": original.get("thread_id"),
                "parent_message_id": original.get("id"),
            },
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
                retries = 0
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

            # resolve pending request/reply future
            correlation_id = data.get("correlation_id")
            if correlation_id and correlation_id in self._pending_replies:
                fut = self._pending_replies.get(correlation_id)
                if fut and not fut.done():
                    fut.set_result(data.get("content", {}))
                # still fall through to handler so caller can also observe

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
