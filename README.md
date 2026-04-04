# Agent Message Bus

Distributed messaging infrastructure for autonomous AI agents. Horizontally scalable WebSocket servers, Redis pub/sub for cross-node routing, PostgreSQL-backed exactly-once delivery, and atomic work distribution via `INSERT ON CONFLICT`.

## Architecture

```
[Agent A] --WS--> [FastAPI Node 1] --Redis Pub/Sub--> [FastAPI Node 2] --WS--> [Agent B]
                         |                                       |
                    [PostgreSQL] <--- shared DB / ack table ---> [PostgreSQL]
```

L4 load balancer in front, sticky sessions by `agent_id` for persistent WS connections.

---

## Quick Start

**Prerequisites:** Docker Desktop, Python 3.12+, [Ollama](https://ollama.com) with models pulled.

```bash
# pull models
ollama pull qwen3:0.6b
ollama pull qwen3:1.7b

# start postgres + redis
docker compose up -d postgres redis

# install deps
pip install -r requirements.txt
```

**Terminal 1 — server:**
```bash
# Windows
python -c "import asyncio,sys;asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy());import uvicorn;uvicorn.run('server.main:app',host='0.0.0.0',port=8000)"

# Linux/Mac
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

**Terminal 2 — run the demo:**
```bash
python demo.py
```

**Dashboard:** open `http://localhost:8000/dashboard` — live agent graph, message flow, stats.

---

## Usage Examples

### 1. Basic worker pool — distribute tasks, collect results

Register agents, spin up workers, post tasks to the group. One worker claims each task atomically.

```python
import asyncio
from agents.base_agent import BaseAgent
from agents.worker_agent import WorkerAgent

async def main():
    # register agents (admin key required, returns per-agent API key)
    worker_key = await BaseAgent.register("worker-1", capabilities=["general"])
    orch_key   = await BaseAgent.register("orchestrator", capabilities=["orchestration"])

    # worker connects and listens for tasks
    worker = WorkerAgent("1", model="qwen3:0.6b", token=worker_key)
    asyncio.create_task(worker.run())
    await asyncio.sleep(1)

    # orchestrator posts tasks to the group
    orch = BaseAgent("orchestrator", groups=["workers"], token=orch_key)
    await orch.connect()

    results = []
    orch.on_message(lambda data: results.append(data["content"]))

    await orch.send_group("workers", {"task": "Summarize the CAP theorem in 2 sentences."})

    await asyncio.wait_for(orch._listen(), timeout=60)
    print(results[0]["result"])

asyncio.run(main())
```

---

### 2. Capability routing — only qualified workers see the task

Tag workers with capabilities. Tasks with `required_capabilities` are routed only to matching agents — other workers never receive them.

```python
# register with specific capabilities
code_key    = await BaseAgent.register("coder-1",    capabilities=["code", "python"])
summary_key = await BaseAgent.register("summarizer", capabilities=["summarize", "text"])

# post a task only coders will receive
await orch.send_group(
    "workers",
    {"task": "Write a binary search in Python."},
    required_capabilities=["code"],
)

# post a task only summarizers will receive
await orch.send_group(
    "workers",
    {"task": "Summarize this paper abstract in one sentence."},
    required_capabilities=["summarize"],
)
```

---

### 3. Request / reply — synchronous feel over async transport

Block until a specific agent (or group) replies. Useful for agent-to-agent tool calls, mid-task queries, or any place you need a result before continuing.

```python
# orchestrator calls a worker like an RPC
result = await orchestrator.request(
    to="workers",
    content={"task": "What is the time complexity of merge sort?"},
    group=True,
    timeout=60.0,
)
print(result["result"])  # "O(n log n)"

# on the worker side — reply() preserves thread and correlation ID automatically
async def handle_task(data):
    answer = await run_llm(data["content"]["task"])
    await worker.reply(data, {"result": answer})

worker.on_message(handle_task)
```

---

### 4. Thread IDs — link a full workflow

Start a thread on the orchestrator. Every message in that workflow (tasks, results, follow-ups) carries the same `thread_id`, making the entire trace queryable.

```python
# start a named workflow
thread_id = orchestrator.start_thread()

# all messages in this workflow share the thread_id
task1_id = await orchestrator.send_group("workers", {"task": "Step 1: research X"})
task2_id = await orchestrator.send_group("workers", {"task": "Step 2: draft Y"})
task3_id = await orchestrator.send_group(
    "workers",
    {"task": "Step 3: review Z"},
    required_capabilities=["review"],
)

# query everything that happened in this workflow
# SELECT * FROM messages WHERE thread_id = '<thread_id>' ORDER BY timestamp;
```

---

### 5. Build your own agent — subclass BaseAgent

```python
from agents.base_agent import BaseAgent

class ReviewerAgent(BaseAgent):
    def __init__(self, token: str):
        super().__init__(
            agent_id="reviewer",
            capabilities=["review", "code"],
            groups=["workers"],
            token=token,
        )

    async def run(self):
        self.on_message(self._handle)
        self._running = True
        await super().run()

    async def _handle(self, data: dict):
        content = data.get("content", {})
        code = content.get("code", "")

        # do work
        feedback = await my_review_function(code)

        # reply or DM result back
        if data.get("reply_to"):
            await self.reply(data, {"feedback": feedback, "approved": True})
        else:
            await self.send_dm(data["from_agent"], {"feedback": feedback})
```

---

## Key Behaviours

| Behaviour | How it works |
|-----------|-------------|
| Exactly-once delivery | `INSERT INTO delivery_attempts` before send — atomic lock prevents duplicate delivery across nodes |
| Atomic task claiming | `INSERT INTO claims ON CONFLICT DO NOTHING RETURNING id` — exactly one worker gets a non-NULL row |
| Task lifecycle | Claims expire after 120s. Sweeper reclaims timed-out tasks and retries up to 3x, then routes to `dead_letter` group |
| Backpressure | `max_concurrent` per agent. Saturated workers are skipped during delivery — no wasted claim races |
| Capability routing | Group delivery filtered by `required_capabilities` — unqualified workers never see the message |
| Thread tracing | `thread_id` + `parent_message_id` + `correlation_id` on every message — full workflow trace queryable from DB |
| Per-agent auth | `POST /agents/register` → unique API key (SHA-256 hashed). WS auth verifies key matches `agent_id` |
| Reconnect | Exponential backoff (2^n, capped at 30s). Stale claims cleared on reconnect so backpressure doesn't deadlock |

## Components

### WebSocket Server (`server/`)
- `/ws/{agent_id}` — persistent duplex channel per agent
- `ConnectionManager` — per-node WS registry, rejects duplicate `agent_id` connections
- `MessageRouter` — DM vs group fan-out, capability filtering, backpressure, exactly-once
- `RedisPubSub` — cross-node message bridge using pattern subscriptions (`dm:*`, `group:*`)
- `ClaimSweeper` — background task, runs every 30s, reclaims expired claims, routes to DLQ
- Per-message error isolation — bad payload returns error JSON, doesn't sever the connection

### Agent Registry (`registry/`)
- PostgreSQL (persistent) + Redis cache (TTL 300s)
- Capability-based discovery: `GET /agents/capability/{capability}`
- Group membership via WS actions `join_group` / `leave_group`

### Persistence (`db/`)
- `messages` — UUID-keyed, `thread_id`, `correlation_id`, `required_capabilities`
- `delivery_attempts` — atomic delivery lock (no TOCTOU)
- `acks` — `(message_id, agent_id)` unique; `delivered` when `count(acks) >= recipient_count`
- `claims` — atomic work distribution with `status`, `expires_at`, `attempt_count`
- `agents` — `capabilities`, `max_concurrent`, `api_key_hash`

### Agent SDK (`agents/`)
- `BaseAgent` — WS client, thread context, request/reply, auto-ack, reconnect
- `WorkerAgent` — claims tasks, marks complete, handles retries
- `llm.py` — async httpx wrapper for Ollama

## HTTP API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | Server status |
| `POST` | `/agents/register` | admin | Register agent, returns API key |
| `DELETE` | `/agents/{id}` | admin | Remove agent |
| `POST` | `/agents/{id}/rotate-key` | admin | Rotate API key |
| `GET` | `/agents` | admin | List connected agents |
| `GET` | `/agents/capability/{cap}` | admin | Find idle agents by capability |
| `GET` | `/dashboard` | — | Monitoring UI |
| `GET` | `/api/dashboard/*` | — | Dashboard data endpoints |

## WS Actions (client → server)

```json
{"action": "send",    "payload": {"id": "...", "to": "agent-id", "type": "dm", "content": {}, "thread_id": "...", "required_capabilities": ["code"]}}
{"action": "ack",     "message_id": "..."}
{"action": "status",  "status": "busy"}
{"action": "complete_claim", "message_id": "...", "failed": false}
{"action": "join_group",  "group_id": "..."}
{"action": "leave_group", "group_id": "..."}
```

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| `delivery_attempts` INSERT before send | Eliminates TOCTOU race — check-then-send across nodes is broken |
| `.returning().fetchone()` over `rowcount` | psycopg3 + SQLAlchemy implicit RETURNING makes `rowcount` return -1 on `ON CONFLICT DO NOTHING` |
| Always publish to Redis, even for local agents | Uniform routing path, no split-brain |
| Group membership table | Fan-out scoped to members only |
| No FK on claims | Workers claim by `message_id` over WS — FK would fail across DB shards |
| Clear claims on reconnect | Prevents backpressure deadlock when a worker crashes mid-task and restarts |
| SelectorEventLoop on Windows | psycopg3 async requires selector-based I/O |
