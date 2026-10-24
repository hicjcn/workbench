"""Typed material definitions and separately registered human evidence."""
from datetime import datetime, timezone
from typing import Literal
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .workspace import WorkbenchError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Named(Input):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_present(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class SummaryDesign(Named):
    markdown: str = Field(min_length=1, max_length=60000)

    @field_validator("markdown")
    @classmethod
    def body_present(cls, value):
        if not value.strip():
            raise ValueError("正文不能为空")
        return value


class Case(Named):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    preconditions: str = Field(max_length=4000)
    steps: list[str] = Field(min_length=1, max_length=30)
    expected: str = Field(min_length=1, max_length=4000)
    required: bool = True


class TestPlan(Named):
    environment: str = Field(min_length=1, max_length=4000)
    cases: list[Case] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_cases(self):
        if not self.environment.strip():
            raise ValueError("请填写明确的测试环境")
        if len({case.id for case in self.cases}) != len(self.cases) or not any(case.required for case in self.cases):
            raise ValueError("用例 ID 必须唯一，且至少有一个必需用例")
        if any(not case.expected.strip() or not case.steps or any(not step.strip() for step in case.steps) for case in self.cases):
            raise ValueError("用例必须有可执行步骤及预期结果")
        return self


class Metric(Input):
    name: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=2000)

    @field_validator("name", "target")
    @classmethod
    def specified(cls, value):
        if not value.strip():
            raise ValueError("性能指标和目标不能为空")
        return value.strip()


class PerformancePlan(TestPlan):
    workload: str = Field(min_length=1, max_length=4000)
    metrics: list[Metric] = Field(min_length=1, max_length=30)

    @field_validator("workload")
    @classmethod
    def specified(cls, value):
        if not value.strip():
            raise ValueError("请填写明确的负载模型")
        return value.strip()


DOCUMENT_SCHEMAS = {"summary_design": SummaryDesign, "test_plan": TestPlan, "performance_plan": PerformancePlan}


def document(kind, content):
    if kind not in DOCUMENT_SCHEMAS or len(json.dumps(content, ensure_ascii=False)) > 80000:
        raise WorkbenchError("材料类型不支持或超过 80,000 字符限制")
    try:
        return DOCUMENT_SCHEMAS[kind].model_validate(content).model_dump()
    except ValidationError:
        raise WorkbenchError("材料未符合模板，不能作为有效版本保存") from None


def parse_document(kind, text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3]
    try:
        return document(kind, json.loads(text))
    except ValueError:
        raise WorkbenchError("模型未输出完整 JSON 材料，不自动重试") from None


class Evidence(Input):
    actor: str = Field(min_length=1, max_length=160)
    occurred_at: datetime
    reason: str = Field(min_length=1, max_length=8000)
    evidence: str = Field(min_length=1, max_length=4000)

    @field_validator("actor", "reason", "evidence")
    @classmethod
    def present(cls, value):
        if not value.strip():
            raise ValueError("人员、原因和证据引用不能为空")
        return value.strip()

    @field_validator("occurred_at")
    @classmethod
    def dated(cls, value):
        if value.tzinfo is None or value.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
            raise ValueError("请填写带时区的实际发生时间，不能晚于当前时间")
        return value


class Supervisor(Evidence):
    decision: Literal["APPROVED", "REJECTED"]


class PerformanceDecision(Evidence):
    decision: Literal["UNDECIDED", "REQUIRED", "NOT_REQUIRED"]


class CaseResult(Input):
    version_id: str
    case_id: str
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    actual: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)

    @field_validator("actual", "evidence")
    @classmethod
    def result_present(cls, value):
        if not value.strip():
            raise ValueError("实际结果及证据不能为空")
        return value.strip()


class TestExecution(Evidence):
    environment: str = Field(min_length=1, max_length=4000)
    results: list[CaseResult] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_results(self):
        if len({(item.version_id, item.case_id) for item in self.results}) != len(self.results):
            raise ValueError("同一登记不能重复填写用例")
        if not self.environment.strip():
            raise ValueError("请填写实际测试环境")
        return self


class Feedback(Evidence):
    kind: Literal["BUG", "REQUIREMENT_CHANGE", "ARCHITECTURE_CHANGE", "DOCUMENT_COMMENT"]
    scope_unchanged: bool = False
    new_requirement: dict | None = None


class Resolution(Evidence):
    feedback_id: str
    decision: Literal["RESOLVED", "ACCEPTED"]


class Edit(Input):
    kind: Literal["summary_design", "test_plan", "performance_plan"]
    expected_version: str
    content: dict
    reason: str = Field(min_length=1, max_length=4000)


RECORD_SCHEMAS = {"supervisor_review": Supervisor, "performance_decision": PerformanceDecision,
    "test_execution": TestExecution, "feedback": Feedback, "resolve_feedback": Resolution, "acceptance": Evidence}


class MaterialStart(Input):
    flow_id: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class MaterialStep(Input):
    round_id: str
    step_key: Literal["summary_design", "test_plan", "performance_plan", "edit_document", "supervisor_review",
        "performance_decision", "test_execution", "feedback", "resolve_feedback", "acceptance", "export"]
    context_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt: str = Field(default="", max_length=16000)
    data: dict = Field(default_factory=dict)
