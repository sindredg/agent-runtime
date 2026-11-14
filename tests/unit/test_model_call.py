"""The model call as the worker makes it: raced against a stop and the loss of the lease."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

import pytest

from runtime.provider.base import ModelRequest, ModelResponse, ProviderError
from runtime.store.held import LeaseLost
from runtime.worker.agent import _complete_unless_stopped
from runtime.worker.lease import Lease

REQUEST = ModelRequest(model="m", max_tokens=10, messages=[{"role": "user", "content": "go"}])
ANSWER = ModelResponse("msg_1", "m", [{"type": "text", "text": "hi"}], "end_turn", 1, 1)


class Model:
    """A provider that answers after `seconds`, or never. It records how its call ended."""

    def __init__(self, seconds: float | None = None, error: Exception | None = None) -> None:
        self.seconds = seconds
        self.error = error
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.sleep(3600 if self.seconds is None else self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return ANSWER

    async def aclose(self) -> None:
        return None


def new_lease() -> Lease:
    return Lease(uuid4(), 1, timedelta(seconds=30), claimed_at=time.monotonic())


def run[T](body: Callable[[], Awaitable[T]]) -> T:
    """Run `body` and check that it left no task behind: every racer was awaited."""

    async def checked() -> T:
        try:
            return await body()
        finally:
            left = asyncio.all_tasks() - {asyncio.current_task()}
            assert not left, f"tasks left behind: {left}"

    return asyncio.run(checked())


def test_an_answer_is_returned_when_nothing_gets_in_the_way() -> None:
    async def body() -> ModelResponse | None:
        return await _complete_unless_stopped(Model(0.01), REQUEST, asyncio.Event(), new_lease())

    assert run(body) is ANSWER


def test_a_stop_cancels_the_call_and_returns_nothing() -> None:
    async def body() -> tuple[ModelResponse | None, bool]:
        model, stop = Model(), asyncio.Event()
        call = asyncio.ensure_future(_complete_unless_stopped(model, REQUEST, stop, new_lease()))
        await model.started.wait()
        stop.set()
        return await call, model.cancelled

    assert run(body) == (None, True)


def test_a_stop_that_is_already_set_means_the_call_is_not_made() -> None:
    async def body() -> tuple[ModelResponse | None, bool]:
        model, stop = Model(), asyncio.Event()
        stop.set()
        return await _complete_unless_stopped(model, REQUEST, stop, new_lease()), (
            model.started.is_set()
        )

    assert run(body) == (None, False)


@pytest.mark.parametrize("stopped_too", [False, True], ids=["lost", "lost-and-stopped"])
def test_a_lost_lease_cancels_the_call_and_drops_the_job(stopped_too: bool) -> None:
    """Also when the worker is told to stop in the same moment: a lost job is not released."""

    async def body() -> bool:
        model, stop, lease = Model(), asyncio.Event(), new_lease()
        call = asyncio.ensure_future(_complete_unless_stopped(model, REQUEST, stop, lease))
        await model.started.wait()
        if stopped_too:
            stop.set()
        lease.mark_lost()
        with pytest.raises(LeaseLost) as lost:
            await call
        assert (lost.value.job_id, lost.value.lease_epoch) == (lease.job_id, 1)
        return model.cancelled

    assert run(body) is True


def test_a_provider_error_passes_through() -> None:
    async def body() -> None:
        failing = Model(0.01, ProviderError("overloaded", status=529, retryable=True))
        await _complete_unless_stopped(failing, REQUEST, asyncio.Event(), new_lease())

    with pytest.raises(ProviderError, match="overloaded"):
        run(body)


def test_a_watcher_that_fails_is_an_error_and_not_a_stop() -> None:
    """Read as a stop, the request would be sent again, and again, without an end."""

    class Broken(Lease):
        async def wait_lost(self) -> None:
            raise RuntimeError("the watcher broke")

    async def body() -> None:
        lease = Broken(uuid4(), 1, timedelta(seconds=30), claimed_at=time.monotonic())
        await _complete_unless_stopped(Model(), REQUEST, asyncio.Event(), lease)

    with pytest.raises(RuntimeError, match="the watcher broke"):
        run(body)
