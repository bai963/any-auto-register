from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from typing import Callable, Optional
from copy import deepcopy
from datetime import datetime, timezone
from core.db import DATABASE_REGISTER_CONCURRENCY_CAP, TaskLog, TaskRunModel, engine
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    NonRetryableRegisterError,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
    global_task_concurrency_limiter,
    global_mail_limiter,
    global_openai_limiter,
    global_sms_limiter,
)
import time, json, asyncio, threading, logging, multiprocessing, queue, os

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250
# LuckMail 的项目编码与本项目平台标识不完全一致，需要显式映射。
LUCKMAIL_PROJECT_CODES = {"chatgpt": "openai"}
_task_store = RegisterTaskStore(
    max_finished_tasks=MAX_FINISHED_TASKS,
    cleanup_threshold=CLEANUP_THRESHOLD,
)


MAX_REGISTER_RETRY_TIMES = 10
MAX_TASK_CONCURRENCY = 200
# SQLite 使用 WAL 与 busy timeout 支撑本地中等并发；更高并发仍建议 PostgreSQL。
SQLITE_MAX_TASK_CONCURRENCY = 40
DEFAULT_REGISTER_RETRY_TIMES = 1
# 「重开也是同样结局」的失败最多连着出现几轮就收手。
#
# 手机注册里这类失败（账号已建好、接码平台一条短信都没收到）以前是一票否决：
# 第一轮撞上就把用户填的重试轮数整个作废，看上去就是「我写了没反应」。可一轮
# 只用了一个号，凭它断定整个号源都被静默拦码证据太薄 —— 再开一轮确认一下，
# 连着两轮同样结局才是真的号源问题，那时候继续只会多几个孤号。
MAX_DEAD_END_ROUNDS = 2
# 上次事故在最后日志后 11 分钟仍显示 40 个 active attempts；这些阈值让 UI 和
# 运维日志在等待短信退款 deadline 之前就可见地告警，而非静默到人工发现。
TASK_STALL_WARNING_SECONDS = 60
TASK_STALL_DIAGNOSIS_SECONDS = 120
# 子进程是线程无法强杀时的最终回收边界；手机号+绑定邮箱允许覆盖短信 240 秒和
# 邮箱绑定余量，普通流程也不允许无限占住 40/200 worker 槽位。
REGISTER_ATTEMPT_TIMEOUT_SECONDS = 300
PHONE_REGISTER_ATTEMPT_TIMEOUT_SECONDS = 360


def _should_run_register_in_child_process() -> bool:
    """生产请求隔离不可信协议 I/O；pytest 内需保留同一解释器的 mock/计数器。

    spawn 会重新 import registry，单元测试对 ``core.registry.get`` 的 patch 与
    ``_FlakyPlatform.attempts`` 均不会跨进程继承，导致测试实际跑到真实 ChatGPT
    流程。正式服务不设置 PYTEST_CURRENT_TEST，仍始终使用可硬回收的子进程。
    """
    return not bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _register_platform_child(payload: dict, result_queue) -> None:
    """单账号平台注册子进程入口。只能使用可 pickle 的 payload。"""
    try:
        from core.base_mailbox import create_mailbox
        from core.base_platform import RegisterConfig
        # spawn 子进程不会继承主进程 lifespan 中的插件注册表，必须自行加载。
        from core.registry import get, load_all

        load_all()

        config = RegisterConfig(
            executor_type=payload["executor_type"],
            captcha_solver=payload["captcha_solver"],
            proxy=payload.get("proxy"),
            extra=payload.get("extra") or {},
        )
        mailbox = create_mailbox(
            provider=(payload.get("extra") or {}).get("mail_provider", "luckmail"),
            extra=payload.get("extra") or {}, proxy=payload.get("proxy"),
        )
        platform = get(payload["platform"])(config=config, mailbox=mailbox)
        platform._log_fn = lambda msg: result_queue.put(("log", str(msg)))
        if getattr(platform, "mailbox", None) is not None:
            platform.mailbox._log_fn = platform._log_fn
        account = platform.register(email=payload.get("email") or None, password=payload.get("password"))
        result_queue.put(("result", account))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _attempt_timeout_seconds(platform: str, extra: dict) -> Optional[int]:
    if platform == "chatgpt":
        flow = str((extra or {}).get("chatgpt_register_flow") or "").strip().lower()
        if flow == "phone_with_email":
            # 号码未验证前会按四分钟窗口持续退号换号，不能由 attempt deadline 杀掉。
            # stop/skip、子进程崩溃和接码 journal 清理仍然有效。
            return None
        if flow == "phone" or uses_sms_register_flow(extra):
            return PHONE_REGISTER_ATTEMPT_TIMEOUT_SECONDS
    return REGISTER_ATTEMPT_TIMEOUT_SECONDS


def uses_sms_register_flow(extra: dict) -> bool:
    """唯一的手机注册判定：phone_with_email 也必须占用接码预算。"""
    extra = extra or {}
    method = str(extra.get("register_method") or "").strip().lower()
    flow = str(extra.get("chatgpt_register_flow") or "").strip().lower()
    return method in {"phone", "sms"} or flow in {"phone", "phone_with_email"}


def normalize_register_retry_times(value) -> int:
    """空值按默认算，越界夹回去 —— 这个数字来自表单和配置项，什么都可能填。"""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_RETRY_TIMES
    return max(0, min(parsed, MAX_REGISTER_RETRY_TIMES))


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = Field(default=1, ge=1)
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    # 整条注册流程失败后再开几轮（每轮都是全新的邮箱/号码/会话）。
    # 0 = 不重试；一次网络抖动、一个二手号就判 FAIL 太浪费。
    register_retry_times: int = DEFAULT_REGISTER_RETRY_TIMES
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


class BackfillRtTaskRequest(BaseModel):
    """批量补 RT 的任务参数。

    默认串行 + 每个号之间隔几秒：补 RT 会对同一批号连续打 OpenAI 的授权链，
    并发拉满等于主动送风控素材，宁可慢点。
    """

    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    only_missing_rt: bool = True
    allow_login: bool = True
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    delay_seconds: float = Field(default=5, ge=0)
    # 补 RT 的协议链若遇到 add-phone，单个账号最多可新租的号码数；0 = 不租号。
    sms_max_phone_attempts: int = Field(default=3, ge=0, le=20)
    proxy: Optional[str] = None


class Bind2faTaskRequest(BaseModel):
    """批量绑 2FA 的任务参数。

    和补 RT 一样默认串行 + 间隔几秒：绑定链要连着打 OpenAI 的登录/enroll 接口，
    并发拉满只会更快撞上风控。
    """

    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    only_missing_2fa: bool = True
    allow_login: bool = True
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    delay_seconds: float = Field(default=5, ge=0)
    proxy: Optional[str] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value, fallback):
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(fallback, ensure_ascii=False)


def _json_loads(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _to_epoch_seconds(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _to_datetime(value) -> datetime:
    try:
        ts = float(value or 0)
        if ts > 1_000_000_000_000:
            ts /= 1000
        if ts <= 0:
            return _utcnow()
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return _utcnow()


def _normalize_snapshot(snapshot: dict) -> dict:
    return {
        "id": str(snapshot.get("id") or ""),
        "status": str(snapshot.get("status") or "pending"),
        "platform": str(snapshot.get("platform") or ""),
        "source": str(snapshot.get("source") or "manual"),
        "meta": snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {},
        "total": int(snapshot.get("total") or 0),
        "progress": str(snapshot.get("progress") or "0/0"),
        "logs": snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else [],
        "success": int(snapshot.get("success") or 0),
        "registered": int(snapshot.get("registered") or 0),
        "skipped": int(snapshot.get("skipped") or 0),
        "errors": snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else [],
        "control": snapshot.get("control") if isinstance(snapshot.get("control"), dict) else {},
        "cashier_urls": snapshot.get("cashier_urls") if isinstance(snapshot.get("cashier_urls"), list) else [],
        "error": str(snapshot.get("error") or ""),
        "created_at": _to_epoch_seconds(snapshot.get("created_at")),
        "updated_at": _to_epoch_seconds(snapshot.get("updated_at")),
    }


def _task_run_to_snapshot(row: TaskRunModel) -> dict:
    return _normalize_snapshot(
        {
            "id": row.id,
            "status": row.status,
            "platform": row.platform,
            "source": row.source,
            "meta": _json_loads(row.meta_json, {}),
            "total": row.total,
            "progress": row.progress,
            "logs": _json_loads(row.logs_json, []),
            "success": row.success,
            "registered": row.registered,
            "skipped": row.skipped,
            "errors": _json_loads(row.errors_json, []),
            "control": _json_loads(row.control_json, {}),
            "cashier_urls": _json_loads(row.cashier_urls_json, []),
            "error": row.error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _upsert_task_run(snapshot: dict) -> None:
    normalized = _normalize_snapshot(snapshot)
    if not normalized["id"]:
        return
    with Session(engine) as s:
        row = s.get(TaskRunModel, normalized["id"])
        if row is None:
            row = TaskRunModel(
                id=normalized["id"],
                platform=normalized["platform"],
                source=normalized["source"],
                status=normalized["status"],
                total=normalized["total"],
                progress=normalized["progress"],
                success=normalized["success"],
                registered=normalized["registered"],
                skipped=normalized["skipped"],
                error=normalized["error"],
                meta_json=_json_dumps(normalized["meta"], {}),
                logs_json=_json_dumps(normalized["logs"], []),
                errors_json=_json_dumps(normalized["errors"], []),
                cashier_urls_json=_json_dumps(normalized["cashier_urls"], []),
                control_json=_json_dumps(normalized["control"], {}),
                created_at=_to_datetime(normalized["created_at"]),
                updated_at=_to_datetime(normalized["updated_at"]),
            )
            s.add(row)
        else:
            row.platform = normalized["platform"]
            row.source = normalized["source"]
            row.status = normalized["status"]
            row.total = normalized["total"]
            row.progress = normalized["progress"]
            row.success = normalized["success"]
            row.registered = normalized["registered"]
            row.skipped = normalized["skipped"]
            row.error = normalized["error"]
            row.meta_json = _json_dumps(normalized["meta"], {})
            row.logs_json = _json_dumps(normalized["logs"], [])
            row.errors_json = _json_dumps(normalized["errors"], [])
            row.cashier_urls_json = _json_dumps(normalized["cashier_urls"], [])
            row.control_json = _json_dumps(normalized["control"], {})
            if row.created_at is None:
                row.created_at = _to_datetime(normalized["created_at"])
            row.updated_at = _to_datetime(normalized["updated_at"])
            s.add(row)
        s.commit()


# 日志、进度会被高并发 worker 频繁更新；合并为最多每秒一次的快照写入，
# 任务结束或读取详情时可用 force=True 强制落库。
_TASK_SNAPSHOT_FLUSH_INTERVAL_SECONDS = 1.0
_snapshot_flush_lock = threading.Lock()
_snapshot_last_flush_at: dict[str, float] = {}


def _persist_task_snapshot(task_id: str, *, force: bool = False) -> None:
    if not _task_store.exists(task_id):
        return
    now = time.monotonic()
    with _snapshot_flush_lock:
        last_flush_at = _snapshot_last_flush_at.get(task_id, 0.0)
        if not force and now - last_flush_at < _TASK_SNAPSHOT_FLUSH_INTERVAL_SECONDS:
            return
        # 先占位，避免 200 个 worker 同时穿透节流并重复写同一条 task_runs。
        _snapshot_last_flush_at[task_id] = now
    try:
        snapshot = _task_store.snapshot(task_id)
        _upsert_task_run(snapshot)
    except Exception:
        # 允许下一次调用尽快重试，不把短暂数据库故障缓存一秒。
        with _snapshot_flush_lock:
            _snapshot_last_flush_at.pop(task_id, None)


def _get_persisted_task(task_id: str) -> Optional[dict]:
    with Session(engine) as s:
        row = s.get(TaskRunModel, task_id)
        if row is None:
            return None
        return _task_run_to_snapshot(row)


def _list_persisted_tasks() -> list[dict]:
    with Session(engine) as s:
        rows = s.exec(select(TaskRunModel)).all()
    snapshots = [_task_run_to_snapshot(row) for row in rows]
    snapshots.sort(
        key=lambda item: (
            {"running": 0, "pending": 1, "done": 2, "failed": 3, "stopped": 4}.get(
                str(item.get("status") or ""),
                9,
            ),
            -_to_epoch_seconds(item.get("created_at")),
        )
    )
    return snapshots


def _finalize_orphan_tasks() -> None:
    with Session(engine) as s:
        rows = s.exec(
            select(TaskRunModel).where(TaskRunModel.status.in_(["pending", "running"]))
        ).all()
        if not rows:
            return
        changed = False
        for row in rows:
            if _task_store.exists(row.id):
                continue
            row.status = "stopped"
            row.error = row.error or "任务因服务重启中断"
            logs = _json_loads(row.logs_json, [])
            tip = "[SYSTEM] 任务因服务重启中断，已自动标记为已停止"
            if tip not in logs:
                ts = datetime.now().strftime("%H:%M:%S")
                logs.append(f"[{ts}] {tip}")
            row.logs_json = _json_dumps(logs, [])
            row.updated_at = _utcnow()
            s.add(row)
            changed = True
        if changed:
            s.commit()


def _ensure_task_exists(task_id: str) -> None:
    if _task_store.exists(task_id):
        return
    if _get_persisted_task(task_id) is None:
        raise HTTPException(404, "任务不存在")


def _ensure_task_mutable(task_id: str) -> None:
    _ensure_task_exists(task_id)
    if _task_store.exists(task_id):
        snapshot = _task_store.snapshot(task_id)
    else:
        snapshot = _get_persisted_task(task_id) or {}
    if snapshot.get("status") in {"done", "failed", "stopped"}:
        raise HTTPException(409, "任务已结束，无法再执行控制操作")


def _get_task_snapshot(task_id: str) -> dict:
    _ensure_task_exists(task_id)
    if _task_store.exists(task_id):
        _persist_task_snapshot(task_id, force=True)
    snapshot = _get_persisted_task(task_id)
    if snapshot is None and _task_store.exists(task_id):
        snapshot = _normalize_snapshot(_task_store.snapshot(task_id))
    if snapshot is None:
        raise HTTPException(404, "任务不存在")
    return snapshot


def _validate_task_concurrency(concurrency: int) -> None:
    """SQLite 支持本地中等并发；更高并发模式要求 PostgreSQL。"""
    if engine.url.get_backend_name() == "sqlite" and concurrency > SQLITE_MAX_TASK_CONCURRENCY:
        raise HTTPException(
            400,
            f"SQLite 模式最多允许 {SQLITE_MAX_TASK_CONCURRENCY} 并发；"
            "需要更高并发（最高 200）请配置 PostgreSQL DATABASE_URL。",
        )


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store
    from core.registry import is_platform_enabled

    _validate_task_concurrency(req.concurrency)
    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)
    prepared.platform = str(prepared.platform or "").strip().lower()

    if not is_platform_enabled(prepared.platform):
        raise HTTPException(400, f"{prepared.platform} 平台已下线，不再支持注册")

    mail_provider = prepared.extra.get("mail_provider") or config_store.get(
        "mail_provider", ""
    )
    if mail_provider == "luckmail":
        prepared.extra["luckmail_project_code"] = LUCKMAIL_PROJECT_CODES.get(
            prepared.platform, prepared.platform
        )

    return prepared


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    _task_store.create(
        task_id,
        platform=req.platform,
        total=req.count,
        source=source,
        meta=meta,
    )
    _persist_task_snapshot(task_id)


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    prepared = _prepare_register_request(req)
    task_id = f"task_{int(time.time() * 1000)}"
    _create_task_record(task_id, prepared, source, meta)
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_register, args=(task_id, prepared), daemon=True
        )
        thread.start()
    else:
        background_tasks.add_task(_run_register, task_id, prepared)
    return task_id


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    return _task_store.has_active(platform=platform, source=source)


def _log(task_id: str, msg: str):
    """向任务追加一条日志"""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _task_store.append_log(task_id, entry)
    _persist_task_snapshot(task_id)
    print(entry)


def _save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict = None
):
    """Write a TaskLog record to the database (fire-and-forget, non-blocking)."""
    def _write():
        with Session(engine) as s:
            log = TaskLog(
                platform=platform,
                email=email,
                status=status,
                error=error,
                detail_json=json.dumps(detail or {}, ensure_ascii=False),
            )
            s.add(log)
            s.commit()
    threading.Thread(target=_write, daemon=True).start()


def _auto_upload_integrations(task_id: str, account):
    """注册成功后自动导入外部系统（后台线程，不阻塞注册流程）。"""
    def _run():
        try:
            from services.external_sync import sync_account

            for result in sync_account(account):
                name = result.get("name", "Auto Upload")
                ok = bool(result.get("ok"))
                msg = result.get("msg", "")
                _log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
        except Exception as e:
            _log(task_id, f"  [Auto Upload] 自动导入异常: {e}")
    threading.Thread(target=_run, daemon=True).start()


def _run_register(task_id: str, req: RegisterTaskRequest):
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.base_mailbox import create_mailbox
    from core.proxy_utils import normalize_proxy_url

    control = _task_store.control_for(task_id)
    _task_store.mark_running(task_id)
    _persist_task_snapshot(task_id)
    success = 0
    skipped = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()

    def _sleep_with_control(
        wait_seconds: float,
        *,
        attempt_id: int | None = None,
    ) -> None:
        remaining = max(float(wait_seconds or 0), 0.0)
        while remaining > 0:
            control.checkpoint(attempt_id=attempt_id)
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _heartbeat(attempt_id: int | None, stage: str) -> None:
        control.heartbeat(attempt_id, stage)

    try:
        PlatformCls = get(req.platform)

        # 预先计算 merged_extra，所有线程共享只读副本，避免每线程重复调用 config_store
        from core.config_store import config_store as _cs
        _base_extra = _cs.get_all().copy()
        _base_extra.update(
            {k: v for k, v in req.extra.items() if v is not None and v != ""}
        )

        # 每个 worker 领取代理租约，默认一个代理同时只服务一个任务。
        # 代理不足会等待空闲代理，而不是让多个账号意外复用同一出口。
        from core.proxy_pool import proxy_pool as _proxy_pool
        _log(
            task_id,
            f"外部服务预算: OpenAI {global_openai_limiter.limit}，邮箱 {global_mail_limiter.limit}，接码 {global_sms_limiter.limit}，任务总并发 {global_task_concurrency_limiter.limit}",
        )
        if req.proxy:
            _log(task_id, "资源预检: 使用固定代理；默认同一代理同时只运行 1 个账号")
        else:
            available_proxies = _proxy_pool.active_count()
            if available_proxies == 0:
                _log(task_id, "资源预检: 未配置可用代理，将使用直连；高并发可能触发风控或限流")
            elif available_proxies < min(req.concurrency, req.count):
                _log(task_id, f"资源预检: 可用代理 {available_proxies} 个，低于目标并发 {min(req.concurrency, req.count)}；任务会等待代理租约")
            else:
                _log(task_id, f"资源预检: 可用代理 {available_proxies} 个，满足目标并发")

        def _get_proxy() -> Optional[str]:
            return _proxy_pool.acquire(
                preferred_url=req.proxy,
                is_stop_requested=control.is_stop_requested,
            )

        def _build_mailbox(proxy: Optional[str]):
            return create_mailbox(
                provider=_base_extra.get("mail_provider", "luckmail"),
                extra=_base_extra,
                proxy=proxy,
            )

        retry_times = normalize_register_retry_times(req.register_retry_times)
        total_rounds = 1 + retry_times
        if total_rounds > 1:
            _log(
                task_id,
                f"失败重试轮数 {retry_times}：每个账号失败后最多重开 {retry_times} 轮，"
                f"连同首轮共 {total_rounds} 轮（每轮都是全新的代理/邮箱/号码/会话）",
            )

        def _is_dead_end(result: AttemptResult) -> bool:
            return result.outcome == AttemptOutcome.FAILED and not result.retryable

        def _do_one(i: int):
            """一个序号的完整交付：失败就整流程重开一轮，直到轮次用尽。

            重开的是整条链（新代理、新邮箱/号码、新会话），不是接码层的换号 ——
            半路建出来的号已经被占了，拿它死磕只会一直撞同一堵墙。
            """
            result = _do_one_round(i, 1, total_rounds)
            dead_end_rounds = 1 if _is_dead_end(result) else 0
            for round_no in range(2, total_rounds + 1):
                if result.outcome != AttemptOutcome.FAILED:
                    break
                if control.is_stop_requested():
                    break
                if dead_end_rounds >= MAX_DEAD_END_ROUNDS:
                    _log(
                        task_id,
                        f"[RETRY] 第 {i + 1} 个账号连续 {dead_end_rounds} 轮都栽在"
                        f"「重开也是同样结局」的失败上，剩下 "
                        f"{total_rounds - round_no + 1} 轮不再重开"
                        f"（再开只会多几个没人认领的号）: {result.message}",
                    )
                    break
                _log(
                    task_id,
                    f"[RETRY] 第 {i + 1} 个账号第 {round_no - 1}/{total_rounds} 轮失败，"
                    f"开始第 {round_no}/{total_rounds} 轮重试（全新会话）: {result.message}",
                )
                result = _do_one_round(i, round_no, total_rounds)
                dead_end_rounds = dead_end_rounds + 1 if _is_dead_end(result) else 0
            if result.outcome == AttemptOutcome.FAILED:
                # 注册记录按"一个序号一条结果"记，所以只有跑完所有轮次才落一条
                # failed；否则重试成功了还会在统计里留下一条失败
                _save_task_log(
                    req.platform,
                    result.email,
                    "failed",
                    error=result.message,
                )
            return result

        def _do_one_round(i: int, round_no: int, rounds: int):
            nonlocal next_start_time
            _proxy = None
            child = None
            child_queue = None
            current_email = req.email or ""
            attempt_id: int | None = None
            concurrency_slot_acquired = False
            mail_slot_acquired = False
            openai_slot_acquired = False
            sms_slot_acquired = False
            round_suffix = f"（第 {round_no}/{rounds} 轮）" if rounds > 1 else ""
            try:
                control.checkpoint()
                concurrency_slot_acquired = global_task_concurrency_limiter.acquire(
                    control.is_stop_requested
                )
                # 注册会持有邮箱资源；ChatGPT 还会占用 OpenAI 授权链预算。
                mail_slot_acquired = global_mail_limiter.acquire(control.is_stop_requested)
                if req.platform == "chatgpt":
                    openai_slot_acquired = global_openai_limiter.acquire(control.is_stop_requested)
                # 手机注册的接码 API 也需要独立预算；非手机流程不占用。
                if uses_sms_register_flow(_base_extra):
                    sms_slot_acquired = global_sms_limiter.acquire(control.is_stop_requested)
                control.checkpoint()
                attempt_id = control.start_attempt()
                control.checkpoint(attempt_id=attempt_id)
                _heartbeat(attempt_id, "acquiring_proxy")
                _proxy = normalize_proxy_url(_get_proxy())
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _log(
                                task_id,
                                f"第 {i + 1} 个账号启动前延迟 {wait_seconds:g} 秒",
                            )
                            _sleep_with_control(
                                wait_seconds,
                                attempt_id=attempt_id,
                            )
                        next_start_time = time.time() + req.register_delay_seconds
                control.checkpoint(attempt_id=attempt_id)

                merged_extra = _base_extra

                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                _task_store.set_progress(task_id, f"{i + 1}/{req.count}")
                _persist_task_snapshot(task_id)
                _heartbeat(attempt_id, "registering")
                _log(task_id, f"开始注册第 {i + 1}/{req.count} 个账号{round_suffix}")
                if _proxy:
                    _log(task_id, f"使用代理: {_proxy}")

                # Windows/线程模式下第三方 DNS、IMAP 或供应商库可永久卡住；注册主体
                # 放进独立 spawn 子进程，父线程能在 deadline 后 terminate 并回收槽位。
                _heartbeat(attempt_id, "platform_register")
                def _run_in_current_process(reason: str = "") -> None:
                    nonlocal child, account, _mailbox
                    if reason:
                        _log(task_id, f"[SYSTEM] {reason}，降级线程执行")
                    _heartbeat(attempt_id, "fallback_thread_register")
                    _mailbox = _build_mailbox(_proxy)
                    _platform = PlatformCls(config=_config, mailbox=_mailbox)
                    _platform._task_attempt_token = attempt_id
                    _platform._log_fn = lambda msg: (_heartbeat(attempt_id, "protocol"), _log(task_id, msg))[1]
                    _platform.bind_task_control(control)
                    if getattr(_platform, "mailbox", None) is not None:
                        _platform.mailbox._task_attempt_token = attempt_id
                        _platform.mailbox._log_fn = _platform._log_fn
                    account = _platform.register(email=req.email or None, password=req.password)
                    child = None

                account = None
                if not _should_run_register_in_child_process():
                    # pytest 需要 mock、类变量计数和注册表 patch 均留在当前进程。
                    _run_in_current_process("pytest 环境禁用注册子进程")
                else:
                    try:
                        child_context = multiprocessing.get_context("spawn")
                        child_queue = child_context.Queue()
                        child = child_context.Process(
                            target=_register_platform_child,
                            args=({
                                "platform": req.platform, "executor_type": req.executor_type,
                                "captcha_solver": req.captcha_solver, "proxy": _proxy,
                                "extra": dict(merged_extra), "email": req.email, "password": req.password,
                            }, child_queue), daemon=True,
                        )
                        child.start()
                    except OSError as exc:
                        # 某些受限 Windows 会话（包括 CI）禁止 spawn。降级保留原线程链路。
                        _run_in_current_process(f"子进程不可用: {exc}")
                attempt_timeout = _attempt_timeout_seconds(req.platform, merged_extra)
                deadline = time.monotonic() + attempt_timeout if attempt_timeout is not None else None
                account = locals().get("account", None)
                child_error = ""
                while child is not None and child.is_alive() and not account and not child_error:
                    control.checkpoint(attempt_id=attempt_id)
                    try:
                        kind, value = child_queue.get(timeout=1.0)
                    except queue.Empty:
                        _heartbeat(attempt_id, "child_register")
                        if deadline is not None and time.monotonic() >= deadline:
                            from services.sms_service import cancel_expired_sms_activations
                            cancelled = cancel_expired_sms_activations(merged_extra, proxy=_proxy)
                            child.terminate()
                            child.join(timeout=10)
                            raise TimeoutError(
                                f"单账号注册超过 {attempt_timeout}s，"
                                f"已终止子进程；超时接码取消 {cancelled} 个"
                            )
                        continue
                    if kind == "log":
                        _heartbeat(attempt_id, "protocol")
                        _log(task_id, value)
                    elif kind == "result":
                        account = value
                    else:
                        child_error = str(value)
                if child is not None:
                    child.join(timeout=5)
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=10)
                if child_error:
                    raise RuntimeError(child_error)
                if account is None:
                    raise RuntimeError(f"注册子进程异常退出（exitcode={child.exitcode if child else 'n/a'}）")
                _heartbeat(attempt_id, "persisting_account")
                _mailbox = None
                current_email = account.email or current_email
                # 手机号注册且没绑上邮箱时，account.email 存的是号码，没有域名可校验
                if (
                    str(merged_extra.get("mail_provider", "")).strip() == "cfworker"
                    and "@" in (account.email or "")
                ):
                    from core.email_domain_policy import validate_email_domain_policy

                    validate_email_domain_policy(
                        account.email,
                        {
                            "email_domain_rule_enabled": merged_extra.get(
                                "email_domain_rule_enabled", "0"
                            ),
                            "email_domain_level_count": merged_extra.get(
                                "email_domain_level_count", "2"
                            ),
                        },
                    )
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault(
                                "luckmail_project_code",
                                merged_extra.get("luckmail_project_code"),
                            )
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault(
                                "luckmail_email_type",
                                merged_extra.get("luckmail_email_type"),
                            )
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault(
                                "luckmail_domain", merged_extra.get("luckmail_domain")
                            )
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault(
                                "luckmail_base_url",
                                merged_extra.get("luckmail_base_url"),
                            )
                # 邮箱状态由注册子进程随结果返回；父进程统一提交，SQLite 下避免
                # 多进程同时写邮箱池导致锁竞争，PostgreSQL 则正常批量事务提交。
                if isinstance(account.extra, dict):
                    from core.base_mailbox import apply_mailbox_status_events
                    apply_mailbox_status_events(account.extra.pop("mailbox_status_events", []))
                saved_account = save_account(account)
                if _proxy:
                    _proxy_pool.report_success(_proxy)
                _log(task_id, f"[OK] 注册成功: {account.email}")
                _save_task_log(req.platform, account.email, "success")
                _auto_upload_integrations(task_id, saved_account or account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _log(task_id, f"  [升级链接] {cashier_url}")
                    _task_store.add_cashier_url(task_id, cashier_url)
                    _persist_task_snapshot(task_id)
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _log(task_id, f"[STOP] {e}")
                return AttemptResult.stopped(str(e))
            except Exception as e:
                if _proxy:
                    _proxy_pool.report_fail(_proxy)
                _log(task_id, f"[FAIL] 注册失败{round_suffix}: {e}")
                return AttemptResult.failed(
                    str(e),
                    retryable=not isinstance(e, NonRetryableRegisterError),
                    email=current_email,
                )
            finally:
                # stop/skip 或父流程异常时也必须清理子进程，不能把孤儿进程留在
                # 后台继续占用号码、代理和网络连接。
                if child is not None and child.is_alive():
                    child.terminate()
                    child.join(timeout=10)
                if child_queue is not None:
                    try:
                        child_queue.close()
                    except Exception:
                        pass
                control.finish_attempt(attempt_id)
                if _proxy:
                    _proxy_pool.release(_proxy)
                if sms_slot_acquired:
                    global_sms_limiter.release()
                if openai_slot_acquired:
                    global_openai_limiter.release()
                if mail_slot_acquired:
                    global_mail_limiter.release()
                if concurrency_slot_acquired:
                    global_task_concurrency_limiter.release()

        from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait

        # 滑动窗口最多保留 max_workers 个 Future，避免大任务一次创建数万个 Future。
        # 数据库模式硬上限：PostgreSQL 200，SQLite 40。
        max_workers = min(req.concurrency, req.count, DATABASE_REGISTER_CONCURRENCY_CAP)
        stopped = False
        next_index = 0
        last_stall_notice_at = 0.0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pending = set()

            def _submit_next() -> bool:
                nonlocal next_index
                if next_index >= req.count or control.is_stop_requested():
                    return False
                pending.add(pool.submit(_do_one, next_index))
                next_index += 1
                return True

            while len(pending) < max_workers and _submit_next():
                pass
            while pending:
                # 有完成项时立即处理；没有时每秒醒来一次检查心跳，避免复现上次 40
                # 个 Future 无限 wait、页面只有 running 而没有任何诊断。
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                if not done:
                    state = control.snapshot()
                    active = int(state.get("active_attempts") or 0)
                    stale = int(state.get("oldest_attempt_heartbeat_seconds") or 0)
                    threshold = (TASK_STALL_DIAGNOSIS_SECONDS if stale >= TASK_STALL_DIAGNOSIS_SECONDS
                                 else TASK_STALL_WARNING_SECONDS)
                    now = time.monotonic()
                    if active and stale >= threshold and now - last_stall_notice_at >= 30:
                        level = "疑似卡死" if stale >= TASK_STALL_DIAGNOSIS_SECONDS else "心跳告警"
                        _log(task_id, f"[SYSTEM] {level}: active_attempts={active}，"
                             f"最长无心跳 {stale}s，阶段={state.get('attempt_stage_counts') or {}}")
                        last_stall_notice_at = now
                    continue
                for future in done:
                    try:
                        result = future.result()
                    except CancelledError:
                        continue
                    except Exception as e:
                        _log(task_id, f"[ERROR] 任务线程异常: {e}")
                        errors.append(str(e))
                    else:
                        if result.outcome == AttemptOutcome.SUCCESS:
                            success += 1
                        elif result.outcome == AttemptOutcome.SKIPPED:
                            skipped += 1
                        elif result.outcome == AttemptOutcome.STOPPED:
                            stopped = True
                        else:
                            errors.append(result.message)
                    _task_store.update_counters(
                        task_id,
                        success=success,
                        registered=success + skipped + len(errors),
                    )
                    _persist_task_snapshot(task_id)

                if stopped or control.is_stop_requested():
                    stopped = True
                    for future in pending:
                        future.cancel()
                    continue
                while len(pending) < max_workers and _submit_next():
                    pass
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        _task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _persist_task_snapshot(task_id, force=True)
        _task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    _log(task_id, summary)
    _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=success + skipped + len(errors),
        skipped=skipped,
        errors=errors,
    )
    _persist_task_snapshot(task_id, force=True)
    _task_store.cleanup()


def _load_account_fields(account_id: int) -> Optional[dict]:
    """把一行账号读成纯数据。

    后面那几十秒网络请求期间不能占着数据库连接不放：连接池就那么几条，攥在手里
    会把面板其它请求一起拖住。
    """
    from core.db import AccountModel

    with Session(engine) as s:
        account = s.get(AccountModel, account_id)
        if account is None or account.platform != "chatgpt":
            return None
        return {
            "email": account.email,
            "password": account.password,
            "extra": account.get_extra(),
            "token": account.token,
        }


def _run_account_batch_task(
    task_id: str,
    account_ids: list[int],
    *,
    label: str,
    concurrency: int = 1,
    delay_seconds: float = 0,
    proxy: Optional[str] = None,
    handle_account: Callable[..., AttemptResult],
) -> None:
    """「逐个号跑一遍」这类后台任务的调度骨架（补 RT、绑 2FA 都走这里）。

    排队限速、可停可跳、计数收尾这些每个批量任务都一样，只有每个号具体做什么
    不同 —— 那部分由 ``handle_account`` 提供，进度和日志复用注册任务那套。
    """
    from core.proxy_pool import proxy_pool
    from core.proxy_utils import normalize_proxy_url

    control = _task_store.control_for(task_id)
    _task_store.mark_running(task_id)
    _persist_task_snapshot(task_id)
    _log(
        task_id,
        f"外部服务预算: OpenAI {global_openai_limiter.limit}，邮箱 {global_mail_limiter.limit}，接码 {global_sms_limiter.limit}，任务总并发 {global_task_concurrency_limiter.limit}",
    )
    if proxy:
        _log(task_id, "资源预检: 使用固定代理；默认同一代理同时只运行 1 个账号")
    else:
        available_proxies = proxy_pool.active_count()
        if available_proxies == 0:
            _log(task_id, "资源预检: 未配置可用代理，将使用直连；高并发可能触发风控或限流")
        elif available_proxies < min(concurrency, len(account_ids)):
            _log(task_id, f"资源预检: 可用代理 {available_proxies} 个，低于目标并发 {min(concurrency, len(account_ids))}；任务会等待代理租约")
        else:
            _log(task_id, f"资源预检: 可用代理 {available_proxies} 个，满足目标并发")

    total = len(account_ids)
    success = 0
    skipped = 0
    errors: list[str] = []
    stopped = False
    start_gate = threading.Lock()
    next_start_time = time.time()

    def _resolve_proxy() -> Optional[str]:
        return normalize_proxy_url(
            proxy_pool.acquire(
                preferred_url=proxy,
                is_stop_requested=control.is_stop_requested,
            )
        )

    def _wait_turn(attempt_id: int | None) -> None:
        nonlocal next_start_time
        if delay_seconds <= 0:
            return
        with start_gate:
            control.checkpoint(attempt_id=attempt_id)
            remaining = max(0.0, next_start_time - time.time())
            while remaining > 0:
                control.checkpoint(attempt_id=attempt_id)
                chunk = min(0.25, remaining)
                time.sleep(chunk)
                remaining -= chunk
            next_start_time = time.time() + delay_seconds

    def _do_one(index: int, account_id: int) -> AttemptResult:
        attempt_id: int | None = None
        account_proxy: Optional[str] = None
        concurrency_slot_acquired = False
        openai_slot_acquired = False
        mail_slot_acquired = False
        try:
            control.checkpoint()
            concurrency_slot_acquired = global_task_concurrency_limiter.acquire(
                control.is_stop_requested
            )
            # 补 RT / 绑 2FA 都会调用 OpenAI；允许重新登录时可能等待邮箱验证码。
            openai_slot_acquired = global_openai_limiter.acquire(control.is_stop_requested)
            if getattr(handle_account, "uses_mail", False):
                mail_slot_acquired = global_mail_limiter.acquire(control.is_stop_requested)
            control.checkpoint()
            attempt_id = control.start_attempt()
            _wait_turn(attempt_id)
            control.checkpoint(attempt_id=attempt_id)

            account_proxy = _resolve_proxy()
            fields = _load_account_fields(account_id)
            if fields is None:
                _log(task_id, f"[SKIP] 账号 #{account_id} 不存在")
                return AttemptResult.skipped("账号不存在")

            _task_store.set_progress(task_id, f"{index + 1}/{total}")
            _log(task_id, f"开始{label} {index + 1}/{total}: {fields['email']}")
            if account_proxy:
                _log(task_id, f"使用代理: {account_proxy}")

            result = handle_account(
                account_id=account_id,
                fields=fields,
                proxy=account_proxy,
                control=control,
                attempt_id=attempt_id,
            )
            if account_proxy:
                if result.outcome == AttemptOutcome.FAILED:
                    proxy_pool.report_fail(account_proxy)
                elif result.outcome == AttemptOutcome.SUCCESS:
                    proxy_pool.report_success(account_proxy)
            return result
        except SkipCurrentAttemptRequested as e:
            _log(task_id, f"[SKIP] 已跳过当前账号: {e}")
            return AttemptResult.skipped(str(e))
        except StopTaskRequested as e:
            _log(task_id, f"[STOP] {e}")
            return AttemptResult.stopped(str(e))
        except Exception as e:
            _log(task_id, f"[FAIL] 账号 #{account_id} {label}异常: {e}")
            return AttemptResult.failed(str(e))
        finally:
            control.finish_attempt(attempt_id)
            if account_proxy:
                proxy_pool.release(account_proxy)
            if mail_slot_acquired:
                global_mail_limiter.release()
            if openai_slot_acquired:
                global_openai_limiter.release()
            if concurrency_slot_acquired:
                global_task_concurrency_limiter.release()

    try:
        from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait

        # 固定大小的 Future 窗口：账号数很大时内存仍只随并发数增长。
        max_workers = max(1, min(int(concurrency or 1), max(total, 1)))
        next_index = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pending = set()

            def _submit_next() -> bool:
                nonlocal next_index
                if next_index >= total or control.is_stop_requested():
                    return False
                pending.add(pool.submit(_do_one, next_index, account_ids[next_index]))
                next_index += 1
                return True

            while len(pending) < max_workers and _submit_next():
                pass
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        result = future.result()
                    except CancelledError:
                        continue
                    except Exception as e:
                        _log(task_id, f"[ERROR] 任务线程异常: {e}")
                        errors.append(str(e))
                    else:
                        if result.outcome == AttemptOutcome.SUCCESS:
                            success += 1
                        elif result.outcome == AttemptOutcome.SKIPPED:
                            skipped += 1
                        elif result.outcome == AttemptOutcome.STOPPED:
                            stopped = True
                        else:
                            errors.append(result.message)
                    _task_store.update_counters(
                        task_id,
                        success=success,
                        registered=success + skipped + len(errors),
                    )
                    _persist_task_snapshot(task_id)

                if stopped or control.is_stop_requested():
                    stopped = True
                    for future in pending:
                        future.cancel()
                    continue
                while len(pending) < max_workers and _submit_next():
                    pass
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        _task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _persist_task_snapshot(task_id, force=True)
        _task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    prefix = f"{label}已停止" if final_status == "stopped" else f"{label}完成"
    _log(task_id, f"{prefix}: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个")
    _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=success + skipped + len(errors),
        skipped=skipped,
        errors=errors,
    )
    _persist_task_snapshot(task_id, force=True)
    _task_store.cleanup()


def _run_backfill_rt(task_id: str, account_ids: list[int], req: BackfillRtTaskRequest):
    """批量补 RT。逐号跑，可停可跳，进度和日志复用注册任务那套。"""
    from core.config_store import config_store
    from core.db import AccountModel
    from services.chatgpt_rt_backfill import apply_backfill_result, backfill_account_data

    base_config = config_store.get_all() or {}
    # 仅作用于本次补 RT，不能修改全局接码配置。
    base_config["sms_max_phone_attempts"] = str(req.sms_max_phone_attempts)

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        email = fields["email"]
        result = backfill_account_data(
            email=email,
            password=fields["password"],
            extra=fields["extra"],
            token=fields["token"],
            config=base_config,
            proxy=proxy,
            allow_login=req.allow_login,
            log_fn=lambda msg: _log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
        )

        with Session(engine) as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_backfill_result(account, result, session=s, commit=True)

        if result.success:
            _log(task_id, f"[OK] {email} {result.summary()}")
            _save_task_log("chatgpt", email, "success", detail={"action": "backfill_rt"})
            return AttemptResult.success()

        _log(task_id, f"[FAIL] {email} {result.summary()}")
        _save_task_log(
            "chatgpt",
            email,
            "failed",
            error=result.summary(),
            detail={"action": "backfill_rt"},
        )
        return AttemptResult.failed(f"{email}: {result.summary()}")

    _handle.uses_mail = req.allow_login

    _run_account_batch_task(
        task_id,
        account_ids,
        label="补 RT",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


def _run_bind_2fa(task_id: str, account_ids: list[int], req: Bind2faTaskRequest):
    """批量绑 2FA。和补 RT 同一套调度，区别只在每个号跑什么。"""
    from core.config_store import config_store
    from core.db import AccountModel
    from services.chatgpt_two_factor import apply_two_factor_result, bind_account_two_factor

    base_config = config_store.get_all() or {}

    def _handle(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        email = fields["email"]
        result = bind_account_two_factor(
            email=email,
            password=fields["password"],
            extra=fields["extra"],
            token=fields["token"],
            config=base_config,
            proxy=proxy,
            allow_login=req.allow_login,
            log_fn=lambda msg: _log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
        )

        with Session(engine) as s:
            account = s.get(AccountModel, account_id)
            if account is not None:
                apply_two_factor_result(account, result, session=s, commit=True)

        if result.ok:
            _log(task_id, f"[OK] {email} {result.summary()}")
            # 密钥只下发这一次，任务日志是用户当场导入验证器的唯一途径
            if result.secret:
                _log(task_id, f"  TOTP 密钥: {result.secret}")
            _save_task_log("chatgpt", email, "success", detail={"action": "bind_2fa"})
            return AttemptResult.success()

        if result.already_bound:
            _log(task_id, f"[SKIP] {email} {result.summary()}")
            return AttemptResult.skipped(f"{email}: {result.summary()}")

        _log(task_id, f"[FAIL] {email} {result.summary()}")
        _save_task_log(
            "chatgpt",
            email,
            "failed",
            error=result.summary(),
            detail={"action": "bind_2fa"},
        )
        return AttemptResult.failed(f"{email}: {result.summary()}")

    _handle.uses_mail = req.allow_login

    _run_account_batch_task(
        task_id,
        account_ids,
        label="绑 2FA",
        concurrency=req.concurrency,
        delay_seconds=req.delay_seconds,
        proxy=req.proxy,
        handle_account=_handle,
    )


@router.post("/backfill-rt")
def create_backfill_rt_task(req: BackfillRtTaskRequest, background_tasks: BackgroundTasks):
    """批量给缺 refresh_token 的 ChatGPT 账号补 RT。"""
    from services.chatgpt_rt_backfill import select_backfill_targets

    _validate_task_concurrency(req.concurrency)
    with Session(engine) as s:
        try:
            accounts, missing_ids = select_backfill_targets(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                only_missing_rt=req.only_missing_rt,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [int(row.id) for row in accounts if row.id]

    if not account_ids:
        if missing_ids:
            detail = "所选账号不存在"
        elif req.only_missing_rt:
            detail = "所选账号都已经有 RT 了"
        else:
            detail = "没有匹配的账号"
        raise HTTPException(400, detail)

    task_id = f"backfill_rt_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="backfill_rt",
        meta={
            "kind": "backfill_rt",
            "only_missing_rt": req.only_missing_rt,
            "allow_login": req.allow_login,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "sms_max_phone_attempts": req.sms_max_phone_attempts,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待补 RT 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_backfill_rt, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/bind-2fa")
def create_bind_2fa_task(req: Bind2faTaskRequest, background_tasks: BackgroundTasks):
    """给库里已有的 ChatGPT 账号补绑 TOTP 2FA。"""
    from services.chatgpt_two_factor import select_two_factor_targets

    _validate_task_concurrency(req.concurrency)
    with Session(engine) as s:
        try:
            accounts, missing_ids = select_two_factor_targets(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                only_missing_2fa=req.only_missing_2fa,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [int(row.id) for row in accounts if row.id]

    if not account_ids:
        if missing_ids:
            detail = "所选账号不存在"
        elif req.only_missing_2fa:
            detail = "所选账号都已经有 2FA 密钥了"
        else:
            detail = "没有匹配的账号"
        raise HTTPException(400, detail)

    task_id = f"bind_2fa_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="bind_2fa",
        meta={
            "kind": "bind_2fa",
            "only_missing_2fa": req.only_missing_2fa,
            "allow_login": req.allow_login,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待绑 2FA 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_bind_2fa, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.post("/{task_id}/skip-current")
def skip_current_account(task_id: str):
    _finalize_orphan_tasks()
    _ensure_task_mutable(task_id)
    if not _task_store.exists(task_id):
        raise HTTPException(409, "任务已结束或服务已重启，无法跳过当前账号")
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "收到手动跳过当前账号请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    _finalize_orphan_tasks()
    _ensure_task_mutable(task_id)
    if not _task_store.exists(task_id):
        raise HTTPException(409, "任务已结束或服务已重启，无法停止")
    control = _task_store.request_stop(task_id)
    _log(task_id, "收到手动停止任务请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    with Session(engine) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        total = len(s.exec(q).all())
        items = s.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": items}


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    _finalize_orphan_tasks()
    _ensure_task_exists(task_id)

    async def event_generator():
        sent = since
        use_memory = _task_store.exists(task_id)
        while True:
            if use_memory:
                logs, status = _task_store.log_state(task_id)
                snapshot = _task_store.snapshot(task_id)
                _persist_task_snapshot(task_id)
            else:
                snapshot = _get_persisted_task(task_id) or {}
                logs = snapshot.get("logs") or []
                status = snapshot.get("status") or "failed"
            counters = {
                "success": int(snapshot.get("success") or 0),
                "registered": int(snapshot.get("registered") or 0),
                "total": int(snapshot.get("total") or 0),
            }
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent], **counters})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status, **counters})}\n\n"
                break
            if not use_memory:
                # 非内存任务仅提供持久化快照，不进入无限轮询
                yield f"data: {json.dumps({'done': True, 'status': 'stopped', **counters})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    _finalize_orphan_tasks()
    return _get_task_snapshot(task_id)


@router.get("")
def list_tasks():
    _finalize_orphan_tasks()
    # 以 DB 为主返回，避免进程重启导致列表丢失
    return _list_persisted_tasks()


@router.delete("/{task_id}")
def delete_task(task_id: str):
    _finalize_orphan_tasks()
    snapshot = _get_task_snapshot(task_id)
    status = str(snapshot.get("status") or "")
    if status in {"pending", "running"}:
        raise HTTPException(409, "运行中的任务不允许删除，请先停止任务")
    with Session(engine) as s:
        row = s.get(TaskRunModel, task_id)
        if row is not None:
            s.delete(row)
            s.commit()
    return {"ok": True, "task_id": task_id}
