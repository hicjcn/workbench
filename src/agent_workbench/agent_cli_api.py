from fastapi import Depends
from .agent_clis import Configurations, Save


def install(app, store, user):
    service = Configurations(store)

    @app.get("/api/v1/agent-clis", dependencies=[Depends(user)])
    def read(): return service.read()

    @app.post("/api/v1/agent-clis", status_code=201, dependencies=[Depends(user)])
    def save(value: Save): return service.save(value.model_dump())

    @app.post("/api/v1/agent-clis/{config_id}/default", dependencies=[Depends(user)])
    def activate(config_id: str): return service.activate(config_id)
