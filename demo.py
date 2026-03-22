"""Quick demo: register agents, 3 workers + orchestrator, short tasks, full flow."""

import asyncio
import sys
import logging

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from agents.base_agent import BaseAgent
from agents.worker_agent import WorkerAgent, _engine
from agents.llm import chat


async def register_or_get_key(agent_id: str, capabilities: list[str]) -> str:
    """Register agent, return key. If already registered, re-register via admin."""
    import httpx
    url = "http://localhost:8000"
    admin_key = "dev-secret-change-me"
    async with httpx.AsyncClient() as client:
        # delete if exists (clean slate)
        await client.delete(f"{url}/agents/{agent_id}",
                            headers={"Authorization": f"Bearer {admin_key}"})
        resp = await client.post(f"{url}/agents/register",
                                 json={"agent_id": agent_id, "capabilities": capabilities},
                                 headers={"Authorization": f"Bearer {admin_key}"})
        resp.raise_for_status()
        key = resp.json()["api_key"]
        logging.info("registered %s → key=%s...", agent_id, key[:12])
        return key


async def main():
    # register all agents and get per-agent keys
    keys = {}
    agent_defs = [
        ("worker-0", ["general"]),
        ("worker-1", ["general"]),
        ("worker-2", ["general"]),
        ("orchestrator", ["orchestration"]),
    ]
    for aid, caps in agent_defs:
        keys[aid] = await register_or_get_key(aid, caps)

    # start workers with per-agent keys
    workers = [
        WorkerAgent("0", model="qwen3:0.6b", capabilities=["general"], token=keys["worker-0"]),
        WorkerAgent("1", model="qwen3:0.6b", capabilities=["general"], token=keys["worker-1"]),
        WorkerAgent("2", model="qwen3:1.7b", capabilities=["general"], token=keys["worker-2"]),
    ]

    orchestrator = BaseAgent(
        agent_id="orchestrator",
        capabilities=["orchestration"],
        groups=["workers"],
        token=keys["orchestrator"],
    )

    results = []

    async def on_result(data):
        content = data.get("content", {})
        if "result" in content:
            results.append(content)
            logging.info(
                "RESULT from %s (%s):\n  task: %s\n  answer: %s",
                content.get("worker"), content.get("model"),
                content.get("task"), content.get("result", "")[:200],
            )

    orchestrator.on_message(on_result)

    # start all concurrently
    async def run_workers():
        await asyncio.gather(*(w.run() for w in workers))

    worker_task = asyncio.create_task(run_workers())

    # give workers time to connect
    await asyncio.sleep(2)

    # connect orchestrator
    await orchestrator.connect()

    # post 3 simple tasks
    tasks = [
        "What is 2+2? Answer in one word.",
        "Name one planet. Answer in one word.",
        "Is Python compiled or interpreted? One sentence.",
    ]

    for t in tasks:
        msg_id = await orchestrator.send_group("workers", {"task": t})
        logging.info("POSTED: '%s' (msg=%s)", t, msg_id[:8])
        await asyncio.sleep(0.3)

    # wait for results
    logging.info("Waiting for results...")
    try:
        await asyncio.wait_for(orchestrator._listen(), timeout=180)
    except asyncio.TimeoutError:
        pass

    logging.info("=== GOT %d/%d RESULTS ===", len(results), len(tasks))
    for r in results:
        logging.info("  %s (%s): %s", r.get("worker"), r.get("model"), r.get("result", "")[:100])

    await orchestrator.disconnect()
    for w in workers:
        await w.disconnect()
    worker_task.cancel()
    await _engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
