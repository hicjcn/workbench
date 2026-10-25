from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .material_schemas import Evidence


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Member(Input):
    requirement_id: str
    required: bool = True
    skip_reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def explained(self):
        if not self.required and not self.skip_reason.strip():
            raise ValueError("可选项目必须记录跳过原因")
        return self


class GroupInput(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=16000)
    members: list[Member] = Field(min_length=1, max_length=8)
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)
    expected_revision: str | None = None

    @model_validator(mode="after")
    def distinct(self):
        if len({item.requirement_id for item in self.members}) != len(self.members) or not any(item.required for item in self.members):
            raise ValueError("项目需求不能重复，且至少有一个必要项目")
        if not self.title.strip() or not self.actor.strip() or not self.reason.strip():
            raise ValueError("标题、确认人和原因不能为空")
        return self


class ScopeDecision(Evidence):
    expected_scope: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["APPROVED", "REJECTED"]


class Operation(Evidence):
    kind: Literal["capture", "archive", "reconcile"]
    context_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class PanoramaOperation(Evidence):
    kind: Literal["panorama", "panorama_check"]
    context_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str = Field(default="opencode/glm-5.3-flash-free", max_length=240)
    prompt: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def model_allowed(self):
        from .runtime import model_config
        model_config(self.model, True)
        return self


class Integration(Evidence):
    expected_baseline: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: Literal["PASS", "FAIL", "NOT_REQUIRED"]
    environment: str = Field(min_length=1, max_length=4000)
    actual: str = Field(min_length=1, max_length=8000)
