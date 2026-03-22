"""Orchestrator agent — uses LLM to decompose work, posts tasks to workers."""

import asyncio
import logging

from agents.base_agent import BaseAgent
from agents.llm import chat

logger = logging.getLogger(__name__)


class OrchestratorAgent(BaseAgent):
    def __init__(self, server_url: str | None = None):
        super().__init__(
            agent_id="orchestrator",
            server_url=server_url,
            capabilities=["orchestration"],
            groups=["workers"],
        )
        self.results: dict[str, dict] = {}

    async def post_task(self, task_description: str, group: str = "workers"):
        msg_id = await self.send_group(group, {"task": task_description})
        logger.info("[orchestrator] posted task '%s' (msg=%s)", task_description, msg_id[:8])
        return msg_id

    async def _handle_response(self, data: dict):
        content = data.get("content", {})
        worker = content.get("worker", "?")
        task = content.get("task", "?")
        result = content.get("result", "")
        model = content.get("model", "?")
        logger.info("[orchestrator] result from %s (%s):\n  task: %s\n  result: %s", worker, model, task, result[:200])
        self.results[data.get("id", "")] = content

    async def run_demo(self):
        # decompose BEFORE connecting so WS doesn't idle-timeout during LLM call
        logger.info("[orchestrator] asking qwen3:1.7b to decompose work...")
        decomposition = await chat(
            model="qwen3:1.7b",
            prompt="Break this goal into 3 short, independent tasks (one line each, no numbering):\n\nGoal: Prepare a brief competitive analysis of Python web frameworks",
            system="You are a task planner. Output only the task list, nothing else. /no_think",
        )
        tasks = [line.strip() for line in decomposition.strip().splitlines() if line.strip()]
        logger.info("[orchestrator] decomposed into %d tasks: %s", len(tasks), tasks)

        await self.connect()

        for task_desc in tasks:
            await self.post_task(task_desc)
            await asyncio.sleep(0.5)

        # listen for worker responses
        self.on_message(self._handle_response)
        logger.info("[orchestrator] waiting for results...")

        # wait up to 120s for all results
        try:
            await asyncio.wait_for(self._listen(), timeout=120)
        except asyncio.TimeoutError:
            logger.info("[orchestrator] timeout — got %d results", len(self.results))

        await self.disconnect()


async def main():
    agent = OrchestratorAgent()
    await agent.run_demo()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
