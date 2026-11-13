"""The JSON bodies the API accepts and returns."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from runtime.spec import JobSpec
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind

# One week. A longer job is a mistake more often than a plan.
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600


class JobSubmission(JobSpec):
    """The body of `POST /jobs`: the job spec plus its limits.

    The limits are stored with the job. Nothing enforces them yet.
    """

    # Left out, the API's configured default is used.
    token_budget: int | None = Field(default=None, ge=1)
    # The job's deadline is this many seconds after submission.
    timeout_seconds: int | None = Field(default=None, ge=1, le=MAX_TIMEOUT_SECONDS)

    def spec(self) -> dict[str, Any]:
        """The part that is stored as the job's spec."""
        return JobSpec.model_validate(
            self.model_dump(exclude={"token_budget", "timeout_seconds"})
        ).model_dump(exclude_none=True)


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: JobStatus
    spec: dict[str, Any]
    token_budget: int
    tokens_used: int
    deadline: datetime
    not_before: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_epoch: int
    cancel_requested: bool
    error: str | None
    created_at: datetime


class StepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: StepKind
    payload: dict[str, Any]
    lease_epoch: int
    created_at: datetime


class StepList(BaseModel):
    steps: list[StepView]
