from fastapi import Depends
from .profiles import Profiles, Save


def install(app, settings, store, user):
    service = Profiles(store, settings.data_dir, settings)

    @app.get("/api/v1/projects/{project_id}/profiles", dependencies=[Depends(user)])
    def read(project_id: str): return service.read(project_id)

    @app.post("/api/v1/projects/{project_id}/profiles", status_code=201, dependencies=[Depends(user)])
    def save(project_id: str, value: Save): return service.save(project_id, value.model_dump())
