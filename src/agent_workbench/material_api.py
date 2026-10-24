"""Authenticated material resources; model output is never a public HTML page."""
from fastapi import Depends, Header
from fastapi.responses import FileResponse
from sqlalchemy import and_, select

from .material_export import verify_export
from .material_schemas import MaterialStart, MaterialStep
from .materials import Materials
from .store import Missing, documents, material_exports, material_rounds, public


def install(app, settings, database, user):
    service = Materials(database)
    prefix = "/api/v1/requirements/{requirement_id}/materials"

    @app.get(prefix, dependencies=[Depends(user)])
    def read(requirement_id: str, round_id: str | None = None):
        return service.read(requirement_id, round_id)

    @app.post(prefix, status_code=201, dependencies=[Depends(user)])
    def start(requirement_id: str, value: MaterialStart, idempotency_key: str | None = Header(default=None, max_length=100)):
        return service.start(requirement_id, value.model_dump(), idempotency_key)

    @app.post(prefix + "/steps", status_code=202, dependencies=[Depends(user)])
    def step(requirement_id: str, value: MaterialStep, idempotency_key: str | None = Header(default=None, max_length=100)):
        return service.enqueue(requirement_id, value.model_dump(), idempotency_key)

    @app.get(prefix + "/versions/{version_id}", dependencies=[Depends(user)])
    def version(requirement_id: str, version_id: str):
        with database.engine.connect() as connection:
            row = connection.execute(select(documents).join(material_rounds, documents.c.round_id == material_rounds.c.id)
                .where(and_(documents.c.id == version_id, material_rounds.c.requirement_id == requirement_id))).mappings().first()
            if not row:
                raise Missing("材料版本不存在")
            return public(row)

    @app.get(prefix + "/exports/{export_id}", dependencies=[Depends(user)])
    def download(requirement_id: str, export_id: str):
        with database.engine.connect() as connection:
            row = connection.execute(select(material_exports).join(material_rounds, material_exports.c.round_id == material_rounds.c.id)
                .where(and_(material_exports.c.id == export_id, material_rounds.c.requirement_id == requirement_id))).mappings().first()
            if not row:
                raise Missing("导出记录不存在")
        item = public(row)
        path = verify_export(settings.data_dir, item["job_id"], item)
        return FileResponse(path, media_type="application/zip", filename=f"workbench-materials-{export_id}.zip")
