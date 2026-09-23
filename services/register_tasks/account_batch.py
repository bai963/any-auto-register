"""Dependency-light sliding-window executor for account batch operations.

The API layer retains task-store, logging, limiter and proxy adapters.  This
module owns only Future-window mechanics, making stop/submit behavior testable
without importing FastAPI or persistent task state.
"""
from __future__ import annotations

from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, TypeVar

from .batch_window import initial_submit_count, should_submit_next

T = TypeVar("T")
R = TypeVar("R")


def run_account_batch_window(
    items: Iterable[T],
    *,
    concurrency: int,
    is_stop_requested: Callable[[], bool],
    handle: Callable[[int, T], R],
    on_result: Callable[[R | None, BaseException | None], None],
) -> bool:
    """Run a bounded future window and return whether execution was stopped.

    Cancellation matches the legacy loop: stop prevents new submissions and
    cancels only not-yet-running futures; completed result accounting remains
    the caller's responsibility through ``on_result``.
    """
    values = list(items)
    total = len(values)
    if not total:
        return bool(is_stop_requested())
    max_workers = max(1, initial_submit_count(total, concurrency))
    stopped = False
    next_index = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = set()

        def submit_next() -> bool:
            nonlocal next_index
            if not should_submit_next(next_index=next_index, total=total, stop_requested=is_stop_requested()):
                return False
            pending.add(pool.submit(handle, next_index, values[next_index]))
            next_index += 1
            return True

        while len(pending) < max_workers and submit_next():
            pass
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    on_result(future.result(), None)
                except CancelledError:
                    on_result(None, None)
                except BaseException as exc:
                    on_result(None, exc)
            if is_stop_requested():
                stopped = True
                for future in pending:
                    future.cancel()
                continue
            while len(pending) < max_workers and submit_next():
                pass
    return stopped or bool(is_stop_requested())
