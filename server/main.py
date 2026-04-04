import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import selectors
import sys
import uuid

# Windows: psycopg async requires SelectorEventLoop, not ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select, func, text
from starlette.requests import Request

from db.database import engine, async_session
from db.models import Base, GroupMembership, Message, Agent, Ack, Claim, DeliveryAttempt, VALID_AGENT_STATUSES
from registry.agent_registry import AgentRegistry
from server.connection_manager import ConnectionManager, validate_agent_id, MAX_MESSAGE_SIZE
from server.claim_sweeper import ClaimSweeper
from server.message_router import MessageRouter
from server.redis_pubsub import RedisPubSub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SERVER_ID = os.getenv("SERVER_ID", f"server-{uuid.uuid4().hex[:8]}")
API_SECRET = os.getenv("API_SECRET", "dev-secret-change-me")

manager = ConnectionManager()
redis_ps = RedisPubSub()
registry: AgentRegistry | None = None
router: MessageRouter | None = None
sweeper: ClaimSweeper | None = None

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_SECRET:
        raise HTTPException(status_code=403, detail="invalid token")
    return credentials.credentials


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry, router, sweeper

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await redis_ps.connect()
    registry = AgentRegistry(redis_ps, async_session)
    router = MessageRouter(manager, redis_ps, async_session)
    sweeper = ClaimSweeper(async_session, redis_ps)
    sweeper.start()

    await redis_ps.psubscribe("dm:*", "group:*", handler=router.deliver_from_pubsub)

    logger.info("server %s started", SERVER_ID)
    yield

    sweeper.stop()
    await redis_ps.disconnect()
    await engine.dispose()
    logger.info("server %s stopped", SERVER_ID)


app = FastAPI(title="Agent Messaging Server", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "server_id": SERVER_ID}


@app.get("/agents")
async def list_agents(_=Depends(verify_token)):
    return {"agents": manager.connected_agents}


@app.get("/agents/capability/{capability}")
async def find_by_capability(capability: str, _=Depends(verify_token)):
    agents = await registry.find_by_capability(capability)
    return {"agents": agents}


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@app.post("/agents/register")
async def register_agent(body: dict, _=Depends(verify_token)):
    agent_id = body.get("agent_id", "").strip()
    if not agent_id or not validate_agent_id(agent_id):
        raise HTTPException(400, "invalid agent_id")
    capabilities = body.get("capabilities", [])

    api_key = secrets.token_urlsafe(32)

    async with async_session() as session:
        existing = await session.get(Agent, agent_id)
        if existing and existing.api_key_hash:
            raise HTTPException(409, f"agent '{agent_id}' already registered")
        if existing:
            existing.api_key_hash = _hash_key(api_key)
            existing.capabilities = capabilities
        else:
            session.add(Agent(
                agent_id=agent_id,
                api_key_hash=_hash_key(api_key),
                capabilities=capabilities,
                status="offline",
            ))
        await session.commit()

    logger.info("registered agent %s", agent_id)
    return {"agent_id": agent_id, "api_key": api_key}


@app.post("/agents/{agent_id}/rotate-key")
async def rotate_key(agent_id: str, body: dict, _=Depends(verify_token)):
    old_key = body.get("old_key", "")
    async with async_session() as session:
        agent = await session.get(Agent, agent_id)
        if not agent or not agent.api_key_hash:
            raise HTTPException(404, "agent not found")
        if agent.api_key_hash != _hash_key(old_key):
            raise HTTPException(403, "invalid old key")
        new_key = secrets.token_urlsafe(32)
        agent.api_key_hash = _hash_key(new_key)
        await session.commit()
    return {"agent_id": agent_id, "api_key": new_key}


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, _=Depends(verify_token)):
    async with async_session() as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(404, "agent not found")
        await session.delete(agent)
        await session.commit()
    manager.disconnect(agent_id)
    logger.info("deleted agent %s", agent_id)
    return {"deleted": agent_id}


DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML.read_text(encoding="utf-8")


@app.get("/api/dashboard/agents")
async def dashboard_agents():
    async with async_session() as session:
        result = await session.execute(select(Agent))
        agents = result.scalars().all()
        return [
            {
                "agent_id": a.agent_id,
                "status": a.status,
                "capabilities": a.capabilities or [],
                "server_id": a.server_id,
                "connected_at": a.connected_at.isoformat() if a.connected_at else None,
            }
            for a in agents
        ]


@app.get("/api/dashboard/stats")
async def dashboard_stats():
    async with async_session() as session:
        total_msgs = (await session.execute(select(func.count(Message.id)))).scalar() or 0
        delivered = (await session.execute(
            select(func.count(Message.id)).where(Message.status == "delivered")
        )).scalar() or 0
        pending = (await session.execute(
            select(func.count(Message.id)).where(Message.status == "pending")
        )).scalar() or 0
        total_claims = (await session.execute(select(func.count(Claim.id)))).scalar() or 0
        active = (await session.execute(
            select(func.count(Agent.agent_id)).where(Agent.status != "offline")
        )).scalar() or 0
        return {
            "connected": len(manager.connected_agents),
            "total_messages": total_msgs,
            "delivered": delivered,
            "pending": pending,
            "total_claims": total_claims,
            "active_agents": active,
        }


@app.get("/api/dashboard/message-flow")
async def dashboard_message_flow():
    async with async_session() as session:
        result = await session.execute(
            select(
                Message.from_agent, Message.to, Message.type,
                func.count(Message.id).label("count")
            ).group_by(Message.from_agent, Message.to, Message.type)
        )
        return [
            {"from_agent": row.from_agent, "to": row.to, "type": row.type, "count": row.count}
            for row in result.all()
        ]


@app.get("/api/dashboard/messages")
async def dashboard_messages(limit: int = Query(default=50, le=200)):
    async with async_session() as session:
        result = await session.execute(
            select(Message).order_by(Message.timestamp.desc()).limit(limit)
        )
        msgs = result.scalars().all()
        return [
            {
                "id": str(m.id),
                "from_agent": m.from_agent,
                "to": m.to,
                "type": m.type,
                "content": m.content,
                "status": m.status,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            }
            for m in msgs
        ]


@app.websocket("/ws/{agent_id}")
async def websocket_endpoint(ws: WebSocket, agent_id: str):
    if not validate_agent_id(agent_id):
        await ws.close(code=4400, reason="invalid agent_id")
        return

    # auth: per-agent key or admin key
    token = ws.query_params.get("token", "")
    if token == API_SECRET:
        pass  # admin key always works
    else:
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent or not agent.api_key_hash or agent.api_key_hash != _hash_key(token):
                await ws.close(code=4403, reason="unauthorized")
                return

    connected = await manager.connect(agent_id, ws)
    if not connected:
        return

    caps = ws.query_params.get("capabilities", "")
    capabilities = [c.strip() for c in caps.split(",") if c.strip()]

    groups = ws.query_params.get("groups", "")
    group_list = [g.strip() for g in groups.split(",") if g.strip()]

    await registry.register(agent_id, capabilities, SERVER_ID)

    # join groups
    if group_list:
        await registry.join_groups(agent_id, group_list)

    try:
        while True:
            raw = await ws.receive_text()

            if len(raw) > MAX_MESSAGE_SIZE:
                await ws.send_json({"error": "message too large", "max_bytes": MAX_MESSAGE_SIZE})
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"error": "invalid json"})
                continue

            try:
                action = data.get("action")

                if action == "ack":
                    message_id = data.get("message_id")
                    if not message_id:
                        await ws.send_json({"error": "missing message_id"})
                        continue
                    await router.handle_ack(agent_id, message_id)

                elif action == "send":
                    payload = data.get("payload", {})
                    if "to" not in payload:
                        await ws.send_json({"error": "missing 'to' in payload"})
                        continue
                    payload["from_agent"] = agent_id
                    await router.route(payload)

                elif action == "status":
                    status = data.get("status")
                    if status not in VALID_AGENT_STATUSES:
                        await ws.send_json({"error": f"invalid status, must be one of {VALID_AGENT_STATUSES}"})
                        continue
                    await registry.update_status(agent_id, status)

                elif action == "join_group":
                    group_id = data.get("group_id")
                    if group_id:
                        await registry.join_groups(agent_id, [group_id])

                elif action == "leave_group":
                    group_id = data.get("group_id")
                    if group_id:
                        await registry.leave_group(agent_id, group_id)

                elif action == "complete_claim":
                    message_id = data.get("message_id")
                    failed = data.get("failed", False)
                    if not message_id:
                        await ws.send_json({"error": "missing message_id"})
                        continue
                    await router.complete_claim(agent_id, message_id, failed=failed)

                else:
                    await ws.send_json({"error": "unknown action", "action": action})

            except Exception:
                logger.exception("error processing message from %s", agent_id)
                await ws.send_json({"error": "internal server error"})

    except WebSocketDisconnect:
        logger.info("agent %s disconnected", agent_id)
    except Exception:
        logger.exception("error on ws for %s", agent_id)
    finally:
        manager.disconnect(agent_id)
        await registry.unregister(agent_id)
