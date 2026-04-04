"""Worker agent — claims tasks from group, processes with local LLM."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from agents.base_agent import BaseAgent
from agents.llm import chat
from db.models import Claim

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/agent_messaging")
CLAIM_TIMEOUT_SECONDS = int(os.getenv("CLAIM_TIMEOUT_SECONDS", "120"))

_engine = create_async_engine(DB_URL, echo=False, pool_size=5, max_overflow=10)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


class WorkerAgent(BaseAgent):
    def __init__(self, worker_id: str, model: str = "qwen3:0.6b", server_url: str | None = None,
                 capabilities: list[str] | None = None, token: str | None = None):
        super().__init__(
            agent_id=f"worker-{worker_id}",
            server_url=server_url,
            capabilities=capabilities or ["general"],
            groups=["workers"],
            token=token,
        )
        self.model = model

    async def _try_claim(self, message_id: str, attempt: int = 1) -> bool:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
        async with _session_factory() as session:
            stmt = (
                pg_insert(Claim)
                .values(
                    message_id=message_id,
                    agent_id=self.agent_id,
                    status="claimed",
                    expires_at=expires_at,
                    attempt_count=attempt,
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(Claim.id)
            )
            result = await session.execute(stmt)
            row = result.fetchone()
            await session.commit()
            return row is not None

    async def _complete_claim(self, message_id: str, failed: bool = False):
        new_status = "failed" if failed else "completed"
        async with _session_factory() as session:
            claim = (await session.execute(
                select(Claim).where(
                    Claim.message_id == message_id,
                    Claim.agent_id == self.agent_id,
                )
            )).scalar_one_or_none()
            if claim:
                claim.status = new_status
                await session.commit()

    async def _process_task(self, data: dict):
        msg_id = data.get("id")
        if not msg_id:
            return
        content = data.get("content", {})
        task = content.get("task", "unknown")
        attempt = data.get("_attempt", 1)

        claimed = await self._try_claim(msg_id, attempt=attempt)
        if not claimed:
            logger.info("[%s] task %s already claimed, skipping", self.agent_id, msg_id[:8])
            return

        logger.info("[%s] CLAIMED task: %s (model=%s, attempt=%d)", self.agent_id, task, self.model, attempt)
        await self.set_status("busy")

        failed = False
        try:
            result = await chat(
                model=self.model,
                prompt=task,
                system="You are a helpful worker agent. Complete the task concisely.",
            )
            logger.info("[%s] completed: %s", self.agent_id, result[:100])
        except Exception as e:
            result = f"error: {e}"
            failed = True
            logger.exception("[%s] LLM call failed", self.agent_id)

        await self._complete_claim(msg_id, failed=failed)

        reply_to = data.get("reply_to") or data.get("from_agent")
        if reply_to:
            response_content = {
                "result": result,
                "task": task,
                "worker": self.agent_id,
                "model": self.model,
                "thread_id": data.get("thread_id"),
            }
            if data.get("correlation_id"):
                # this was a request/reply call — use reply()
                await self.reply(data, response_content)
            else:
                await self.send_dm(
                    reply_to, response_content,
                    thread_id=data.get("thread_id"),
                    parent_message_id=data.get("id"),
                )

        await self.set_status("idle")

    async def run(self):
        self.on_message(self._process_task)
        self._running = True
        await super().run()


async def main():
    workers = [
        WorkerAgent("0", model="qwen3:1.7b", capabilities=["general", "reasoning"]),
        WorkerAgent("1", model="qwen3:0.6b", capabilities=["general", "fast"]),
        WorkerAgent("2", model="qwen3:0.6b", capabilities=["general", "fast"]),
    ]
    try:
        await asyncio.gather(*(w.run() for w in workers))
    finally:
        await _engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
