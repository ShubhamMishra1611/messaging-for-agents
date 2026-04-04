import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Message, Ack, DeliveryAttempt, GroupMembership, Agent, Claim
from server.connection_manager import ConnectionManager
from server.redis_pubsub import RedisPubSub

logger = logging.getLogger(__name__)

CLAIM_TIMEOUT_SECONDS = 120  # reclaim after 2 min of no completion


class MessageRouter:
    def __init__(self, manager: ConnectionManager, redis: RedisPubSub, session_factory):
        self._manager = manager
        self._redis = redis
        self._session_factory = session_factory

    async def route(self, payload: dict):
        """Route an incoming message from a sender agent."""
        msg_id = uuid.UUID(payload["id"]) if "id" in payload else uuid.uuid4()
        msg_type = payload.get("type", "dm")
        to = payload["to"]
        from_agent = payload["from_agent"]
        content = payload.get("content", {})
        now = datetime.now(timezone.utc)
        timestamp = payload.get("timestamp", now.isoformat())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        thread_id_raw = payload.get("thread_id")
        thread_id = uuid.UUID(thread_id_raw) if thread_id_raw else None
        parent_message_id_raw = payload.get("parent_message_id")
        parent_message_id = uuid.UUID(parent_message_id_raw) if parent_message_id_raw else None
        reply_to = payload.get("reply_to")
        correlation_id = payload.get("correlation_id")
        required_capabilities = payload.get("required_capabilities") or []

        recipient_count = 1
        if msg_type == "group":
            async with self._session_factory() as session:
                members = await self._get_capable_members(
                    session, to, exclude=from_agent, required_capabilities=required_capabilities
                )
                recipient_count = len(members)

        async with self._session_factory() as session:
            await self._persist_message(
                session, msg_id, from_agent, to, msg_type, content, timestamp,
                recipient_count, thread_id, parent_message_id, reply_to, correlation_id,
                required_capabilities or None,
            )

        envelope = {
            "id": str(msg_id),
            "from_agent": from_agent,
            "to": to,
            "type": msg_type,
            "content": content,
            "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
            "thread_id": str(thread_id) if thread_id else None,
            "parent_message_id": str(parent_message_id) if parent_message_id else None,
            "reply_to": reply_to,
            "correlation_id": correlation_id,
            "required_capabilities": required_capabilities or None,
        }

        if msg_type == "dm":
            await self._route_dm(envelope)
        elif msg_type == "group":
            await self._route_group(envelope)

    async def _route_dm(self, envelope: dict):
        await self._redis.publish(f"dm:{envelope['to']}", envelope)

    async def _route_group(self, envelope: dict):
        await self._redis.publish(f"group:{envelope['to']}", envelope)

    async def deliver_from_pubsub(self, envelope: dict):
        """Called by redis subscriber — deliver to local agent if connected."""
        msg_type = envelope.get("type", "dm")

        # reply_to routing: if this is a reply, deliver to the reply_to agent directly
        if envelope.get("reply_to") and msg_type == "dm":
            to = envelope["to"]
            if self._manager.is_local(to):
                await self._deliver_local(to, envelope)
            return

        if msg_type == "dm":
            to = envelope["to"]
            if self._manager.is_local(to):
                await self._deliver_local(to, envelope)
        elif msg_type == "group":
            group_id = envelope["to"]
            sender = envelope["from_agent"]
            required_capabilities = envelope.get("required_capabilities") or []
            async with self._session_factory() as session:
                members = await self._get_capable_members(
                    session, group_id, exclude=sender,
                    required_capabilities=required_capabilities,
                )
            # backpressure: filter out saturated agents
            available = await self._filter_available(members)
            for agent_id in available:
                if self._manager.is_local(agent_id):
                    await self._deliver_local(agent_id, envelope)

    async def _get_capable_members(
        self, session: AsyncSession, group_id: str, exclude: str | None,
        required_capabilities: list[str]
    ) -> list[str]:
        """Group members that satisfy required_capabilities."""
        if required_capabilities:
            # join Agent only when capability filtering is needed
            stmt = (
                select(GroupMembership.agent_id)
                .join(Agent, Agent.agent_id == GroupMembership.agent_id)
                .where(GroupMembership.group_id == group_id)
            )
            if exclude:
                stmt = stmt.where(GroupMembership.agent_id != exclude)
            for cap in required_capabilities:
                stmt = stmt.where(Agent.capabilities.any(cap))
        else:
            stmt = select(GroupMembership.agent_id).where(
                GroupMembership.group_id == group_id
            )
            if exclude:
                stmt = stmt.where(GroupMembership.agent_id != exclude)
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def _filter_available(self, agent_ids: list[str]) -> list[str]:
        """Return agents that have capacity (in-flight claims < max_concurrent)."""
        if not agent_ids:
            return []
        async with self._session_factory() as session:
            # get max_concurrent for each agent
            agents = (await session.execute(
                select(Agent.agent_id, Agent.max_concurrent)
                .where(Agent.agent_id.in_(agent_ids))
            )).all()
            limits = {row.agent_id: row.max_concurrent for row in agents}

            # count active (claimed) claims per agent
            in_flight = (await session.execute(
                select(Claim.agent_id, func.count(Claim.id).label("cnt"))
                .where(
                    Claim.agent_id.in_(agent_ids),
                    Claim.status == "claimed",
                )
                .group_by(Claim.agent_id)
            )).all()
            in_flight_map = {row.agent_id: row.cnt for row in in_flight}

        available = []
        for aid in agent_ids:
            limit = limits.get(aid, 1)
            current = in_flight_map.get(aid, 0)
            if current < limit:
                available.append(aid)
            else:
                logger.debug("agent %s saturated (%d/%d), skipping", aid, current, limit)
        return available

    async def _deliver_local(self, agent_id: str, envelope: dict) -> bool:
        msg_id = uuid.UUID(envelope["id"])

        async with self._session_factory() as session:
            stmt = (
                pg_insert(DeliveryAttempt)
                .values(message_id=msg_id, agent_id=agent_id)
                .on_conflict_do_nothing(constraint="uq_delivery_message_agent")
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 0:
                logger.debug("msg %s already delivered to %s, skipping", msg_id, agent_id)
                return True

        return await self._manager.send_json(agent_id, envelope)

    async def handle_ack(self, agent_id: str, message_id: str):
        """Process ACK — insert ack, mark delivered when all recipients acked."""
        msg_id = uuid.UUID(message_id)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(Ack)
                .values(message_id=msg_id, agent_id=agent_id, acked_at=datetime.now(timezone.utc))
                .on_conflict_do_nothing(constraint="uq_ack_message_agent")
            )
            await session.execute(stmt)

            msg = await session.get(Message, msg_id)
            if msg and msg.status != "delivered":
                ack_count = await session.execute(
                    select(func.count()).select_from(Ack).where(Ack.message_id == msg_id)
                )
                if ack_count.scalar() >= msg.recipient_count:
                    msg.status = "delivered"

            await session.commit()
        logger.debug("ack recorded: msg=%s agent=%s", msg_id, agent_id)

    async def complete_claim(self, agent_id: str, message_id: str, failed: bool = False):
        """Mark a claim as completed or failed (called by worker after finishing task)."""
        msg_id = uuid.UUID(message_id)
        new_status = "failed" if failed else "completed"
        async with self._session_factory() as session:
            claim = (await session.execute(
                select(Claim).where(Claim.message_id == msg_id, Claim.agent_id == agent_id)
            )).scalar_one_or_none()
            if claim:
                claim.status = new_status
                await session.commit()
        logger.debug("claim %s marked %s by %s", message_id[:8], new_status, agent_id)

    async def _persist_message(
        self, session: AsyncSession, msg_id, from_agent, to, msg_type,
        content, timestamp, recipient_count, thread_id, parent_message_id,
        reply_to, correlation_id, required_capabilities,
    ):
        stmt = (
            pg_insert(Message)
            .values(
                id=msg_id,
                from_agent=from_agent,
                to=to,
                type=msg_type,
                content=content,
                status="pending",
                timestamp=timestamp,
                recipient_count=recipient_count,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                reply_to=reply_to,
                correlation_id=correlation_id,
                required_capabilities=required_capabilities,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(stmt)
        await session.commit()
