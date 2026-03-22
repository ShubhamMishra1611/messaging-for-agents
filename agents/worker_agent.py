"""Worker agent — claims tasks from group, processes with local LLM."""

import asyncio
import logging
import os

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from agents.base_agent import BaseAgent
from agents.llm import chat
from db.models import Claim

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/agent_messaging")

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

    async def _try_claim(self, message_id: str) -> bool:
        async with _session_factory() as session:
            stmt = (
                pg_insert(Claim)
                .values(message_id=message_id, agent_id=self.agent_id)
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(Claim.id)
            )
            result = await session.execute(stmt)
            row = result.fetchone()
            await session.commit()
            return row is not None

    async def _process_task(self, data: dict):
        msg_id = data.get("id")
        if not msg_id:
            return
        content = data.get("content", {})
        task = content.get("task", "unknown")

        claimed = await self._try_claim(msg_id)
        if not claimed:
            logger.info("[%s] task %s already claimed, skipping", self.agent_id, msg_id[:8])
            return

        logger.info("[%s] CLAIMED task: %s (model=%s)", self.agent_id, task, self.model)
        print(f"[{self.agent_id}] CLAIMED: {task}", flush=True)
        await self.set_status("busy")

        try:
            result = await chat(
                model=self.model,
                prompt=task,
                system="You are a helpful worker agent. Complete the task concisely.",
            )
            logger.info("[%s] completed: %s", self.agent_id, result[:100])
        except Exception as e:
            result = f"error: {e}"
            logger.exception("[%s] LLM call failed", self.agent_id)

        if data.get("from_agent"):
            await self.send_dm(data["from_agent"], {
                "result": result,
                "task": task,
                "worker": self.agent_id,
                "model": self.model,
            })

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
