"""The agent loop: run one claimed job to a final status.

Each pass reads the step log back as a conversation (replay), does the one
thing that comes next, and writes it down as a step: a model call, or the
tool calls the model asked for. Every step is its own transaction.

Not built yet: the lease is not renewed, a job is not resumed after its
worker stops, and budget and deadline are not enforced.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse, Provider, ProviderError
from runtime.spec import JobSpec
from runtime.store.jobs import Job, add_tokens_used, update_status
from runtime.store.payload import InvalidPayload, check_json
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, append_step, read_steps
from runtime.tools.base import ToolRegistry, ToolResult
from runtime.worker.replay import replay
from runtime.worker.workspace import prepare_workspace, remove_workspace

log = logging.getLogger("runtime.worker")

# Stop reasons that end a job well. Any other reason without tool calls
# to run is a failure.
_FINISHED = frozenset({"end_turn", "stop_sequence"})
_FAILURES = {
    "max_tokens": "the model's response was cut off at max_tokens",
    "refusal": "the model declined the request",
}


@dataclass(frozen=True, slots=True)
class AgentSettings:
    default_model: str
    max_tokens: int
    workspace_root: Path


class _JobFailed(Exception):
    """The job cannot go on. The message becomes the job's error."""


class _StepWriter:
    """Appends steps for one job with rising `seq`, one transaction per step."""

    def __init__(self, engine: AsyncEngine, job: Job, steps: list[Step]) -> None:
        self._engine = engine
        self._job = job
        self.steps = steps

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        seq = self.steps[-1].seq + 1 if self.steps else 1
        async with self._engine.begin() as conn:
            step = await append_step(
                conn,
                job_id=self._job.id,
                seq=seq,
                kind=kind,
                payload=payload,
                lease_epoch=self._job.lease_epoch,
            )
            if tokens:
                # Same transaction as the step: the count and the log cannot disagree.
                await add_tokens_used(conn, self._job.id, tokens)
        self.steps.append(step)
        log.info("step", extra={"job_id": self._job.id, "seq": seq, "kind": kind.value})
        return step


async def _run_tool(tools: ToolRegistry, workspace: Path, call: dict[str, Any]) -> ToolResult:
    """Run one tool call. A failure of the tool becomes an error result for the model."""
    tool = tools.get(call["name"])
    if tool is None:
        return ToolResult(f"unknown tool: {call['name']}", is_error=True)
    try:
        result = await asyncio.to_thread(tool.run, workspace, call["input"])
    # SystemExit too: a tool that calls sys.exit() must not take the worker down.
    except (Exception, SystemExit) as error:
        log.exception("tool raised", extra={"tool": call["name"]})
        result = ToolResult(f"the tool failed: {error}", is_error=True)
    if not isinstance(result, ToolResult) or not isinstance(result.content, str):
        return ToolResult("the tool returned no text result", is_error=True)
    try:
        check_json(result.content)
    except InvalidPayload as error:
        return ToolResult(f"the tool's output cannot be stored: {error}", is_error=True)
    return result


def _response_problem(response: ModelResponse) -> str | None:
    """What is wrong with a response's shape, or None. Checked before any tool runs."""
    if not response.stop_reason:
        return "it has no stop_reason"
    content: Any = response.content
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        return "its content is not a list of blocks"
    ids = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        tool_use_id, name = block.get("id"), block.get("name")
        if not (isinstance(tool_use_id, str) and tool_use_id and isinstance(name, str)):
            return "a tool_use block has no id or no name"
        if not isinstance(block.get("input"), dict):
            return "a tool_use block's input is not an object"
        ids.append(tool_use_id)
    if len(set(ids)) != len(ids):
        return "two tool_use blocks have the same id"
    return None


async def _loop(
    writer: _StepWriter,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    spec: JobSpec,
    settings: AgentSettings,
    workspace: Path,
) -> None:
    """Run until the model ends its turn. Raises _JobFailed if the job cannot finish."""
    model = spec.model or settings.default_model
    while True:
        state = replay(spec.prompt, writer.steps)

        if state.needs_model_call:
            request = ModelRequest(
                model=model,
                max_tokens=settings.max_tokens,
                messages=state.messages,
                system=spec.system,
                tools=tools.definitions(),
                user_id=str(job.id),
            )
            await writer.append(
                StepKind.MODEL_REQUEST,
                {"model": model, "max_tokens": request.max_tokens, "messages": len(state.messages)},
            )
            try:
                response = await provider.complete(request)
            except ProviderError as error:
                raise _JobFailed(f"model call failed: {error}") from error
            payload: dict[str, Any] = {
                "id": response.id,
                "content": response.content,
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                },
            }
            problem = _response_problem(response)
            try:
                check_json(payload)
            except InvalidPayload as error:
                # The tokens were spent, so the step and the count are still
                # written. Only the content that cannot be stored is left out.
                problem = problem or f"it cannot be stored: {error}"
                payload["content"] = []
                payload["content_dropped"] = True
            await writer.append(
                StepKind.MODEL_RESPONSE,
                payload,
                tokens=response.input_tokens + response.output_tokens,
            )
            if problem:
                raise _JobFailed(f"the model's response is not usable: {problem}")
            continue

        # A response cut off at max_tokens can hold half a tool call, so the
        # stop reason is checked before any tool runs.
        if state.stop_reason == "tool_use" and state.pending:
            for call in state.pending:
                await writer.append(
                    StepKind.TOOL_INTENT,
                    {"tool_use_id": call["id"], "name": call["name"], "input": call["input"]},
                )
                result = await _run_tool(tools, workspace, call)
                await writer.append(
                    StepKind.TOOL_RESULT,
                    {
                        "tool_use_id": call["id"],
                        "content": result.content,
                        "is_error": result.is_error,
                    },
                )
            continue

        if state.stop_reason in _FINISHED:
            return
        raise _JobFailed(
            _FAILURES.get(state.stop_reason or "", f"unexpected stop_reason: {state.stop_reason}")
        )


async def run_job(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    settings: AgentSettings,
) -> JobStatus:
    """Run a job this worker has claimed and return its final status.

    A failure of the job (a bad spec, a provider error, a response that
    cannot be used) is recorded as an `error` step and the status `failed`.
    Only a failure to record the outcome reaches the caller, and the job
    then stays `running`. The workspace is removed either way.
    """
    async with engine.begin() as conn:
        writer = _StepWriter(engine, job, await read_steps(conn, job.id))
    failure: str | None = None
    try:
        spec = JobSpec.model_validate(job.spec)
        workspace = await asyncio.to_thread(
            prepare_workspace, settings.workspace_root, job.id, spec.files
        )
        await _loop(writer, provider, tools, job, spec, settings, workspace)
    except _JobFailed as error:
        failure = str(error)
    except Exception as error:
        log.exception("job crashed", extra={"job_id": job.id})
        failure = f"{type(error).__name__}: {error}"

    try:
        if failure is None:
            status = JobStatus.SUCCEEDED
        else:
            status = JobStatus.FAILED
            try:
                check_json(failure)
            except InvalidPayload:
                failure = "the job failed with an error that cannot be stored as text"
            # Read the log again first: if the failure was a step that did
            # not go in, the writer's idea of the next seq may be wrong.
            async with engine.begin() as conn:
                writer.steps = await read_steps(conn, job.id)
            await writer.append(StepKind.ERROR, {"message": failure})
        async with engine.begin() as conn:
            await update_status(conn, job.id, status, error=failure)
    finally:
        # Also when the outcome could not be recorded: this worker will not
        # touch the job again.
        await asyncio.to_thread(remove_workspace, settings.workspace_root, job.id)
    log.info("job finished", extra={"job_id": job.id, "status": status.value, "error": failure})
    return status
