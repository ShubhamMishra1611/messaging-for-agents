import asyncio
import json
import logging
import os
from typing import Callable, Awaitable

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


class RedisPubSub:
    """Redis pub/sub bridge: publish messages and subscribe to channels."""

    def __init__(self):
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._listener_task: asyncio.Task | None = None
        self._handlers: list[Callable[[dict], Awaitable[None]]] = []

    async def connect(self):
        self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        logger.info("redis connected")

    async def disconnect(self):
        if self._listener_task:
            self._listener_task.cancel()
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()

    async def subscribe(self, *channels: str, handler: Callable[[dict], Awaitable[None]]):
        self._handlers.append(handler)
        await self._pubsub.subscribe(*channels)
        self._ensure_listener()

    async def psubscribe(self, *patterns: str, handler: Callable[[dict], Awaitable[None]]):
        self._handlers.append(handler)
        await self._pubsub.psubscribe(*patterns)
        self._ensure_listener()

    def _ensure_listener(self):
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._listen())

    async def publish(self, channel: str, message: dict):
        await self._redis.publish(channel, json.dumps(message))

    async def _listen(self):
        try:
            async for raw in self._pubsub.listen():
                if raw["type"] not in ("message", "pmessage"):
                    continue
                try:
                    data = json.loads(raw["data"])
                    for handler in self._handlers:
                        await handler(data)
                except Exception:
                    logger.exception("error handling pubsub message")
        except asyncio.CancelledError:
            pass

    # --- cache helpers ---

    async def set_json(self, key: str, value: dict, ex: int | None = None):
        await self._redis.set(key, json.dumps(value), ex=ex)

    async def get_json(self, key: str) -> dict | None:
        raw = await self._redis.get(key)
        return json.loads(raw) if raw else None

    async def delete(self, key: str):
        await self._redis.delete(key)

    async def scan_keys(self, pattern: str) -> list[str]:
        """Use SCAN instead of KEYS to avoid blocking Redis."""
        keys = []
        async for key in self._redis.scan_iter(match=pattern, count=100):
            keys.append(key)
        return keys
