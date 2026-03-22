import logging
from datetime import datetime, timezone

from sqlalchemy import select, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Agent, GroupMembership, VALID_AGENT_STATUSES
from server.redis_pubsub import RedisPubSub

logger = logging.getLogger(__name__)
AGENT_CACHE_TTL = 300  # 5 min


class AgentRegistry:
    def __init__(self, redis: RedisPubSub, session_factory):
        self._redis = redis
        self._session_factory = session_factory

    async def register(self, agent_id: str, capabilities: list[str], server_id: str):
        async with self._session_factory() as session:
            stmt = (
                pg_insert(Agent)
                .values(
                    agent_id=agent_id,
                    capabilities=capabilities,
                    status="idle",
                    server_id=server_id,
                    connected_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=["agent_id"],
                    set_=dict(
                        capabilities=capabilities,
                        status="idle",
                        server_id=server_id,
                        connected_at=datetime.now(timezone.utc),
                    ),
                )
            )
            await session.execute(stmt)
            await session.commit()

        await self._cache_agent(agent_id, capabilities, "idle", server_id)
        logger.info("registered agent %s on server %s", agent_id, server_id)

    async def unregister(self, agent_id: str):
        async with self._session_factory() as session:
            await session.execute(update(Agent).where(Agent.agent_id == agent_id).values(status="offline"))
            await session.commit()
        await self._redis.delete(f"agent:{agent_id}")
        logger.info("unregistered agent %s", agent_id)

    async def update_status(self, agent_id: str, status: str):
        if status not in VALID_AGENT_STATUSES:
            raise ValueError(f"invalid status: {status}")
        async with self._session_factory() as session:
            await session.execute(update(Agent).where(Agent.agent_id == agent_id).values(status=status))
            await session.commit()
        cached = await self._redis.get_json(f"agent:{agent_id}")
        if cached:
            cached["status"] = status
            await self._redis.set_json(f"agent:{agent_id}", cached, ex=AGENT_CACHE_TTL)

    async def lookup(self, agent_id: str) -> dict | None:
        cached = await self._redis.get_json(f"agent:{agent_id}")
        if cached:
            return cached
        async with self._session_factory() as session:
            row = await session.get(Agent, agent_id)
            if row is None:
                return None
            data = {
                "agent_id": row.agent_id,
                "capabilities": row.capabilities,
                "status": row.status,
                "server_id": row.server_id,
            }
            await self._redis.set_json(f"agent:{agent_id}", data, ex=AGENT_CACHE_TTL)
            return data

    async def find_by_capability(self, capability: str) -> list[dict]:
        async with self._session_factory() as session:
            stmt = select(Agent).where(
                Agent.capabilities.any(capability),
                Agent.status == "idle",
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {"agent_id": r.agent_id, "capabilities": r.capabilities, "status": r.status, "server_id": r.server_id}
                for r in rows
            ]

    async def join_groups(self, agent_id: str, group_ids: list[str]):
        async with self._session_factory() as session:
            for gid in group_ids:
                stmt = (
                    pg_insert(GroupMembership)
                    .values(group_id=gid, agent_id=agent_id)
                    .on_conflict_do_nothing(constraint="uq_group_agent")
                )
                await session.execute(stmt)
            await session.commit()
        logger.info("agent %s joined groups %s", agent_id, group_ids)

    async def leave_group(self, agent_id: str, group_id: str):
        async with self._session_factory() as session:
            await session.execute(
                delete(GroupMembership).where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.agent_id == agent_id,
                )
            )
            await session.commit()
        logger.info("agent %s left group %s", agent_id, group_id)

    async def _cache_agent(self, agent_id: str, capabilities: list[str], status: str, server_id: str):
        await self._redis.set_json(
            f"agent:{agent_id}",
            {"agent_id": agent_id, "capabilities": capabilities, "status": status, "server_id": server_id},
            ex=AGENT_CACHE_TTL,
        )
