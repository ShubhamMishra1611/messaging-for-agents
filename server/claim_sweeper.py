"""Background sweeper: reclaims timed-out claims, routes failed tasks to DLQ."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import Claim, Message

logger = logging.getLogger(__name__)

SWEEP_INTERVAL = 30        # seconds between sweeps
MAX_ATTEMPTS = 3           # tasks failing this many times go to DLQ
DLQ_GROUP = "dead_letter"  # group to route unrecoverable tasks


class ClaimSweeper:
    def __init__(self, session_factory, redis_pubsub):
        self._session_factory = session_factory
        self._redis = redis_pubsub
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="claim-sweeper")

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _run(self):
        while True:
            try:
                await asyncio.sleep(SWEEP_INTERVAL)
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("sweeper error")

    async def _sweep(self):
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            # find expired claimed tasks
            expired = (await session.execute(
                select(Claim)
                .where(
                    Claim.status == "claimed",
                    Claim.expires_at != None,
                    Claim.expires_at < now,
                )
            )).scalars().all()

            if not expired:
                return

            logger.info("sweeper: found %d expired claims", len(expired))

            for claim in expired:
                msg = await session.get(Message, claim.message_id)
                if not msg:
                    await session.delete(claim)
                    continue

                if claim.attempt_count >= MAX_ATTEMPTS:
                    # route to dead letter queue
                    claim.status = "timed_out"
                    msg.status = "dead_letter"
                    await session.commit()

                    dlq_envelope = {
                        "id": str(uuid.uuid4()),
                        "from_agent": "system",
                        "to": DLQ_GROUP,
                        "type": "group",
                        "content": {
                            "original_message_id": str(msg.id),
                            "original_task": msg.content,
                            "failed_agent": claim.agent_id,
                            "attempts": claim.attempt_count,
                            "reason": "timed_out",
                        },
                        "thread_id": str(msg.thread_id) if msg.thread_id else None,
                    }
                    await self._redis.publish(f"group:{DLQ_GROUP}", dlq_envelope)
                    logger.warning(
                        "task %s sent to DLQ after %d attempts (last: %s)",
                        str(msg.id)[:8], claim.attempt_count, claim.agent_id,
                    )
                else:
                    # reclaim: delete claim so another worker can pick it up
                    attempt_count = claim.attempt_count
                    await session.delete(claim)
                    await session.commit()

                    # re-publish original message for redelivery
                    envelope = {
                        "id": str(msg.id),
                        "from_agent": msg.from_agent,
                        "to": msg.to,
                        "type": msg.type,
                        "content": msg.content,
                        "timestamp": msg.timestamp.isoformat(),
                        "thread_id": str(msg.thread_id) if msg.thread_id else None,
                        "required_capabilities": msg.required_capabilities,
                        "_attempt": attempt_count + 1,
                    }

                    # update attempt count on new claim when worker re-claims
                    # we pass attempt in envelope so worker can include it
                    await self._redis.publish(f"group:{msg.to}", envelope)
                    logger.info(
                        "task %s re-queued (attempt %d)",
                        str(msg.id)[:8], attempt_count + 1,
                    )
