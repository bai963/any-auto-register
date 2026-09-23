import logging
from pathlib import Path

from core.file_logging import ShardedFileHandler


def test_log_handler_shards_before_ten_megabytes():
    # Controlled Windows runner denies pytest's OS temp fixture.
    root = Path("data") / ".file-log-test"
    root.mkdir(parents=True, exist_ok=True)
    handler = ShardedFileHandler(root, "app")
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Keep test small while exercising the same boundary logic.
    import core.file_logging as mod
    original = mod.TARGET_LOG_FILE_BYTES
    mod.TARGET_LOG_FILE_BYTES = 100
    try:
        logger = logging.getLogger("test.file.shards")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.info("a" * 70)
        logger.info("b" * 70)
    finally:
        mod.TARGET_LOG_FILE_BYTES = original
        handler.close()
    files = sorted(root.glob("app-*.log"))
    assert len(files) == 2
    assert all(path.stat().st_size <= 100 for path in files)
    for path in root.glob("*"):
        path.unlink()
    root.rmdir()
