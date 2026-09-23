"""Application log files with bounded 10 MiB shards.

Each process writes through a QueueHandler so 200 registration workers never
block on disk I/O. The listener rotates before a shard reaches the hard limit.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import threading
from pathlib import Path

MAX_LOG_FILE_BYTES = 10 * 1024 * 1024
TARGET_LOG_FILE_BYTES = 9 * 1024 * 1024
_QUEUE: queue.Queue | None = None
_LISTENER: logging.handlers.QueueListener | None = None
_LOCK = threading.Lock()


class ShardedFileHandler(logging.Handler):
    def __init__(self, directory: Path, prefix: str = "app"):
        super().__init__()
        self.directory, self.prefix = directory, prefix
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stream = None
        self._path: Path | None = None

    def _next_path(self) -> Path:
        indices = []
        for path in self.directory.glob(f"{self.prefix}-*.log"):
            try: indices.append(int(path.stem.rsplit("-", 1)[1]))
            except (ValueError, IndexError): pass
        return self.directory / f"{self.prefix}-{(max(indices, default=0) + 1):06d}.log"

    def _ensure_stream(self, size: int) -> None:
        if self._path is None:
            candidates = sorted(self.directory.glob(f"{self.prefix}-*.log"))
            self._path = candidates[-1] if candidates else self._next_path()
        current = self._path.stat().st_size if self._path.exists() else 0
        if current and current + size > TARGET_LOG_FILE_BYTES:
            if self._stream: self._stream.close()
            self._path, self._stream = self._next_path(), None
        if self._stream is None:
            self._stream = self._path.open("a", encoding="utf-8", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            encoded = line.encode("utf-8", errors="replace")
            # Prevent a single pathological record from making a shard exceed 10 MiB.
            if len(encoded) > TARGET_LOG_FILE_BYTES:
                line = line[:1_000_000] + " [TRUNCATED oversized log record]\n"
                encoded = line.encode("utf-8", errors="replace")
            with self._lock:
                self._ensure_stream(len(encoded))
                self._stream.write(line)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self._lock:
            if self._stream: self._stream.close()
            self._stream = None
        super().close()


def configure_file_logging() -> None:
    global _QUEUE, _LISTENER
    with _LOCK:
        if _LISTENER is not None:
            return
        root = Path(os.getenv("APP_RUNTIME_DIR", "data")) / "logs"
        file_handler = ShardedFileHandler(root)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        _QUEUE = queue.Queue(maxsize=20_000)
        _LISTENER = logging.handlers.QueueListener(_QUEUE, file_handler, respect_handler_level=True)
        _LISTENER.start()
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        # Do not remove Uvicorn's own console handlers; add exactly one queue handler.
        if not any(getattr(h, "_app_file_queue", False) for h in root_logger.handlers):
            handler = logging.handlers.QueueHandler(_QUEUE)
            handler._app_file_queue = True
            root_logger.addHandler(handler)


def shutdown_file_logging() -> None:
    global _LISTENER
    with _LOCK:
        if _LISTENER:
            _LISTENER.stop()
            _LISTENER = None
