"""Bounded, ordered tool execution with conservative side-effect barriers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class ScheduledCall:
    call_id: str
    run: Callable[[], Awaitable[Any]]
    read_only: bool = False


async def run_ordered_batch(calls: Iterable[ScheduledCall], *,
                            max_parallel_calls: int = 1) -> list[Any]:
    """Return results in model order, while overlapping only safe reads.

    Unknown, control, write and delegation calls are exclusive barriers. Each
    read window is bounded, and all started calls settle before an exception is
    propagated. A failure prevents dispatch of later windows/barriers. Callers
    persist their own start/result records inside each closure, so outcomes are
    retained even when another call in the window raises an approval or error.
    """
    limit = max(1, int(max_parallel_calls))
    pending = list(calls)
    results: list[Any] = []
    index = 0

    async def invoke(item: ScheduledCall) -> Any:
        # Invoke inside the task: a callable that raises synchronously or
        # returns a Future must follow the same settlement rules as async def.
        return await item.run()

    while index < len(pending):
        call = pending[index]
        if not call.read_only or limit == 1:
            results.append(await call.run())
            index += 1
            continue
        end = index
        while end < len(pending) and pending[end].read_only and end - index < limit:
            end += 1
        tasks = [asyncio.create_task(invoke(item), name=f"tool:{item.call_id}")
                 for item in pending[index:end]]
        try:
            settled = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            # gather cancels children when the parent is cancelled. Reap them
            # before leaving, so no executor silently outlives this batch.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for outcome in settled:
            if isinstance(outcome, BaseException):
                raise outcome
        results.extend(settled)
        index = end
    return results
