"""Untrusted model reports are data, never control-plane decisions."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .workspace import WorkbenchError


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=12000)


class Analysis(Report):
    impacted_paths: list[str] = Field(max_length=200)
    plan: str = Field(min_length=1, max_length=20000)
    risks: list[str] = Field(max_length=30)


class Development(Report):
    verification_notes: str = Field(min_length=1, max_length=12000)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["BLOCKER", "MAJOR", "MINOR"]
    path: str = Field(max_length=1000)
    line: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1, max_length=4000)
    recommendation: str = Field(min_length=1, max_length=2000)


class Review(Report):
    findings: list[Finding] = Field(max_length=100)


def validate_report(step_key, value):
    if len(json.dumps(value, ensure_ascii=False)) > 80000:
        raise WorkbenchError("报告超过 80,000 字符限制；保留日志但不能推进流程")
    schema = Analysis if step_key == "analysis" else Development if step_key == "development" else Review
    try:
        return schema.model_validate(value).model_dump()
    except ValidationError:
        raise WorkbenchError("模型报告未符合本步骤结构，请查看日志并明确重试") from None


def parse_report(step_key, text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    try:
        value = json.loads(text)
    except ValueError:
        raise WorkbenchError("模型未输出完整 JSON 报告，请查看日志；不自动修复或重放") from None
    return validate_report(step_key, value)
