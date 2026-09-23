"""Pure sliding-window accounting used by long-running account batches."""

from __future__ import annotations

from dataclasses import dataclass


def initial_submit_count(total: int, requested_concurrency: int, cap: int | None = None) -> int:
    effective = max(1, int(requested_concurrency or 1))
    if cap is not None:
        effective = min(effective, max(1, int(cap)))
    return min(max(0, int(total or 0)), effective)


def should_submit_next(*, next_index: int, total: int, stop_requested: bool) -> bool:
    return not stop_requested and next_index < total


@dataclass(slots=True)
class BatchCounters:
    success: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: int = 0

    def apply(self, outcome: object) -> None:
        normalized = str(getattr(outcome, "value", outcome) or "").lower()
        if normalized == "success":
            self.success += 1
        elif normalized == "skipped":
            self.skipped += 1
        elif normalized == "stopped":
            self.stopped += 1
        else:
            self.failed += 1
