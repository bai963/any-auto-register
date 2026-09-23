from services.register_tasks.batch_window import BatchCounters, initial_submit_count, should_submit_next


def test_initial_window_honors_total_requested_and_cap():
    assert initial_submit_count(0, 10) == 0
    assert initial_submit_count(2, 10) == 2
    assert initial_submit_count(10, 8, cap=3) == 3


def test_batch_counters_and_submission_guard():
    counters = BatchCounters()
    for outcome in ("success", "skipped", "stopped", "failed", "unknown"):
        counters.apply(outcome)

    assert (counters.success, counters.skipped, counters.stopped, counters.failed) == (1, 1, 1, 2)
    assert should_submit_next(next_index=1, total=2, stop_requested=False)
    assert not should_submit_next(next_index=2, total=2, stop_requested=False)
    assert not should_submit_next(next_index=1, total=2, stop_requested=True)
