import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mixxx_backend.config import Settings
from mixxx_backend.library import BackendError
from mixxx_backend.models import (
    Acknowledge,
    BridgeClaim,
    BridgeRegister,
    BridgeState,
    CommandInput,
    QueueEdit,
    Reconcile,
    Revision,
    Rules,
    SessionCreate,
    TrackMetadata,
    TrackRegister,
)
from mixxx_backend.scheduler import Scheduler, stop_scheduler
from mixxx_backend.service import Service
from mixxx_backend.store import Store

Key = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


def create_app(settings: Settings | None = None, *, run_scheduler=True):
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app):
        settings.prepare()
        app.state.api_token = settings.token("api-token")
        app.state.bridge_token = settings.token("bridge-token")
        store = Store(settings.data_dir / "mixxx.sqlite3")
        app.state.service = Service(store, settings)
        task = (
            asyncio.create_task(Scheduler(app.state.service).run())
            if run_scheduler
            else None
        )
        try:
            yield
        finally:
            if task:
                await stop_scheduler(task)
            store.close()

    app = FastAPI(
        title="Mixxx Agent Backend",
        version="0.1.0",
        lifespan=lifespan,
        description="Local DJ orchestration. Commands require native bridge confirmation.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )

    @app.exception_handler(BackendError)
    async def backend_error(request, error):
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request validation failed",
                    "fields": [list(item["loc"]) for item in error.errors()],
                }
            },
        )

    def service(request: Request):
        return request.app.state.service

    def authenticate(request: Request, token_name, authorization):
        expected = "Bearer " + getattr(request.app.state, token_name)
        if not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise BackendError(
                "UNAUTHORIZED", "Valid bearer token required", 401
            )

    def api_auth(request: Request, authorization: str = Header(default="")):
        authenticate(request, "api_token", authorization)

    def bridge_auth(request: Request, authorization: str = Header(default="")):
        authenticate(request, "bridge_token", authorization)

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(api_auth)])
    bridge = APIRouter(
        prefix="/api/v1/bridge", dependencies=[Depends(bridge_auth)]
    )

    @app.get("/api/v1/health")
    async def health(svc=Depends(service)):
        svc.store.db.execute("SELECT 1")
        return {
            "status": "ok",
            "database": "ok",
            "playback_engine": "mixxx",
            "version": "0.1.0",
        }

    @api.get("/openapi.json")
    async def openapi():
        return app.openapi()

    @api.get("/capabilities")
    async def capabilities(svc=Depends(service)):
        return {
            "bridge": svc.bridge_status(),
            "local_files": True,
            "session_planning": True,
            "apple_music_deck_streaming": False,
            "apple_music_catalogue": False,
            "chatgpt_plugin_installed": False,
        }

    @api.get("/mixxx/status")
    async def status(svc=Depends(service)):
        return svc.bridge_status()

    @api.post("/tracks", status_code=201)
    async def register(data: TrackRegister, svc=Depends(service)):
        inspected = await asyncio.to_thread(svc.library.inspect, data.path)
        return svc.register_track(inspected)

    @api.get("/tracks")
    async def tracks(
        q: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        svc=Depends(service),
    ):
        items = [
            t
            for t in svc.library.tracks()
            if q.casefold() in (t["title"] + " " + t["artist"]).casefold()
        ]
        return {"items": items[offset : offset + limit], "total": len(items)}

    @api.get("/tracks/{identifier}")
    async def track(identifier: str, svc=Depends(service)):
        item = svc.require("track", identifier)
        return {**item, "available": svc.library.available(item)}

    @api.put("/tracks/{identifier}/metadata")
    async def metadata(
        identifier: str, data: TrackMetadata, svc=Depends(service)
    ):
        return svc.metadata(identifier, data)

    @api.post("/dj/sessions", status_code=201)
    async def create(data: SessionCreate, svc=Depends(service)):
        return svc.create_session(data)

    @api.get("/dj/sessions")
    async def sessions(svc=Depends(service)):
        return {"items": svc.store.all("session")}

    @api.get("/dj/sessions/active")
    async def active(svc=Depends(service)):
        items = [
            s
            for s in svc.store.all("session")
            if s["status"] in {"running", "suspended"}
        ]
        return {
            "session": svc.session_status(items[0]["id"]) if items else None
        }

    @api.get("/dj/sessions/{identifier}")
    async def session(identifier: str, svc=Depends(service)):
        return svc.session_status(identifier)

    @api.put("/dj/sessions/{identifier}/settings")
    async def rules(
        identifier: str, revision: int, data: Rules, svc=Depends(service)
    ):
        return svc.update_rules(identifier, revision, data)

    @api.get("/dj/sessions/{identifier}/plan")
    async def preview(identifier: str, svc=Depends(service)):
        return svc.preview(identifier)

    @api.post("/dj/sessions/{identifier}/plan")
    async def build_plan(
        identifier: str, data: Revision, svc=Depends(service)
    ):
        return svc.set_queue(identifier, data.revision)

    @api.put("/dj/sessions/{identifier}/queue")
    async def queue(identifier: str, data: QueueEdit, svc=Depends(service)):
        return svc.set_queue(identifier, data.revision, data.track_ids)

    @api.get("/dj/sessions/{identifier}/coverage")
    async def coverage(identifier: str, svc=Depends(service)):
        return svc.coverage(svc.require("session", identifier))

    @api.get("/dj/sessions/{identifier}/upcoming")
    async def upcoming(identifier: str, svc=Depends(service)):
        return svc.upcoming(identifier)

    @api.post("/dj/sessions/{identifier}/start")
    async def start(identifier: str, data: Revision, svc=Depends(service)):
        return svc.start_session(identifier, data.revision)

    @api.post("/dj/sessions/{identifier}/end")
    async def end(identifier: str, svc=Depends(service)):
        return svc.end_session(identifier)

    @api.post("/dj/commands", status_code=202)
    async def command(
        data: CommandInput, idempotency_key: Key, svc=Depends(service)
    ):
        return svc.command(data, idempotency_key)

    @api.post("/dj/sessions/{identifier}/commands", status_code=202)
    async def session_command(
        identifier: str,
        data: CommandInput,
        idempotency_key: Key,
        svc=Depends(service),
    ):
        return svc.command(data, idempotency_key, identifier)

    @api.get("/dj/commands/{identifier}")
    async def command_status(identifier: str, svc=Depends(service)):
        with svc.store.transaction():
            svc.expire()
            return svc.require("command", identifier)

    @api.post("/dj/commands/{identifier}/reconcile")
    async def reconcile(
        identifier: str, data: Reconcile, svc=Depends(service)
    ):
        return svc.reconcile(identifier, data.outcome)

    @api.get("/events")
    async def events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        svc=Depends(service),
    ):
        return {"items": svc.store.events(after, limit)}

    @bridge.post("/register", status_code=201)
    async def register_bridge(data: BridgeRegister, svc=Depends(service)):
        return svc.register_bridge(data)

    @bridge.post("/state")
    async def state(data: BridgeState, svc=Depends(service)):
        return svc.state(data)

    @bridge.post("/commands/claim")
    async def claim(data: BridgeClaim, svc=Depends(service)):
        return svc.claim(data.instance_id)

    @bridge.post("/commands/{identifier}/ack")
    async def acknowledge(
        identifier: str, data: Acknowledge, svc=Depends(service)
    ):
        return svc.acknowledge(identifier, data)

    app.include_router(api)
    app.include_router(bridge)
    return app
