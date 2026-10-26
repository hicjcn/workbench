import json
import shutil
import time
from pathlib import Path
from fastapi import Depends, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, insert, select, update

from .platform_support import is_link
from .delivery import Delivery
from .delivery_schemas import GroupInput, ScopeDecision, Operation, Integration, PanoramaOperation
from .delivery_export import verify
from .material_schemas import Evidence
from .resources import controller_id
from .store import Missing, delivery_exports, runner_reports, jobs, public


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    report: dict


def install(app, settings, store, user, runner):
    service = Delivery(store)
    prefix = "/api/v1/deliveries"

    @app.get(prefix, dependencies=[Depends(user)])
    def listing(): return service.list()

    @app.post(prefix, status_code=201, dependencies=[Depends(user)])
    def create(value: GroupInput): return service.save(value.model_dump())

    @app.get(prefix + "/{group_id}", dependencies=[Depends(user)])
    def detail(group_id: str): return service.read(group_id)

    @app.post(prefix + "/{group_id}/revisions", status_code=201, dependencies=[Depends(user)])
    def revise(group_id: str, value: GroupInput): return service.save(value.model_dump(), group_id)

    @app.post(prefix + "/{group_id}/scope", status_code=201, dependencies=[Depends(user)])
    def scope(group_id: str, value: ScopeDecision): return service.decide(group_id, value.model_dump(mode="json"))

    @app.post(prefix + "/{group_id}/integration", status_code=201, dependencies=[Depends(user)])
    def integration(group_id: str, value: Integration): return service.integrate(group_id, value.model_dump(mode="json"))

    @app.post(prefix + "/{group_id}/reopen", dependencies=[Depends(user)])
    def reopen(group_id: str, value: Evidence): return service.reopen(group_id, value.model_dump(mode="json"))

    @app.post(prefix + "/{group_id}/operations", status_code=202, dependencies=[Depends(user)])
    def operate(group_id: str, value: Operation, idempotency_key: str | None = Header(default=None, max_length=100)):
        return service.enqueue(group_id, value.model_dump(mode="json"), idempotency_key)

    @app.post(prefix + "/{group_id}/panorama", status_code=202, dependencies=[Depends(user)])
    def panorama(group_id: str, value: PanoramaOperation, idempotency_key: str | None = Header(default=None, max_length=100)):
        return service.enqueue(group_id, value.model_dump(mode="json"), idempotency_key)

    @app.get(prefix + "/{group_id}/exports/{export_id}", dependencies=[Depends(user)])
    def download(group_id: str, export_id: str):
        with store.engine.connect() as connection:
            row = connection.execute(select(delivery_exports).where(and_(delivery_exports.c.id == export_id, delivery_exports.c.group_id == group_id))).mappings().first()
        if not row: raise Missing("交付包不存在")
        item = public(row)
        path = verify(settings.data_dir, item["job_id"], item, item["input_digest"])
        return FileResponse(path, media_type="application/zip", filename=f"workbench-delivery-{export_id}.zip")

    @app.post("/internal/runtime", dependencies=[Depends(runner)])
    def runtime_report(value: RuntimeReport):
        with store.engine.begin() as connection:
            if store.engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as upsert
            else:
                from sqlalchemy.dialects.sqlite import insert as upsert
            data = {"owner": value.owner, "report_json": json.dumps(value.report, ensure_ascii=False), "updated_at": time.time()}
            connection.execute(upsert(runner_reports).values(**data).on_conflict_do_update(index_elements=[runner_reports.c.owner], set_=data))
        return {"saved": True}

    @app.get("/api/v1/runtime", dependencies=[Depends(user)])
    def runtime_status():
        reports = store.list(runner_reports)
        reports = [item for item in reports if item["report"].get("controller_id") == controller_id(settings.data_dir)]
        latest = max(reports, key=lambda item: item["updated_at"], default=None)
        with store.engine.connect() as connection:
            counts = {status: len(connection.execute(select(jobs.c.id).where(jobs.c.status == status)).all()) for status in ("QUEUED", "RUNNING", "UNKNOWN")}
        disk = shutil.disk_usage(settings.data_dir)
        backup_indexes = []
        for path in (settings.data_dir / "backups").glob("backup_*.json"):
            if path.is_file() and not is_link(path) and path.stat().st_size <= 65536:
                item = json.loads(path.read_text(encoding="utf-8")); item["available"] = path.with_suffix(".zip").is_file(); backup_indexes.append(item)
        restored = settings.data_dir / "restore-info.json"
        from .settings import storage_summary
        return {"storage": storage_summary(store.engine.url), "limits": {"active_jobs": settings.max_active_jobs, "containers": settings.max_containers}, "jobs": counts,
            "resources": latest, "resources_stale": not latest or time.time() - latest["updated_at"] > 15,
            "disk_free_bytes": disk.free, "data_dir": str(settings.data_dir), "backups": backup_indexes,
            "restored": json.loads(restored.read_text(encoding="utf-8")) if restored.is_file() and not is_link(restored) else None}
