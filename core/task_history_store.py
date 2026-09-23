"""Size-bounded file-backed task history.

No JSON history file may exceed 10 MiB. Task metadata and task output logs are
stored separately; logs are sharded before they reach 9 MiB. Writes are
atomic and protected by a process lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
MAX_FILE_BYTES = 10 * 1024 * 1024
TARGET_FILE_BYTES = 9 * 1024 * 1024


def _root() -> Path:
    root = Path(os.getenv("APP_RUNTIME_DIR", "data")) / "task_history"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _legacy_path() -> Path:
    return Path(os.getenv("APP_RUNTIME_DIR", "data")) / "task_history.json"


def _task_dir(task_id: str) -> Path:
    # Do not put user-controlled task identifiers into paths.
    path = _root() / "tasks" / hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path, fallback):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception:
        return fallback


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError(f"history shard exceeds {MAX_FILE_BYTES} bytes: {path.name}")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(encoded)
    temp.replace(path)


def _split_items(items: list[Any]) -> list[list[Any]]:
    chunks: list[list[Any]] = [[]]
    current = 2
    for item in items:
        raw = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # A single exceptional log line must still not violate the hard limit.
        if len(raw) + 2 > TARGET_FILE_BYTES:
            item = {"truncated": True, "message": str(item)[:1_000_000]}
            raw = json.dumps(item, ensure_ascii=False).encode("utf-8")
        addition = len(raw) + (1 if chunks[-1] else 0)
        if chunks[-1] and current + addition > TARGET_FILE_BYTES:
            chunks.append([])
            current = 2
        chunks[-1].append(item)
        current += addition
    return [chunk for chunk in chunks if chunk] or [[]]


def _write_shards(directory: Path, prefix: str, items: list[Any]) -> None:
    chunks = _split_items(items)
    expected: set[str] = set()
    for index, chunk in enumerate(chunks, 1):
        name = f"{prefix}-{index:06d}.json"
        expected.add(name)
        _write_json(directory / name, chunk)
    for path in directory.glob(f"{prefix}-*.json"):
        if path.name not in expected:
            path.unlink(missing_ok=True)


def _read_shards(directory: Path, prefix: str) -> list[Any]:
    items: list[Any] = []
    for path in sorted(directory.glob(f"{prefix}-*.json")):
        value = _read_json(path, [])
        if isinstance(value, list):
            items.extend(value)
    return items


def _migrate_legacy_once() -> None:
    legacy = _legacy_path()
    marker = legacy.with_name(legacy.name + ".migrated")
    if not legacy.exists() or marker.exists():
        return
    value = _read_json(legacy, {})
    if not isinstance(value, dict):
        return
    for snapshot in (value.get("tasks") or {}).values():
        if isinstance(snapshot, dict):
            _save_task(snapshot)
    logs = [item for item in (value.get("logs") or []) if isinstance(item, dict)]
    if logs:
        _write_shards(_root(), "account-logs", logs)
        _write_json(_root() / "account_logs_meta.json", {"next_log_id": int(value.get("next_log_id") or len(logs) + 1)})
    legacy.replace(marker)


def _save_task(snapshot: dict) -> None:
    task_id = str(snapshot.get("id") or "")
    if not task_id:
        return
    directory = _task_dir(task_id)
    logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
    meta = dict(snapshot)
    meta.pop("logs", None)
    meta["log_count"] = len(logs)
    _write_json(directory / "meta.json", meta)
    _write_shards(directory, "logs", logs)


def save_task(snapshot: dict) -> None:
    with _LOCK:
        _migrate_legacy_once()
        _save_task(snapshot)


def _load_task(task_id: str) -> dict | None:
    meta = _read_json(_task_dir(task_id) / "meta.json", None)
    if not isinstance(meta, dict) or str(meta.get("id") or "") != task_id:
        return None
    meta["logs"] = _read_shards(_task_dir(task_id), "logs")
    return meta


def get_task(task_id: str) -> dict | None:
    with _LOCK:
        _migrate_legacy_once()
        return _load_task(str(task_id))


def list_tasks() -> list[dict]:
    with _LOCK:
        _migrate_legacy_once()
        tasks: list[dict] = []
        for path in (_root() / "tasks").glob("*/meta.json") if (_root() / "tasks").exists() else []:
            meta = _read_json(path, None)
            if isinstance(meta, dict):
                # Listing does not need task console output; details/SSE hydrate it.
                meta["logs"] = []
                tasks.append(meta)
        return tasks


def delete_task(task_id: str) -> bool:
    with _LOCK:
        directory = _task_dir(str(task_id))
        meta = directory / "meta.json"
        if not meta.exists():
            return False
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()
        return True


def append_log(*, platform: str, email: str, status: str, error: str = "", detail: dict | None = None) -> dict:
    with _LOCK:
        _migrate_legacy_once()
        root = _root()
        meta_path = root / "account_logs_meta.json"
        meta = _read_json(meta_path, {"next_log_id": 1})
        next_id = int(meta.get("next_log_id") or 1)
        item = {"id": next_id, "platform": platform, "email": email, "status": status,
                "error": error, "detail_json": json.dumps(detail or {}, ensure_ascii=False)}
        logs = _read_shards(root, "account-logs")
        logs.append(item)
        _write_shards(root, "account-logs", logs)
        _write_json(meta_path, {"next_log_id": next_id + 1})
        return item


def list_logs(platform: str | None, page: int, page_size: int) -> tuple[int, list[dict]]:
    with _LOCK:
        _migrate_legacy_once()
        logs = [x for x in _read_shards(_root(), "account-logs") if isinstance(x, dict)]
    if platform:
        logs = [x for x in logs if x.get("platform") == platform]
    logs.sort(key=lambda x: int(x.get("id") or 0), reverse=True)
    return len(logs), logs[max(0, (page - 1) * page_size): max(0, page * page_size)]


def delete_logs(ids: list[int]) -> tuple[int, list[int]]:
    wanted = set(ids)
    with _LOCK:
        _migrate_legacy_once()
        root = _root()
        logs = [x for x in _read_shards(root, "account-logs") if isinstance(x, dict)]
        found = {int(x.get("id")) for x in logs if x.get("id") in wanted}
        if found:
            _write_shards(root, "account-logs", [x for x in logs if x.get("id") not in wanted])
    return len(found), [x for x in ids if x not in found]
