from contextlib import ExitStack
import fcntl
from pathlib import Path

from . import resources
from .execution import Canceled, Unknown
from .workspace import WorkbenchError


class DeliveryFailure(WorkbenchError):
    def __init__(self, message, observations, stopped):
        super().__init__(message); self.observations = observations; self.stopped = stopped


def execute(executor, job, emit, canceled):
    members = sorted(job["input"]["members"], key=lambda item: item["requirement_id"])
    observations, stopped = [], False
    with ExitStack() as stack:
        try:
            for member in members:
                path = executor.settings.data_dir / "locks" / (member["requirement_id"] + ".lock")
                path.parent.mkdir(parents=True, exist_ok=True)
                lock = stack.enter_context(path.open("a"))
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise Unknown("成员仍被旧执行占用，不能采集第二个基线") from None
            for member in members:
                emit("delivery", {"text": "停止并核实项目：" + member["project"]["name"]})
                workspace = Path(member["workspace_path"])
                executor.cleanup_attempts(member["requirement_id"])
                if (workspace / "workspace.json").exists(): resources.stop_workspace(executor.settings.data_dir, workspace)
                observed = executor.observe(workspace)
                observations.append({"requirement_id": member["requirement_id"], "tree_digest": observed.get("snapshot", {}).get("tree_digest"), "environment": "STOPPED" if "snapshot" in observed else "NOT_CREATED"})
            stopped = True
            if canceled.is_set(): raise Canceled("交付操作已取消，所有成员已停止，代码与证据保留")
            result = {"delivery_observations": observations, "delivery_stopped": True}
            if job["input"]["request"]["kind"] in ("panorama", "panorama_check"):
                from .panorama import execute as execute_panorama
                result["panorama_output"] = execute_panorama(executor, job, emit, canceled)
            elif job["input"]["request"]["kind"] != "reconcile":
                from .delivery_export import build
                result["delivery_export"] = build(executor, job, observations)
            return result
        except Exception as failure:
            if isinstance(failure, (Unknown, Canceled)):
                failure.observations, failure.stopped = observations, stopped
                raise
            raise DeliveryFailure(str(failure), observations, stopped) from None
