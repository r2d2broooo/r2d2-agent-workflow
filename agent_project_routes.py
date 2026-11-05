"""Authenticated project-session API; background turns survive browser disconnects."""
import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from shared import agent_projects


def build_agent_project_router(require_auth):
    router = APIRouter()
    cached = {"ts": 0, "data": None}
    overview_lock = asyncio.Lock()

    @router.on_event("startup")
    async def startup():
        agent_projects.recover()

    def error(exc):
        return JSONResponse({"error": str(exc)}, status_code=404 if isinstance(exc, KeyError) else 400)

    @router.get("/api/agent-project")
    async def overview(request: Request):
        if not require_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        async with overview_lock:
            if cached["data"] is None or time.monotonic() - cached["ts"] > 60:
                from shared import project_observability, chat_log
                data = await asyncio.to_thread(project_observability.overview)
                # Existing task list, shared across projects; never infer project ownership.
                data["tasks"] = await asyncio.to_thread(chat_log.get_tasks)
                cached.update(ts=time.monotonic(), data=data)
        return {"name": "R2D2 Agents", "providers": agent_projects.providers(),
                "sessions": agent_projects.list_sessions(), "root": str(agent_projects.PROJECT_ROOT),
                "overview": cached["data"]}

    @router.post("/api/agent-project/sessions")
    async def create(request: Request):
        if not require_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            return agent_projects.create(body.get("title", ""), body.get("provider", ""), body.get("discussion", False),
                                         body.get("project", agent_projects.ROOT.parent.name))
        except (ValueError, TypeError) as exc:
            return error(exc)

    @router.get("/api/agent-project/sessions/{sid}")
    async def session(sid: str, request: Request):
        if not require_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return agent_projects.get(sid)
        except (ValueError, KeyError) as exc:
            return error(exc)

    @router.post("/api/agent-project/sessions/{sid}/{action}")
    async def action(sid: str, action: str, request: Request):
        if not require_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            if action == "send":
                agent_projects.start(sid, body.get("message", ""))
            elif action == "acknowledge":
                agent_projects.acknowledge(sid)
            else:
                return JSONResponse({"error": "unknown action"}, status_code=404)
            return agent_projects.get(sid)
        except (ValueError, KeyError) as exc:
            return error(exc)

    return router
