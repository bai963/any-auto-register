"""补 RT 的库侧胶水：挑号、跑引擎、把结果写回账号表。

引擎（``platforms.chatgpt.rt_backfill``）刻意不认识数据库，这里负责把 ``accounts``
表的一行翻译成引擎要的入参，再把拿到的凭证塞回 ``extra_json``。每个号的补号
过程还会在 extra 里留一份 ``chatgpt_rt_backfill`` 留痕，方便事后查是哪条策略
成的、失败又是卡在哪。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlmodel import Session

from core.db import AccountModel
from platforms.chatgpt.rt_backfill import BackfillResult, RefreshTokenBackfiller
from services.chatgpt_account_selection import select_chatgpt_accounts

logger = logging.getLogger(__name__)

# 账号表历史上会把单手机号放在 ``email`` 列。补 RT 前先按登录标识本身分流，
# 只接受 E.164，避免
# 将任意包含数字的字符串误判为手机号；其余值仍按邮箱路径处理以保持兼容。
_E164_ACCOUNT_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_ACCOUNT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def account_identifier_kind(value: str) -> str:
    """返回 ``phone``、``email`` 或 ``unknown``，用于补 RT 策略分流。"""
    identifier = str(value or "").strip()
    if _E164_ACCOUNT_RE.fullmatch(identifier):
        return "phone"
    if _EMAIL_ACCOUNT_RE.fullmatch(identifier):
        return "email"
    return "unknown"


def account_refresh_token(model: AccountModel) -> str:
    """返回已明确识别为 ChatGPT/Codex 的 RT，而不是微软邮箱 OAuth RT。"""
    extra = model.get_extra()
    refresh_token = str(extra.get("refresh_token") or extra.get("refreshToken") or "").strip()
    if not refresh_token:
        return ""
    source = str(extra.get("chatgpt_refresh_token_source") or "").strip().lower()
    if source in {"codex_oauth", "openai", "chatgpt"}:
        return refresh_token
    # 历史 bug：微软邮箱 refresh_token 曾覆盖到标准 RT 字段。检测到微软 OAuth
    # 凭据且没有 ChatGPT 来源标记时，强制显示为缺 RT 并允许补 RT 修复。
    is_microsoft = str(extra.get("mail_provider") or extra.get("provider") or "").lower() == "microsoft"
    if is_microsoft and (extra.get("client_id") or extra.get("mailbox_refresh_token")):
        return ""
    return refresh_token

def account_missing_rt(model: AccountModel) -> bool:
    return not account_refresh_token(model)


def select_backfill_targets(
    session: Session,
    *,
    account_ids: Optional[Iterable[int]] = None,
    all_filtered: bool = False,
    email: str = "",
    status: str = "",
    plus_status: str = "",
    only_missing_rt: bool = True,
) -> tuple[list[AccountModel], list[int]]:
    """挑出要补 RT 的号，返回 ``(账号列表, 找不到的 id)``。

    ``only_missing_rt`` 是默认行为：已经有 RT 的号再跑一遍纯属给 OpenAI 送风控
    素材。想强制重拿（比如怀疑旧 RT 失效）才关掉它。
    """
    return select_chatgpt_accounts(
        session,
        account_ids=account_ids,
        all_filtered=all_filtered,
        email=email,
        status=status,
        plus_status=plus_status,
        keep=account_missing_rt if only_missing_rt else None,
    )


def backfill_account_data(
    *,
    email: str,
    password: str = "",
    extra: Optional[dict] = None,
    token: str = "",
    config: Optional[dict] = None,
    proxy: Optional[str] = None,
    allow_login: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
    task_control=None,
    attempt_id=None,
) -> BackfillResult:
    """按账号字段补 RT，不落库（落库交给 ``apply_backfill_result``）。

    只收纯数据不收 ORM 对象：一个号要跑几十秒网络请求，调用方得以在这期间
    把数据库连接还回池子里。

    ``task_control`` 传的是后台任务的停止/跳过开关，会一路交到邮箱的等码循环
    里 —— 等验证码是整个补号流程里最长的一段，不接开关就停不下来。
    """
    from services.chatgpt_otp_mailbox import resolve_otp_mail_provider

    extra = dict(extra or {})
    config = dict(config or _load_config())
    log = log_fn or logger.info

    identifier_kind = account_identifier_kind(email)
    # 按账号字符串分流：E.164 走手机号登录，邮箱格式保留邮箱补 RT。
    is_phone_only = identifier_kind == "phone"
    if is_phone_only:
        mail_provider, mail_reason = None, "账号字符串为手机号，跳过邮箱收件通道"
        log(f"[补RT] {email} 识别为手机号账号，跳过邮箱收件通道；直连失败后将尝试手机号登录")
    else:
        if identifier_kind == "email":
            log(f"[补RT] {email} 识别为邮箱账号，保留邮箱补 RT 流程")
        else:
            log(f"[补RT] {email} 无法确认邮箱/手机号格式，按兼容策略保留邮箱补 RT 流程")
        mail_provider, mail_reason = resolve_otp_mail_provider(
            email, account_extra=extra, config=config, proxy=proxy, log_fn=log,
            task_control=task_control, attempt_id=attempt_id,
        )
        if mail_provider is None:
            if allow_login:
                log(f"[补RT] {email} 暂时读不到收件箱（{mail_reason}），需要邮箱验证码时会失败")
        else:
            log(f"[补RT] 收件通道: {getattr(mail_provider, 'display_name', '邮箱')} → {email}")

    return RefreshTokenBackfiller(
        email=email,
        password=password,
        session_token=str(extra.get("session_token") or ""),
        access_token=str(extra.get("access_token") or token or ""),
        device_id=str(extra.get("device_id") or ""),
        totp_secret=str(extra.get("totp_secret") or ""),
        proxy=proxy,
        extra_config=config,
        mail_provider=mail_provider,
        mail_unavailable_reason=mail_reason,
        allow_login=allow_login,
        phone_only=is_phone_only,
        log_fn=log,
    ).run()


def build_extra_patch(result: BackfillResult) -> dict[str, Any]:
    """把补号结果整理成可以合并进 ``extra_json`` 的补丁。

    只写非空字段：会话复用那条路常常只刷新了 access_token，用空串覆盖掉库里
    原有的 session_token 等于把号弄坏。
    """
    patch: dict[str, Any] = {}
    # 未验证候选 RT 禁止写库；AT/session 等已从可信会话刷新到的字段仍可保留。
    for key in ("access_token", "session_token", "id_token"):
        value = str(getattr(result, key, "") or "").strip()
        if value:
            patch[key] = value
    verified_rt = (
        str(getattr(result, "refresh_token", "") or "").strip()
        if result.success and result.refresh_token_verified
        else ""
    )
    if verified_rt:
        patch["refresh_token"] = verified_rt
        # 只认可经过 refresh-token grant 验证（并已接住轮换值）的 Codex RT。
        patch["chatgpt_has_refresh_token_solution"] = True
        patch["chatgpt_refresh_token_source"] = "codex_oauth"
    if result.cookie_header:
        patch["cookies"] = result.cookie_header
    patch["chatgpt_rt_backfill"] = {
        "ok": result.success,
        "strategy": result.strategy,
        "message": result.summary(),
        "refresh_token_verified": bool(result.refresh_token_verified),
        "refresh_token_verify_error": result.refresh_token_verify_error,
        "attempts": [
            {"strategy": item.strategy, "ok": item.ok, "message": item.message}
            for item in result.attempts
        ],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return patch


def apply_backfill_result(
    model: AccountModel,
    result: BackfillResult,
    *,
    session: Optional[Session] = None,
    commit: bool = False,
) -> dict[str, Any]:
    """把补号结果落到账号行上，返回实际写入的补丁。"""
    patch = build_extra_patch(result)
    extra = model.get_extra()
    extra.update(patch)
    model.set_extra(extra)
    # 绑定成功后只更新账号字符串本身。下次补 RT 会按新的邮箱字符串分流，
    # 不写 phone_number / register_flow / bound_email 等数据库分类字段。
    if result.bound_email:
        model.email = result.bound_email
    if patch.get("access_token"):
        model.token = patch["access_token"]
    model.updated_at = datetime.now(timezone.utc)
    if session is not None:
        session.add(model)
        if commit:
            session.commit()

    # 手机号登录要求绑定邮箱时，绑定流程会把实际领取的邮箱作为 ``used``
    # 事件带回。必须在父进程回写邮箱池，避免该邮箱再次被分配；失败邮箱仍由
    # 引擎以 failed 事件处理。这里集中处理，也覆盖非 tasks API 的调用方。
    if result.mailbox_status_events:
        from core.base_mailbox import apply_mailbox_status_events
        apply_mailbox_status_events(result.mailbox_status_events)
    return patch


def _load_config() -> dict:
    from core.config_store import config_store

    return config_store.get_all() or {}
