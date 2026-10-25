"""Local authenticated API. Docker access belongs exclusively to the Runner."""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import store as tables
from .settings import Settings
from .store import Conflict, Missing, Store, identifier
from .workspace import WorkbenchError, git
from .workflow import Workflow, current


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "title", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()


class Login(Input):
    token: str = Field(max_length=200)


class ProjectInput(Input):
    name: str = Field(min_length=1, max_length=160)
    source_path: str = Field(min_length=1, max_length=2000)
    image: str = Field(default="agent-workbench-m1:local", min_length=1, max_length=240)


class RequirementInput(Input):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=32000)
    acceptance: str = Field(default="", max_length=16000)


class ActionInput(Input):
    action: Literal["prepare", "start", "stop", "refresh", "task", "reconcile", "preflight", "build", "verify", "app_start", "app_stop", "app_check", "app_logs"]
    prompt: str = Field(default="", max_length=32000)


class ReviewInput(Input):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def rejection_reason(self):
        if self.decision == "REJECTED" and not self.reason.strip():
            raise ValueError("退回时请填写原因")
        return self


class Claim(Input):
    owner: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


class Lease(Claim):
    epoch: int = Field(ge=1)


class EventInput(Lease):
    seq: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=60)
    payload: dict


class Finish(Lease):
    status: Literal["SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"]
    result: dict = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=4000)


class Observation(Input):
    snapshot: dict | None = None
    diff: str = Field(default="", max_length=500000)
    unreadable: bool = False
    diff_truncated: bool = False


class RevisionInput(Input):
    expected_revision: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=32000)
    acceptance: str = Field(default="", max_length=16000)
    reason: str = Field(min_length=1, max_length=4000)


class WorkflowStart(Input):
    review_models: list[str] = Field(min_length=2, max_length=4)
    verification_command: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=4000)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Disposition(Input):
    decision: Literal["ACCEPTED", "FALSE_POSITIVE"]
    reason: str = Field(min_length=1, max_length=4000)


class WorkflowStep(Input):
    flow_id: str
    step_key: str = Field(max_length=40)
    prompt: str = Field(default="", max_length=16000)
    decision: Literal["APPROVED", "REJECTED"] | None = None
    reason: str = Field(default="", max_length=4000)
    dispositions: dict[str, Disposition] = Field(default_factory=dict)


def create_app(settings: Settings, store: Store | None = None):
    app = FastAPI(title="Agent 开发工作台", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    database = store or Store(settings.database_url, settings.lease_seconds, settings.max_active_jobs)
    app.state.store = database
    workflow = Workflow(database, settings.model)

    @app.exception_handler(Conflict)
    async def conflict(_, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(Missing)
    async def missing(_, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(IntegrityError)
    async def simultaneous_change(_, error):
        return JSONResponse({"detail": "记录已被其他操作更新，请重新载入"}, status_code=409)

    @app.exception_handler(WorkbenchError)
    async def invalid_operation(_, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if (origin and origin != f"{request.url.scheme}://{request.headers.get('host')}") or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站操作"}, status_code=403)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 2 * 1024 * 1024):
            return JSONResponse({"detail": "请求超过大小限制"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def signature(stamp):
        return hmac.new(settings.access_token.encode(), stamp.encode(), hashlib.sha256).hexdigest()

    def authenticated(request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if supplied and secrets.compare_digest(supplied, settings.access_token):
            return True
        try:
            stamp, signed = request.cookies.get("awb_session", "").split(".", 1)
            return 0 <= time.time() - int(stamp) < 8 * 3600 and secrets.compare_digest(signed, signature(stamp))
        except (ValueError, TypeError):
            return False

    def user(request: Request):
        if not authenticated(request):
            raise HTTPException(401, "请使用本机访问令牌登录")

    def runner(request: Request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not supplied or not secrets.compare_digest(supplied, settings.runner_token):
            raise HTTPException(401, "执行器认证失败")

    from .material_api import install as install_material_routes
    install_material_routes(app, settings, database, user)
    from .delivery_api import install as install_delivery_routes
    install_delivery_routes(app, settings, database, user, runner)
    from .profile_api import install as install_profile_routes
    install_profile_routes(app, settings, database, user)

    @app.get("/api/v1/session")
    def session(request: Request):
        return {"authenticated": authenticated(request), "token_file": str(settings.data_dir / "access-token")}

    @app.post("/api/v1/session")
    def login(value: Login, response: Response):
        if not secrets.compare_digest(value.token, settings.access_token):
            raise HTTPException(401, "访问令牌不正确")
        stamp = str(int(time.time()))
        response.set_cookie("awb_session", stamp + "." + signature(stamp), httponly=True, samesite="strict", max_age=8 * 3600)
        return {"authenticated": True}

    @app.delete("/api/v1/session")
    def logout(response: Response):
        response.delete_cookie("awb_session")
        return {"authenticated": False}

    @app.get("/api/v1/config", dependencies=[Depends(user)])
    def config():
        return {"model": settings.model, "data_dir": str(settings.data_dir), "mode": "single-host"}

    @app.get("/api/v1/projects", dependencies=[Depends(user)])
    def list_projects():
        return database.list(tables.projects)

    @app.post("/api/v1/projects", status_code=201, dependencies=[Depends(user)])
    def register(value: ProjectInput):
        source = Path(value.source_path).expanduser().resolve()
        if not source.is_dir() or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", value.image):
            raise HTTPException(422, "代码目录或镜像名称不正确")
        try:
            if Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source:
                raise WorkbenchError("请选择 Git 仓库根目录")
        except WorkbenchError as error:
            raise HTTPException(422, str(error)) from None
        if settings.data_dir.is_relative_to(source):
            raise HTTPException(422, "代码目录不能包含工作台的数据目录")
        record = dict(id=identifier("prj"), name=value.name.strip(), source_path=str(source), image=value.image, created_at=time.time())
        with database.engine.begin() as connection:
            connection.execute(insert(tables.projects).values(**record))
        return record

    @app.get("/api/v1/requirements", dependencies=[Depends(user)])
    def list_requirements():
        database.expire()
        return [{**item, "workspace": database.get(tables.workspaces, item["id"], exclude=("snapshot_json", "diff"))} for item in database.list(tables.requirements)]

    @app.post("/api/v1/requirements", status_code=201, dependencies=[Depends(user)])
    def create_requirement(value: RequirementInput):
        database.get(tables.projects, value.project_id)
        record = dict(id=identifier("req"), **value.model_dump(), created_at=time.time())
        with database.engine.begin() as connection:
            connection.execute(insert(tables.requirements).values(**record))
            connection.execute(insert(tables.workspaces).values(requirement_id=record["id"],
                path=str(settings.data_dir / "workspaces" / record["id"] / value.project_id),
                environment="NOT_CREATED", approval="PENDING", updated_at=time.time()))
        return {**record, "workspace": database.get(tables.workspaces, record["id"])}

    @app.get("/api/v1/requirements/{requirement_id}", dependencies=[Depends(user)])
    def detail(requirement_id: str):
        database.expire()
        requirement = database.get(tables.requirements, requirement_id)
        from .profiles import binding
        from .preflight import read_report
        from .dependencies import status as dependency_status
        from .application import status as application_status
        with database.engine.connect() as connection: profile = binding(connection, requirement_id)
        workspace = database.get(tables.workspaces, requirement_id)
        return {**requirement, "project": database.get(tables.projects, requirement["project_id"]), "profile": profile,
            "preflight": read_report(Path(workspace["path"])),
            "dependency": dependency_status(Path(workspace["path"])),
            "application": application_status(Path(workspace["path"]), workspace.get("snapshot", {}).get("tree_digest") if workspace.get("snapshot") else None),
            "workspace": database.get(tables.workspaces, requirement_id),
            "jobs": database.job_history(requirement_id),
            "reviews": database.list(tables.reviews, tables.reviews.c.requirement_id == requirement_id)}

    @app.post("/api/v1/requirements/{requirement_id}/actions", status_code=202, dependencies=[Depends(user)])
    def action(requirement_id: str, value: ActionInput, idempotency_key: str | None = Header(default=None, max_length=100)):
        database.expire()
        with database.engine.begin() as connection:
            if not connection.execute(select(tables.requirements.c.id).where(tables.requirements.c.id == requirement_id).with_for_update()).first():
                raise Missing("需求不存在")
            if value.action == "task" and current(connection, requirement_id):
                raise Conflict("此需求已开启流程，请从流程面板运行开发步骤")
            if value.action == "task" and not value.prompt.strip():
                raise HTTPException(422, "请填写 Agent 任务")
            return database.enqueue(requirement_id, value.action, {"prompt": value.prompt}, idempotency_key, connection=connection)

    @app.post("/api/v1/requirements/{requirement_id}/reviews", status_code=202, dependencies=[Depends(user)])
    def review(requirement_id: str, value: ReviewInput, idempotency_key: str | None = Header(default=None, max_length=100)):
        database.expire()
        with database.engine.begin() as connection:
            if not connection.execute(select(tables.requirements.c.id).where(tables.requirements.c.id == requirement_id).with_for_update()).first():
                raise Missing("需求不存在")
            if current(connection, requirement_id):
                raise Conflict("请完成模型评审和问题处理，再从流程面板自审")
            return database.enqueue(requirement_id, "review", value.model_dump(), idempotency_key, connection=connection)

    @app.get("/api/v1/requirements/{requirement_id}/workflow", dependencies=[Depends(user)])
    def workflow_state(requirement_id: str):
        return workflow.read(requirement_id)

    @app.get("/api/v1/requirements/{requirement_id}/workflow/runs/{flow_id}", dependencies=[Depends(user)])
    def archived_workflow(requirement_id: str, flow_id: str):
        return workflow.archived(requirement_id, flow_id)

    @app.post("/api/v1/requirements/{requirement_id}/revisions", status_code=201, dependencies=[Depends(user)])
    def revise(requirement_id: str, value: RevisionInput):
        return workflow.revise(requirement_id, value.expected_revision,
            value.model_dump(include={"title", "description", "acceptance"}), value.reason)

    @app.post("/api/v1/requirements/{requirement_id}/workflow", status_code=201, dependencies=[Depends(user)])
    def start_workflow(requirement_id: str, value: WorkflowStart):
        return workflow.start(requirement_id, value.review_models, value.verification_command, value.reason, value.digest)

    @app.post("/api/v1/requirements/{requirement_id}/workflow/steps", status_code=202, dependencies=[Depends(user)])
    def run_step(requirement_id: str, value: WorkflowStep, idempotency_key: str | None = Header(default=None, max_length=100)):
        supplied = value.model_dump(exclude={"flow_id", "step_key"})
        return workflow.enqueue(requirement_id, value.flow_id, value.step_key, supplied, idempotency_key)

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(user)])
    def job(job_id: str):
        database.expire()
        return database.get(tables.jobs, job_id)

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=[Depends(user)])
    def cancel(job_id: str):
        return database.cancel(job_id)

    @app.get("/api/v1/jobs/{job_id}/events", dependencies=[Depends(user)])
    def event_history(job_id: str, after: int = 0):
        database.get(tables.jobs, job_id)
        return database.event_list(job_id, after)

    @app.get("/api/v1/jobs/{job_id}/stream", dependencies=[Depends(user)])
    async def stream(job_id: str, request: Request):
        database.get(tables.jobs, job_id)
        try:
            after = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            raise HTTPException(422, "事件位置不正确") from None
        async def generate():
            nonlocal after
            while not await request.is_disconnected():
                batch = database.event_list(job_id, after)
                for item in batch:
                    after = item["id"]
                    yield f"id: {after}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                database.expire()
                if database.get(tables.jobs, job_id)["status"] not in ("QUEUED", "RUNNING") and len(batch) < 200:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/internal/claim", dependencies=[Depends(runner)])
    def claim(value: Claim):
        record = database.claim(value.owner)
        return {"job": record, "model": settings.model}

    @app.get("/internal/watch", dependencies=[Depends(runner)])
    def watch():
        from sqlalchemy import exists, or_
        return database.list(tables.workspaces, or_(tables.workspaces.c.approval == "APPROVED",
            exists().where((tables.flows.c.requirement_id == tables.workspaces.c.requirement_id) &
                tables.flows.c.is_current.is_(True) & tables.flows.c.state.in_(["ACTIVE", "COMPLETE"]))),
            exclude=("snapshot_json", "diff"))

    @app.post("/internal/workspaces/{requirement_id}/observation", dependencies=[Depends(runner)])
    def observation(requirement_id: str, value: Observation):
        database.idle_observation(requirement_id, value.snapshot, value.diff, value.unreadable, value.diff_truncated)
        return {"accepted": True}

    @app.post("/internal/jobs/{job_id}/heartbeat", dependencies=[Depends(runner)])
    def heartbeat(job_id: str, value: Lease):
        return database.heartbeat(job_id, value.owner, value.epoch)

    @app.post("/internal/jobs/{job_id}/events", dependencies=[Depends(runner)])
    def append(job_id: str, value: EventInput):
        return database.append_event(job_id, value.owner, value.epoch, value.seq, value.kind, value.payload)

    @app.post("/internal/jobs/{job_id}/finish", dependencies=[Depends(runner)])
    def finish(job_id: str, value: Finish):
        database.finish(job_id, value.owner, value.epoch, value.status, value.result, value.error)
        return {"accepted": True}

    if settings.web_dir and (settings.web_dir / "index.html").exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/assets", StaticFiles(directory=settings.web_dir / "assets"), name="assets")
        @app.get("/")
        def index():
            return FileResponse(settings.web_dir / "index.html")
    else:
        @app.get("/")
        def unbuilt():
            return JSONResponse({"detail": "先执行 npm --prefix web run build"}, status_code=503)
    return app

