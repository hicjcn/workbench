from contextlib import ExitStack
import time
from fastapi import Depends, Header
from sqlalchemy import select, update

from . import locking
from . import store as t
from .docker_management import Acknowledge, ImageOperation, Images, RecipeInput, guard
from .resources import controller_id
from .store import Conflict, Missing


def install(app, settings, store, user, runner, Claim, Lease, Finish):
    service = Images(store)

    @app.get("/api/v1/docker", dependencies=[Depends(user)])
    def status():
        service.expire(); store.expire()
        reports = [r for r in store.list(t.runner_reports) if r["report"].get("controller_id") == controller_id(settings.data_dir)]
        latest = max(reports, key=lambda r: r["updated_at"], default=None)
        fresh = bool(latest and time.time() - latest["updated_at"] <= 15)
        recipes = service.recipes()
        return {"connected": bool(fresh and latest["report"].get("available")), "stale": not fresh,
            "updated_at": latest["updated_at"] if latest else None, "trial": False,
            "images": latest["report"].get("images", []) if latest else [],
            "images_available": bool(fresh and latest["report"].get("images_available")),
            "containers": latest["report"].get("containers", []) if latest else [],
            "recipes": recipes, "operations": store.list(t.docker_jobs)[:100]}

    @app.post("/api/v1/docker/recipes", status_code=201, dependencies=[Depends(user)])
    def save(value: RecipeInput): return service.save(value.model_dump())

    @app.post("/api/v1/docker/operations", status_code=202, dependencies=[Depends(user)])
    def operate(value: ImageOperation, idempotency_key: str = Header(min_length=1, max_length=100)):
        return service.enqueue(value.model_dump(), idempotency_key)

    @app.post("/api/v1/docker/operations/{identity}/cancel", dependencies=[Depends(user)])
    def cancel(identity: str): return service.cancel(identity)

    @app.post("/api/v1/docker/operations/{identity}/acknowledge", dependencies=[Depends(user)])
    def acknowledge(identity: str, value: Acknowledge):
        if not value.checked_daemon or not value.actor.strip() or not value.reason.strip(): raise Conflict("需要记录实际核实人员、依据及 Docker 检查确认")
        service.expire()
        locks = settings.data_dir / "locks"; locks.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            for name in ("docker-images.lock", "docker-command.lock"):
                lock = stack.enter_context((locks / name).open("a+b"))
                try: locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
                except BlockingIOError: raise Conflict("旧镜像操作仍持有主机锁，请等待原执行退出") from None
            with store.engine.begin() as c:
                guard(c, store)
                row = c.execute(select(t.docker_jobs).where(t.docker_jobs.c.id == identity).with_for_update()).mappings().first()
                if not row: raise Missing("镜像任务不存在")
                if row["status"] != "UNKNOWN": raise Conflict("只有待核实任务可人工结束核实")
                c.execute(update(t.docker_jobs).where(t.docker_jobs.c.id == identity).values(status="ACKNOWLEDGED", lease_until=None,
                    result_json=t.encoded({"manual_acknowledgement": value.model_dump(), "execution_succeeded": False}),
                    error="用户登记已核实 Docker；原操作未被认定成功，不会重放", updated_at=time.time()))
        return store.get(t.docker_jobs, identity)

    @app.post("/internal/docker/claim", dependencies=[Depends(runner)])
    def claim(value: Claim): return {"job": service.claim(value.owner)}

    @app.post("/internal/docker/{identity}/heartbeat", dependencies=[Depends(runner)])
    def heartbeat(identity: str, value: Lease): return service.heartbeat(identity, value.owner, value.epoch)

    @app.post("/internal/docker/{identity}/finish", dependencies=[Depends(runner)])
    def finish(identity: str, value: Finish): return service.finish(identity, value.owner, value.epoch, value.status, value.result, value.error)
