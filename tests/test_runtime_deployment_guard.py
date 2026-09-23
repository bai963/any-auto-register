"""Deployment guards for process-local task runtime state."""

from __future__ import annotations

import pytest

from main import _validate_web_concurrency


def test_single_web_worker_is_supported():
    _validate_web_concurrency("1")


@pytest.mark.parametrize("value", ["0", "2", "200", "-1"])
def test_multiple_or_invalid_worker_counts_are_rejected(value: str):
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY=1"):
        _validate_web_concurrency(value)


def test_invalid_worker_count_is_rejected():
    with pytest.raises(RuntimeError, match="正整数"):
        _validate_web_concurrency("not-a-number")
