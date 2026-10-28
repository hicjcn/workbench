"""Consistent progress logs without request bodies, command arguments or secrets."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import traceback

_context = ContextVar("workbench_log_context", default={})
_secrets = ()
_fields = {"job_id", "requirement_id", "action", "step", "status", "duration_ms", "elapsed_ms", "exit_code",
    "request_id", "method", "route", "http_status", "operation", "error_type", "frames", "pid", "role", "mode",
    "instance", "attempt", "count", "total", "repository", "event_kind", "bytes"}
_secret_name = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|(?:^|_)KEY|DATABASE_URL|PROXY|BASE_URL", re.I)
_logger = logging.getLogger("agent_workbench")
_logger.addHandler(logging.NullHandler())
_logger.propagate = False


def redact(value):
    text = str(value)
    values = {*_secrets, *[value for name, value in os.environ.items() if value and _secret_name.search(name)]}
    for secret in sorted(values, key=len, reverse=True):
        if secret: text = text.replace(secret, "[REDACTED]")
    return text


def line(value):
    return re.sub(r"[\x00-\x1f\x7f]", lambda match: json.dumps(match[0])[1:-1], redact(value))[:4000]


class ProgressFormatter(logging.Formatter):
    def format(self, record):
        stamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        fields = getattr(record, "progress_fields", {})
        suffix = " ".join(f"{key}={json.dumps(line(value), ensure_ascii=False)}" for key, value in fields.items() if value is not None and key in _fields)
        result = f"{stamp} {record.levelname} [{record.name}] {line(record.getMessage())}"
        if suffix: result += " " + suffix
        if record.exc_info:
            # Stack locations help diagnosis; exception values/locals can contain credentials.
            frames = traceback.extract_tb(record.exc_info[2])[-8:]
            result += " error_type=" + record.exc_info[0].__name__
            result += " frames=" + json.dumps([f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames])
        return result


def configure(mode="concise", *, secrets=(), stream=None):
    global _secrets
    if mode not in ("concise", "detailed"): raise ValueError("log_mode 必须是 concise 或 detailed")
    _secrets = tuple(value for value in secrets if value)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(ProgressFormatter())
    # Only replace handlers owned by this package. Reconfiguration is idempotent.
    _logger.handlers[:] = [handler]
    _logger.setLevel(logging.DEBUG if mode == "detailed" else logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.setLevel(logging.WARNING)
        logger.propagate = False


@contextmanager
def context(**fields):
    token = _context.set({**_context.get(), **{key: value for key, value in fields.items() if key in _fields}})
    try: yield
    finally: _context.reset(token)


def event(component, step, message, *, detailed=False, level=None, **fields):
    logger = logging.getLogger("agent_workbench." + component)
    severity = level if level is not None else logging.DEBUG if detailed else logging.INFO
    if logger.isEnabledFor(severity):
        logger.log(severity, message, extra={"progress_fields": {**_context.get(), "step": step, **{key: value for key, value in fields.items() if key in _fields}}})


@contextmanager
def operation(component, step, message, *, detailed=True, **fields):
    started = time.monotonic()
    event(component, step, message + "：开始", detailed=detailed, **fields)
    try:
        yield
    except BaseException as error:
        event(component, step, message + "：失败", level=logging.WARNING, error_type=type(error).__name__,
            duration_ms=round((time.monotonic()-started)*1000), **fields)
        event(component, step + ".trace", "失败位置", detailed=True, frames=";".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(error.__traceback__)[-8:]))
        raise
    else:
        event(component, step, message + "：完成", detailed=detailed, duration_ms=round((time.monotonic()-started)*1000), **fields)


def progress(emit, step, text, *, detailed=False, **fields):
    emit("stage", {"step": step, "level": "DEBUG" if detailed else "INFO", "text": text, **fields})
