import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Message, Ack, DeliveryAttempt, GroupMembership
from server.connection_manager import ConnectionManager
from server.redis_pubsub import RedisPubSub

logger = logging.getLogger(__name__)


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

        # for group msgs, count recipients
        recipient_count = 1
        if msg_type == "group":
            async with self._session_factory() as session:
                count = await session.execute(
                    select(func.count()).select_from(GroupMembership).where(
                        GroupMembership.group_id == to,
                        GroupMembership.agent_id != from_agent,
                    )
                )
                recipient_count = count.scalar() or 0

        async with self._session_factory() as session:
            await self._persist_message(session, msg_id, from_agent, to, msg_type, content, timestamp, recipient_count)

        envelope = {
            "id": str(msg_id),
            "from_agent": from_agent,
            "to": to,
            "type": msg_type,
            "content": content,
            "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
        }

        if msg_type == "dm":
            await self._route_dm(envelope)
        elif msg_type == "group":
            await self._route_group(envelope)

    async def _route_dm(self, envelope: dict):
        # always publish to Redis for consistency/auditability
        await self._redis.publish(f"dm:{envelope['to']}", envelope)

    async def _route_group(self, envelope: dict):
        await self._redis.publish(f"group:{envelope['to']}", envelope)

    async def deliver_from_pubsub(self, envelope: dict):
        """Called by redis subscriber — deliver to local agent if connected."""
        msg_type = envelope.get("type", "dm")
        if msg_type == "dm":
            to = envelope["to"]
            if self._manager.is_local(to):
                await self._deliver_local(to, envelope)
        elif msg_type == "group":
            group_id = envelope["to"]
            sender = envelope["from_agent"]
            members = await self._get_local_group_members(group_id, exclude=sender)
            for agent_id in members:
                await self._deliver_local(agent_id, envelope)

    async def _get_local_group_members(self, group_id: str, exclude: str | None = None) -> list[str]:
        """Return locally connected agents that are members of this group."""
        async with self._session_factory() as session:
            stmt = select(GroupMembership.agent_id).where(GroupMembership.group_id == group_id)
            if exclude:
                stmt = stmt.where(GroupMembership.agent_id != exclude)
            rows = (await session.execute(stmt)).scalars().all()
        return [aid for aid in rows if self._manager.is_local(aid)]

    async def _deliver_local(self, agent_id: str, envelope: dict) -> bool:
        msg_id = uuid.UUID(envelope["id"])

        # exactly-once: INSERT delivery_attempt BEFORE sending
        # if INSERT conflicts, another server already delivered — skip
        async with self._session_factory() as session:
            stmt = (
                pg_insert(DeliveryAttempt)
                .values(message_id=msg_id, agent_id=agent_id)
                .on_conflict_do_nothing(constraint="uq_delivery_message_agent")
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 0:
                logger.debug("msg %s already being delivered to %s, skipping", msg_id, agent_id)
                return True

        return await self._manager.send_json(agent_id, envelope)

    async def handle_ack(self, agent_id: str, message_id: str):
        """Process ACK — insert ack, mark delivered only when all recipients acked."""
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

    async def _persist_message(self, session: AsyncSession, msg_id, from_agent, to, msg_type, content, timestamp, recipient_count):
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
            )
            .on_conflict_do_nothing()
        )
        await session.execute(stmt)
        await session.commit()
