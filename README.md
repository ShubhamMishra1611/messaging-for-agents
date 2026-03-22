# Agent Message Bus

Distributed messaging infrastructure for autonomous AI agents. Horizontally scalable WebSocket servers, Redis pub/sub for cross-node routing, PostgreSQL-backed exactly-once delivery, and atomic work distribution via `INSERT ON CONFLICT`.

## Architecture

```
[Agent A] --WS--> [FastAPI Node 1] --Redis Pub/Sub--> [FastAPI Node 2] --WS--> [Agent B]
                         |                                       |
                    [PostgreSQL] <--- shared DB / ack table ---> [PostgreSQL]
```

L4 load balancer in front, sticky sessions by `agent_id` for persistent WS connections.

## Components

### WebSocket Server (`server/`)
- `/ws/{agent_id}` — persistent duplex channel per agent
- `ConnectionManager` — per-node WS registry, rejects duplicate `agent_id` connections
- `MessageRouter` — DM vs group fan-out, exactly-once via `delivery_attempts` table
- `RedisPubSub` — cross-node message bridge using pattern subscriptions (`dm:*`, `group:*`)
- Bearer token auth on HTTP, query param auth on WS handshake
- Per-message error isolation (bad payload doesn't tear down the connection)
- Input validation: `agent_id` regex `^[a-zA-Z0-9_-]{1,128}$`, 1MB message cap, status enum

### Agent Registry (`registry/`)
- PostgreSQL (persistent) + Redis cache (TTL 300s) for fast lookup
- Capability-based discovery: query idle agents by skill tags
- Group membership model — `group_memberships` table with join/leave over WS
- Agent lifecycle: `idle` → `busy` → `idle` | `offline` on disconnect

### Persistence & Exactly-Once (`db/`)
- `messages` — UUID-keyed, stores `recipient_count` for group delivery tracking
- `delivery_attempts` — atomic lock before send: `INSERT ON CONFLICT` prevents duplicate delivery across nodes (no TOCTOU race)
- `acks` — `(message_id, agent_id)` unique, message marked `delivered` only when `count(acks) >= recipient_count`
- `claims` — atomic work distribution: `INSERT ON CONFLICT DO NOTHING RETURNING id`, winner gets a row back, losers get `NULL`
- `group_memberships` — scopes group delivery to members only

### Agent SDK (`agents/`)
- `BaseAgent` — async WS client, auto-ack, iterative reconnect with exponential backoff, configurable auth
- `WorkerAgent` — claims tasks atomically, delegates to local LLM (Ollama)
- `OrchestratorAgent` — decomposes goals via LLM, distributes subtasks to group
- `llm.py` — thin async httpx wrapper around Ollama `/api/chat`

## Exactly-Once Delivery

```
1. Sender transmits message with client-generated UUID
2. Server persists message (status=pending, recipient_count=N)
3. Server publishes envelope to Redis channel dm:{agent_id} or group:{group_id}
4. Receiving node's pubsub handler attempts INSERT INTO delivery_attempts (atomic)
5. INSERT succeeds → deliver over WS. Conflict → another node already delivered, skip.
6. Agent sends ACK {message_id}
7. Server inserts ack row. When count(acks) >= recipient_count → status=delivered
```

## Atomic Work Distribution

```
1. Orchestrator publishes task to group:workers
2. All group members receive the message via Redis fan-out
3. Each worker: INSERT INTO claims (message_id, agent_id) ON CONFLICT DO NOTHING RETURNING id
4. Exactly one gets a non-NULL return → that worker owns the task
5. Winner sets status=busy, processes, sends result DM, sets status=idle
```

Note: uses `.returning(Claim.id).fetchone()` instead of `rowcount` — psycopg3 returns `-1` for `rowcount` with `ON CONFLICT DO NOTHING`.

## Quick Start

```bash
docker compose up -d
pip install -r requirements.txt

# start server (Windows needs SelectorEventLoop for psycopg async)
python -c "
import asyncio, sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import uvicorn
uvicorn.run('server.main:app', host='0.0.0.0', port=8000)
"

# run demo (requires Ollama with qwen3:0.6b and qwen3:1.7b)
python demo.py
```

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| `delivery_attempts` INSERT before send | Eliminates TOCTOU race in exactly-once — check-then-send across nodes is fundamentally broken |
| `.returning().fetchone()` over `rowcount` | psycopg3 + SQLAlchemy implicit RETURNING makes `rowcount` return -1 on `ON CONFLICT DO NOTHING` |
| Always publish to Redis, even for local agents | Uniform routing path, no split-brain between local and cross-node delivery |
| Group membership table | Fan-out scoped to members only — no broadcast storms to unrelated agents |
| Per-message try/except in WS handler | Fault isolation: malformed payload returns error JSON, doesn't sever the connection |
| No FK on claims | Workers claim by `message_id` received over WS — FK to `messages` would fail if worker connects to a different DB shard or the message hasn't replicated yet |
| SelectorEventLoop on Windows | psycopg3 async requires selector-based I/O, ProactorEventLoop is incompatible |
