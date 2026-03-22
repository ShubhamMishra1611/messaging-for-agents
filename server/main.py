import asyncio
import json
import logging
import os
import re
import selectors
import sys
import uuid

# Windows: psycopg async requires SelectorEventLoop, not ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.requests import Request

from db.database import engine, async_session
from db.models import Base, GroupMembership, VALID_AGENT_STATUSES
from registry.agent_registry import AgentRegistry
from server.connection_manager import ConnectionManager, validate_agent_id, MAX_MESSAGE_SIZE
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

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_SECRET:
        raise HTTPException(status_code=403, detail="invalid token")
    return credentials.credentials


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry, router

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await redis_ps.connect()
    registry = AgentRegistry(redis_ps, async_session)
    router = MessageRouter(manager, redis_ps, async_session)

    await redis_ps.psubscribe("dm:*", "group:*", handler=router.deliver_from_pubsub)

    logger.info("server %s started", SERVER_ID)
    yield

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


@app.websocket("/ws/{agent_id}")
async def websocket_endpoint(ws: WebSocket, agent_id: str):
    # auth: check token in query param (WS can't use headers easily)
    token = ws.query_params.get("token", "")
    if token != API_SECRET:
        await ws.close(code=4403, reason="unauthorized")
        return

    if not validate_agent_id(agent_id):
        await ws.close(code=4400, reason="invalid agent_id")
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
