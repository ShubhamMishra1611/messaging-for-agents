# WhatsApp for AI Agents

Messaging infrastructure for AI agents inspired by WhatsApp's architecture. Agents DM each other, post tasks to groups, and idle agents claim work atomically.

## Architecture

```
[Agent A] --WS--> [FastAPI Server 1] --Redis Pub/Sub--> [FastAPI Server 2] --WS--> [Agent B]
                         |                                       |
                    [PostgreSQL] <--- shared DB / ack table ---> [PostgreSQL]
```

**Layer 4 LB** in front of FastAPI servers (sticky sessions by agent_id for WS).

## Components

### WebSocket Server (`server/`)
- FastAPI with `/ws/{agent_id}` endpoint
- `ConnectionManager` — local WS registry, rejects duplicate agent_ids
- `MessageRouter` — DM vs group routing, exactly-once via delivery_attempts table
- `RedisPubSub` — cross-server message bridge
- Auth via bearer token on HTTP, query param on WS
- Per-message error handling (bad message doesn't kill connection)
- Input validation, message size limits, agent_id regex

### Agent Registry (`registry/`)
- Postgres (persistent) + Redis cache (fast lookup)
- Capability-based discovery: "who can do NLP?" → list of idle agents
- Group membership model with join/leave
- Status tracking: idle, busy, offline

### Persistence & Exactly-Once (`db/`)
- `messages` — every message stored with UUID, sender, receiver, status, recipient_count
- `delivery_attempts` — INSERT before sending (not TOCTOU check-then-send)
- `acks` — receiver dedup, message marked "delivered" only when all recipients ack
- `claims` — atomic task claiming via `INSERT ON CONFLICT DO NOTHING`
- `group_memberships` — who belongs to which group

### Demo Agents (`agents/`)
- `BaseAgent` — WS client with auto-ack, reconnect loop (not recursive), auth
- `WorkerAgent` — claims tasks atomically, processes with local LLM (Ollama)
- `OrchestratorAgent` — decomposes goals via LLM, posts tasks to group
- `llm.py` — async Ollama wrapper

## Exactly-Once Flow

```
1. Agent sends message with UUID
2. Server persists (status=pending)
3. Server publishes to Redis channel dm:{to} or group:{group_id}
4. Target server receives, INSERT delivery_attempt (atomic lock)
5. If INSERT succeeds → deliver. If conflict → skip (another server delivered)
6. Agent receives, sends ACK
7. Server inserts ack, marks delivered when ack_count >= recipient_count
```

## Group Task Claiming

```
1. Orchestrator posts to group:workers
2. All group members receive the message
3. Each tries: INSERT INTO claims (message_id, agent_id) ON CONFLICT DO NOTHING
4. Check .returning(Claim.id).fetchone() — only one gets a row back
5. Winner processes task, others skip
```

## Quick Start

```bash
# start infra
docker compose up -d

# install deps
pip install -r requirements.txt

# run server
python -c "
import asyncio, sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import uvicorn
uvicorn.run('server.main:app', host='0.0.0.0', port=8000)
"

# run demo (3 workers + orchestrator, needs Ollama with qwen3 models)
python demo.py
```

## Tech Stack
- **FastAPI** + websockets — async WS server
- **Redis** — pub/sub for cross-server routing, agent status cache
- **PostgreSQL** + SQLAlchemy async — persistence, exactly-once, claims
- **psycopg3** — async Postgres driver
- **Ollama** — local LLM inference for demo agents
- **asyncio** — fully async, no blocking I/O

## Key Design Decisions

| Decision | Why |
|----------|-----|
| Delivery attempts table over TOCTOU ack check | Prevents race between check and send across servers |
| `.returning().fetchone()` over `rowcount` | psycopg3 returns -1 for rowcount with ON CONFLICT DO NOTHING |
| Always publish to Redis (even local) | Consistency + auditability across all server instances |
| Group membership table | Prevents broadcasting to all agents, only group members receive |
| Per-message try/except in WS handler | One bad message doesn't kill the connection |
| No FK on claims table | Workers claim by message_id received over WS, FK would require message to exist in local DB first |

## Project Structure

```
messaging_for_agents/
├── server/
│   ├── main.py               # FastAPI app, WS endpoint, auth
│   ├── connection_manager.py  # local WS registry
│   ├── message_router.py      # routing + exactly-once delivery
│   └── redis_pubsub.py        # Redis pub/sub + cache
├── registry/
│   └── agent_registry.py      # register, lookup, groups, capabilities
├── db/
│   ├── database.py            # SQLAlchemy async engine
│   ├── models.py              # Message, Agent, Ack, Claim, etc.
│   └── init.sql               # raw SQL schema
├── agents/
│   ├── base_agent.py          # WS client base class
│   ├── orchestrator_agent.py  # posts tasks via LLM decomposition
│   ├── worker_agent.py        # claims + processes with LLM
│   └── llm.py                 # Ollama async wrapper
├── demo.py                    # end-to-end demo script
├── test_claim.py              # claim atomicity test
├── docker-compose.yml         # Postgres + Redis + 2 servers
├── Dockerfile
└── requirements.txt
```
