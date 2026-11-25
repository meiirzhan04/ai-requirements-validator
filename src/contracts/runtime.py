from typing import Generic, Literal, Self, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, SecretStr, model_validator

from .enums import PipelineStage
from .source import Contract, Count, Digest, Id, Positive, Text


class ProviderSettings(Contract):
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    model: Text = "gemini-3.5-flash-lite"
    timeout_ms: Positive = 60_000


class RunMetadata(Contract):
    run_id: Id
    document_version: Digest
    started_at: AwareDatetime
    model: Text
    agent_revision: Text
    prompt_version: Id
    origin: Literal["live", "synthetic"]
    duration_seconds: float = Field(ge=0)
    request_attempts: Count
    input_tokens: Count | None = None
    output_tokens: Count | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if self.origin == "synthetic" and self.request_attempts:
            raise ValueError("Synthetic fixture cannot claim live requests")
        return self


class ProgressEvent(Contract):
    stage: PipelineStage
    state: Literal["started", "completed", "failed"]
    message: Text


T = TypeVar("T", bound=BaseModel)


class StageResult(Contract, Generic[T]):
    stage: PipelineStage
    output: T
    attempts: Positive
    input_tokens: Count | None = None
    output_tokens: Count | None = None
