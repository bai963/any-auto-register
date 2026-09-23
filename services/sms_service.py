"""手机接码服务（sms-activate 协议系）。

供 ChatGPT 注册链路命中 ``add-phone`` 时自动租号、收码、验证。

目前接入 SmsBower 与 HeroSMS，两家共用 sms-activate 的 ``handler_api.php`` 协议，
差别只在 base_url 和固定价格参数的写法上，所以由同一个 provider 类覆盖。

三段式用法（``AuthFlow._handle_add_phone_via_sms`` 就是这么调的）：

    controller = build_phone_callback(config, log_fn=...)
    phone = controller.get_phone()          # 租号
    code = controller.get_code(timeout=80)  # 等短信
    controller.report_success()             # 业务侧验证通过

⚠️ OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，纯 SMS 路径实测只有泰国
（country_id=52）稳定可用。其它国家可能抽到 WhatsApp 号导致收不到短信，所以
``OPENAI_SMS_COUNTRIES`` 白名单之外的国家在自动选号时会打告警但不阻止。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.db import (
    DATABASE_BACKEND, DATABASE_REGISTER_CONCURRENCY_CAP, SmsActivationModel,
    SmsCleanupJobModel, database_write_session, engine,
)
import requests

logger = logging.getLogger(__name__)

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # 泰国 —— OpenAI 走纯 SMS 的稳定国家
# 从接码平台成功租到号码起算；超过此期限仍未收到验证码就必须取消并申请退款。
SMS_WAIT_TIMEOUT_SECONDS = 240
# No successful send: refund after ten minutes from rental. Successful send:
# refund after four minutes without a code.
SMS_ACTIVATION_MAX_LIFETIME_SECONDS = 600
SMS_CODE_WAIT_AFTER_SEND_SECONDS = 240
SMS_REFUND_SCAN_INTERVAL_SECONDS = 5
SMS_REFUND_LEASE_SECONDS = 45
SMS_HTTP_TIMEOUT = (5, 15)  # connect, read；任何接码 HTTP 都不能无限阻塞

# OpenAI 走纯 SMS 的国家白名单（截至 2025-2026 实测；其它国家会抽到 WhatsApp 号）
OPENAI_SMS_COUNTRIES = {"52"}

SMS_PROVIDERS: dict[str, dict[str, str]] = {
    "smsbower": {
        "label": "SmsBower",
        "base_url": "https://smsbower.page/stubs/handler_api.php",
    },
    "herosms": {
        "label": "HeroSMS",
        "base_url": "https://hero-sms.com/stubs/handler_api.php",
    },
}

def _sms_limit(name: str, default: int) -> int:
    import os
    try:
        # 接码 HTTP 的局部预算绝不可以突破数据库模式下的任务硬上限：
        # PostgreSQL 为 200，SQLite 为 40。
        return max(1, min(int(os.getenv(name, str(default))), DATABASE_REGISTER_CONCURRENCY_CAP))
    except ValueError:
        return default


# 租号接口常有供应商侧 QPS 限制，不能因为注册 worker=200 就同时请求 200 次；
# 轮询和退号则必须覆盖全部活跃 activation，保证四分钟超时能及时退号。
_SMS_RENT_SEMAPHORE = threading.BoundedSemaphore(
    _sms_limit("SMS_RENT_MAX_CONCURRENCY", 50 if DATABASE_REGISTER_CONCURRENCY_CAP == 200 else 15)
)
_SMS_POLL_SEMAPHORE = threading.BoundedSemaphore(
    _sms_limit("SMS_POLL_MAX_CONCURRENCY", DATABASE_REGISTER_CONCURRENCY_CAP)
)
_SMS_CLOSE_SEMAPHORE = threading.BoundedSemaphore(
    _sms_limit("SMS_CLOSE_MAX_CONCURRENCY", 100 if DATABASE_REGISTER_CONCURRENCY_CAP == 200 else 30)
)


@contextmanager
def _sms_slot(kind: str):
    semaphore = {"rent": _SMS_RENT_SEMAPHORE, "poll": _SMS_POLL_SEMAPHORE, "close": _SMS_CLOSE_SEMAPHORE}[kind]
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""

    activation_id: str
    phone_number: str  # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到码即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(
        self,
        *,
        service: str,
        country: str = "",
        country_candidates: Optional[list[str]] = None,
    ) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def _finish_activation(self, activation_id: str, *, reason: str = "") -> bool:
        """确认号码已被业务侧使用后结束 activation，绝不能再走取消退款。"""
        ok = False
        try:
            response = self._request({"action": "finishActivation", "id": activation_id})
            ok = response.status_code in (200, 204) or "ACCESS" in response.text
        except Exception:
            try:
                response = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                ok = "ACCESS" in response.text or response.status_code in (200, 204)
            except Exception:
                ok = False
        logger.info(
            "号码 activation_id=%s 结束%s（原因: %s）",
            activation_id,
            "成功" if ok else "失败",
            (reason or "业务验证完成")[:80],
        )
        if ok:
            complete = getattr(self, "_db_complete", None)
            if callable(complete):
                complete(activation_id, "finished")
            forget = getattr(self, "_forget_activation", None)
            if callable(forget):
                forget(activation_id)
        if not ok:
            enqueue = getattr(self, "_enqueue_cleanup", None)
            if callable(enqueue):
                enqueue(activation_id, "finish", reason or "业务验证完成")
        return ok

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台据此结算。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到码但 validate 失败 → 记下这个码，别再拿它去验。"""

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 取消并退款。"""

    def complete_activation(self, activation_id: str, reason: str = "") -> None:
        """号码已被业务侧占用，结束 activation，不能走退款。"""

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""


SMS_COUNTRY_NAMES_CN: dict[str, str] = {
    "0": "俄罗斯", "1": "乌克兰", "2": "哈萨克斯坦", "3": "中国", "4": "菲律宾",
    "5": "缅甸", "6": "印度尼西亚", "7": "马来西亚", "8": "肯尼亚", "9": "坦桑尼亚",
    "10": "越南", "11": "吉尔吉斯斯坦", "12": "美国(虚拟)", "13": "以色列", "14": "香港",
    "15": "波兰", "16": "英国", "17": "马达加斯加", "18": "刚果(布)", "19": "尼日利亚",
    "20": "澳门", "21": "埃及", "22": "印度", "23": "爱尔兰", "24": "柬埔寨",
    "25": "老挝", "26": "海地", "27": "科特迪瓦", "28": "冈比亚", "29": "塞尔维亚",
    "30": "也门", "31": "南非", "32": "罗马尼亚", "33": "哥伦比亚", "34": "爱沙尼亚",
    "35": "阿塞拜疆", "36": "加拿大", "37": "摩洛哥", "38": "加纳", "39": "阿根廷",
    "40": "乌兹别克斯坦", "41": "喀麦隆", "42": "乍得", "43": "德国", "44": "立陶宛",
    "45": "克罗地亚", "46": "瑞典", "47": "伊拉克", "48": "荷兰", "49": "拉脱维亚",
    "50": "奥地利", "51": "白俄罗斯", "52": "泰国", "53": "沙特阿拉伯", "54": "墨西哥",
    "55": "台湾", "56": "西班牙", "57": "伊朗", "58": "阿尔及利亚", "59": "斯洛文尼亚",
    "60": "孟加拉国", "61": "塞内加尔", "62": "土耳其", "63": "捷克", "64": "斯里兰卡",
    "65": "秘鲁", "66": "巴基斯坦", "67": "新西兰", "68": "几内亚", "69": "马里",
    "70": "委内瑞拉", "71": "埃塞俄比亚", "72": "蒙古", "73": "巴西", "74": "阿富汗",
    "75": "乌干达", "76": "安哥拉", "77": "塞浦路斯", "78": "法国", "79": "巴布亚新几内亚",
    "80": "莫桑比克", "81": "尼泊尔", "82": "比利时", "83": "保加利亚", "84": "匈牙利",
    "85": "摩尔多瓦", "86": "意大利", "87": "巴拉圭", "88": "洪都拉斯", "89": "突尼斯",
    "90": "尼加拉瓜", "91": "东帝汶", "92": "玻利维亚", "93": "哥斯达黎加", "94": "危地马拉",
    "95": "阿联酋", "96": "津巴布韦", "97": "波多黎各", "98": "苏丹", "99": "多哥",
    "100": "科威特", "101": "萨尔瓦多", "102": "利比亚", "103": "牙买加", "104": "特立尼达和多巴哥",
    "105": "厄瓜多尔", "106": "斯威士兰", "107": "阿曼", "108": "波黑", "109": "多米尼加",
    "110": "叙利亚", "111": "卡塔尔", "112": "巴拿马", "113": "古巴", "114": "毛里塔尼亚",
    "115": "塞拉利昂", "116": "约旦", "117": "葡萄牙", "118": "巴巴多斯", "119": "布隆迪",
    "120": "贝宁", "121": "文莱", "122": "巴哈马", "123": "博茨瓦纳", "124": "伯利兹",
    "125": "中非", "126": "多米尼克", "127": "格林纳达", "128": "格鲁吉亚", "129": "希腊",
    "130": "几内亚比绍", "131": "圭亚那", "132": "冰岛", "133": "科摩罗", "134": "利比里亚",
    "135": "莱索托", "136": "马拉维", "137": "纳米比亚", "138": "尼日尔", "139": "卢旺达",
    "140": "斯洛伐克", "141": "苏里南", "142": "塔吉克斯坦", "143": "摩纳哥", "144": "巴林",
    "145": "留尼汪岛", "146": "赞比亚", "147": "亚美尼亚", "148": "索马里", "149": "刚果(金)",
    "150": "智利", "151": "布基纳法索", "152": "黎巴嫩", "153": "加蓬", "154": "阿尔巴尼亚",
    "155": "乌拉圭", "156": "毛里求斯", "157": "不丹", "158": "马尔代夫", "159": "瓜德罗普岛",
    "160": "土库曼斯坦", "161": "法属圭亚那", "162": "芬兰", "163": "圣卢西亚", "164": "卢森堡",
    "165": "圣文森特", "166": "赤道几内亚", "167": "吉布提", "168": "安提瓜和巴布达", "169": "开曼群岛",
    "170": "黑山", "171": "丹麦", "172": "瑞士", "173": "挪威", "174": "澳大利亚",
    "175": "厄立特里亚", "176": "南苏丹", "177": "圣多美", "178": "阿鲁巴岛", "179": "蒙特塞拉特",
    "180": "安圭拉岛", "181": "北马其顿", "182": "塞舌尔", "183": "新喀里多尼亚", "184": "佛得角",
    "185": "美国(实体)", "186": "巴勒斯坦", "187": "美国", "188": "中国", "189": "韩国",
    "190": "科特迪瓦", "191": "日本",
}


def country_label(country_id) -> str:
    """返回 ``52 泰国`` 这样的展示标签。"""
    cid = str(country_id or "").strip()
    return f"{cid} {SMS_COUNTRY_NAMES_CN.get(cid, '')}".strip()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


_SMS_CLEANUP_LOCK = threading.Lock()


def _cleanup_queue_file() -> Path:
    cache_dir = Path(__file__).resolve().parents[1] / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / ".sms_activation_cleanup_queue.json"


def _load_cleanup_queue() -> list[dict]:
    try:
        raw = json.loads(_cleanup_queue_file().read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _save_cleanup_queue(items: list[dict]) -> None:
    path = _cleanup_queue_file()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)

def _activation_journal_file() -> Path:
    cache_dir = Path(__file__).resolve().parents[1] / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / ".sms_activation_journal.json"


def _load_activation_journal() -> dict[str, dict]:
    try:
        raw = json.loads(_activation_journal_file().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_activation_journal(items: dict[str, dict]) -> None:
    path = _activation_journal_file()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


# getStatusV2 的数字状态：8 = 已取消
SMS_STATUS_CANCELED = 8


def _status_token(text) -> str:
    """平台的纯文本状态本身就是一行短 token（``BAD_KEY`` 这种）。

    长得不像 token 的都是错误页正文之类的响应体，日志里不需要它们 —— 丢掉。
    """
    if isinstance(text, dict):
        text = text.get("message") or text.get("code") or ""
    if not isinstance(text, str):
        return ""
    token = " ".join(text.split())
    if not token or len(token) > 60 or "<" in token:
        return ""
    return token


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": _status_token(text)}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(f"{activation_id}:{code}".encode("utf-8")).hexdigest(),
    }


class SmsActivateProvider(BaseSmsProvider):
    """sms-activate 协议系 provider（SmsBower / HeroSMS 共用）。"""

    DEFAULT_BASE_URL = SMS_PROVIDERS["smsbower"]["base_url"]
    auto_report_success_on_code = False  # 等业务侧确认后才结算 activation

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        fixed_price: float = -1,
        proxy: Optional[str] = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        # 仅按 max_price 控制租号，固定价会要求恰好命中某个价位并无谓缩小库存。
        # 保留构造参数以兼容旧调用，但不再用于请求。
        self.fixed_price = -1.0
        self._proxy = (proxy or "").strip() or None
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        self.last_code_result: Optional[dict] = None
        self._used_codes: dict[str, set[str]] = {}
        self.current_activation: Optional[SmsActivation] = None
        self._warned_low_bid = False
        # Database-backed cleanup jobs are the only runtime source of truth.
        # Legacy JSON is imported once during application startup, never replayed
        # by individual provider instances.

    def _cleanup_identity(self) -> str:
        return _hash_secret(f"{self.base_url}|{self.api_key}")

    def _enqueue_cleanup(self, activation_id: str, action: str, reason: str) -> None:
        """Persist cleanup work in the database; no JSON queue is used at runtime."""
        now = _utcnow()
        with database_write_session() as session:
            activation = session.query(SmsActivationModel).filter_by(
                provider_identity=self._cleanup_identity(), activation_id=str(activation_id)
            ).first()
            if activation is None:
                # A provider response may have succeeded immediately before an old
                # process was upgraded. Keep a durable placeholder rather than
                # dropping its refund request.
                activation = SmsActivationModel(
                    provider_identity=self._cleanup_identity(), activation_id=str(activation_id),
                    state="refund_pending" if action == "cancel" else "finish_pending",
                    rented_at=now,
                    activation_deadline_at=now,
                    created_at=now, updated_at=now,
                )
                session.add(activation)
                session.flush()
            existing = session.query(SmsCleanupJobModel).filter_by(
                activation_db_id=activation.id, action=action
            ).first()
            if existing is None:
                session.add(SmsCleanupJobModel(
                    activation_db_id=activation.id, action=action, status="pending",
                    next_retry_at=now, reason=reason[:300], created_at=now, updated_at=now,
                ))
            else:
                existing.reason = reason[:300]
                existing.next_retry_at = min(existing.next_retry_at, now)
                existing.updated_at = now
                session.add(existing)
            session.commit()
        logger.warning("接码清理已进入数据库重试队列: activation_id=%s action=%s", activation_id, action)

    def _db_upsert_activation(self, activation: SmsActivation, state: str = "rented") -> None:
        """Persist before returning the phone: a child crash must not lose a billable activation."""
        now = _utcnow()
        identity = self._cleanup_identity()
        with database_write_session() as session:
            existing = session.query(SmsActivationModel).filter_by(
                provider_identity=identity, activation_id=str(activation.activation_id)
            ).first()
            if existing is None:
                session.add(SmsActivationModel(
                    provider_identity=identity, activation_id=str(activation.activation_id),
                    phone_number=activation.phone_number, country=activation.country, state=state,
                    rented_at=now,
                    activation_deadline_at=now + timedelta(seconds=SMS_ACTIVATION_MAX_LIFETIME_SECONDS),
                    created_at=now, updated_at=now,
                ))
                session.commit()

    def _db_mark_sms_sent(self, activation_id: str) -> bool:
        """Set the four-minute deadline once; duplicate callbacks must not extend it."""
        now = _utcnow()
        identity = self._cleanup_identity()
        with database_write_session() as session:
            row = session.query(SmsActivationModel).filter_by(
                provider_identity=identity, activation_id=str(activation_id)
            ).first()
            if row is None or row.state not in {"rented", "send_requested"} or row.sms_sent_at:
                return False
            row.state = "sms_sent"
            row.sms_sent_at = now
            row.code_deadline_at = now + timedelta(seconds=SMS_CODE_WAIT_AFTER_SEND_SECONDS)
            row.updated_at = now
            row.version += 1
            session.add(row)
            session.commit()
            return True

    def _db_mark_otp_received(self, activation_id: str) -> bool:
        now = _utcnow()
        with database_write_session() as session:
            row = session.query(SmsActivationModel).filter_by(
                provider_identity=self._cleanup_identity(), activation_id=str(activation_id)
            ).first()
            if row is None or row.state != "sms_sent":
                return False
            row.state, row.otp_received_at, row.updated_at, row.version = "otp_received", now, now, row.version + 1
            session.add(row)
            session.commit()
            return True

    def _db_complete(self, activation_id: str, state: str) -> None:
        now = _utcnow()
        with database_write_session() as session:
            row = session.query(SmsActivationModel).filter_by(
                provider_identity=self._cleanup_identity(), activation_id=str(activation_id)
            ).first()
            if row:
                row.state, row.updated_at, row.version = state, now, row.version + 1
                if state == "refunded": row.refunded_at = now
                if state == "finished": row.finished_at = now
                session.add(row); session.commit()
    def _track_activation(self, activation: SmsActivation, state: str = "rented") -> None:
        with _SMS_CLEANUP_LOCK:
            items = _load_activation_journal()
            items[str(activation.activation_id)] = {
                "provider": self._cleanup_identity(),
                "activation_id": str(activation.activation_id),
                "phone_number": activation.phone_number,
                "country": activation.country,
                "state": state,
                # Activation recovery needs an absolute wall-clock deadline after restart.
                "rented_at": time.time(),
                "sms_deadline_at": time.time() + SMS_WAIT_TIMEOUT_SECONDS,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            _save_activation_journal(items)

    def _set_activation_state(self, activation_id: str, state: str) -> None:
        with _SMS_CLEANUP_LOCK:
            items = _load_activation_journal()
            item = items.get(str(activation_id))
            if item:
                item["state"] = state
                item["updated_at"] = time.time()
                _save_activation_journal(items)

    def _forget_activation(self, activation_id: str) -> None:
        with _SMS_CLEANUP_LOCK:
            items = _load_activation_journal()
            if items.pop(str(activation_id), None) is not None:
                _save_activation_journal(items)

    def _recover_activation_journal(self) -> None:
        """重启后将未关闭 activation 转入 cancel/finish 重试队列。"""
        with _SMS_CLEANUP_LOCK:
            items = _load_activation_journal()
            ours = [item for item in items.values() if item.get("provider") == self._cleanup_identity()]
        for item in ours:
            state = str(item.get("state") or "rented")
            # otp_submitted 的请求可能已被 OpenAI 接受，宁可结算也不能错误退款。
            action = "finish" if state in {"otp_submitted", "phone_verified", "account_created"} else "cancel"
            self._enqueue_cleanup(str(item.get("activation_id") or ""), action, f"进程重启恢复: {state}")
            self._forget_activation(str(item.get("activation_id") or ""))

    def _retry_pending_cleanup(self) -> None:
        """Compatibility no-op. Cleanup retries are run by sms_refund_watchdog."""
        return None

    # ── HTTP ──

    def _request(self, params: dict, *, needs_key: bool = True, timeout=SMS_HTTP_TIMEOUT):
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        action = str(payload.get("action") or "")
        kind = "rent" if action in {"getNumber", "getNumberV2"} else "close" if action in {"cancelActivation", "finishActivation", "setStatus"} else "poll"
        # 只限制 API 请求本身；worker 等短信时不会占着限流槽位。
        with _sms_slot(kind):
            response = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
        response.raise_for_status()
        return response

    # ── 余额 / 价格 / 国家 ──

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"查询余额失败: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("getPrices 返回结构异常")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """按价格升序、库存降序返回国家列表。"""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 排名 API 不可用时退回 getPrices 自己算
        try:
            prices = self.get_prices(service=service_code)
        except Exception:
            return []
        rows = []
        for country_id, services in prices.items():
            if not isinstance(services, dict):
                continue
            entry = services.get(service_code)
            if not isinstance(entry, dict):
                continue
            price = _safe_float(entry.get("cost") or entry.get("price"), -1)
            count = _safe_int(entry.get("count") or entry.get("qty") or entry.get("available"), 0)
            if price >= 0 and count > 0:
                rows.append({"country": str(country_id), "price": price, "count": count})
        rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
        return rows

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data

        rows: list[dict] = []
        if isinstance(items, dict):
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = _safe_float(
                    value.get("price") or value.get("cost") or value.get("retail_price"), -1
                )
                count = _safe_int(value.get("count") or value.get("qty") or value.get("available"), 0)
                if price >= 0:
                    rows.append({"country": country_id, "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = (
                    item.get("country")
                    or item.get("countryId")
                    or item.get("country_id")
                    or item.get("id")
                )
                if country_id is None:
                    continue
                price = _safe_float(item.get("price") or item.get("cost"), -1)
                count = _safe_int(item.get("count") or item.get("qty") or item.get("available"), 0)
                if price >= 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
        return rows

    def get_best_country(
        self,
        service: Optional[str] = None,
        *,
        min_stock: int = 20,
        max_price: float = 0,
        strict_whitelist: bool = False,
        allowed_countries: Optional[list[str]] = None,
    ) -> Optional[str]:
        """自动选最优国家。

        ``allowed_countries`` 优先级最高（从这些国家里挑最便宜且库存足的）；
        其次 ``strict_whitelist`` 只从 ``OPENAI_SMS_COUNTRIES`` 里选；都没设
        就全平台自由选，由调用方承担"OpenAI 让用 WhatsApp"的风险。
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("查询国家排名失败: %s", exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                if allowed_set is not None:
                    if cid not in allowed_set:
                        continue
                elif strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    continue
                price = row.get("price") or 0
                count = row.get("count") or 0
                if count < stock_threshold:
                    continue
                if max_price > 0 and price > max_price:
                    continue
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    logger.warning(
                        "自动选中非 OpenAI-SMS 白名单国家 country=%s price=%s"
                        "（OpenAI 可能让此号走 WhatsApp 验证，收不到短信）",
                        cid,
                        price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # ── 租号 ──

    def _request_number(self, action: str, service: str, country: str) -> dict:
        """单次调用 getNumberV2 或 getNumber，失败原样抛给调用方的双重循环。"""
        params = {"action": action, "service": service, "country": country}
        # 唯一价格约束：服务端成交价不得超过用户设置的上限。
        if self.max_price > 0:
            params["maxPrice"] = self.max_price

        logger.info(
            "接码 %s: service=%s country=%s maxPrice=%s",
            action,
            service,
            country,
            params.get("maxPrice", "未设置"),
        )
        resp = self._request(params)
        resp_text = resp.text.strip()

        if action == "getNumberV2":
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                return data
            raise RuntimeError(resp_text[:200] or "空响应")

        if resp_text.startswith("ACCESS_NUMBER:"):
            parts = resp_text.split(":", 2)
            if len(parts) == 3:
                return {
                    "activationId": parts[1],
                    "phoneNumber": parts[2],
                    "countryPhoneCode": "",
                }
        raise RuntimeError(resp_text[:200] or "空响应")

    @staticmethod
    def _format_phone(info: dict) -> str:
        raw = str(info.get("phoneNumber") or "").strip()
        code = str(info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if code and raw.startswith(code):
            return f"+{raw}"
        if code:
            return f"+{code}{raw}"
        return f"+{raw}"

    def get_number(
        self,
        *,
        service: str,
        country: str = "",
        country_candidates: Optional[list[str]] = None,
    ) -> SmsActivation:
        """租号，按候选国家顺序依次尝试，每个国家先试 V2 再退 V1。"""
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        if not country_candidates:
            country_candidates = [
                str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()
            ]

        failures: list[str] = []
        last_exc: Optional[Exception] = None
        for cid in country_candidates:
            cid = str(cid).strip()
            if not cid:
                continue
            for action in ("getNumberV2", "getNumber"):
                try:
                    info = self._request_number(action, service_code, cid)
                except Exception as exc:
                    failures.append(f"{cid}: {action}={str(exc)[:120]}")
                    last_exc = exc
                    continue

                activation_id = str(info.get("activationId") or "")
                phone = self._format_phone(info)
                if not activation_id or not phone.strip("+"):
                    failures.append(f"{cid}: {action} 返回信息不完整")
                    continue

                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=cid,
                    metadata={},
                )
                self.current_activation = activation
                self._db_upsert_activation(activation, "rented")
                self._track_activation(activation, "rented")
                if len(country_candidates) > 1:
                    logger.info("在国家 %s 租到号 %s (action=%s)", cid, phone, action)
                return activation

        detail = " | ".join(failures) if failures else "未知"
        if "NO_NUMBERS" in detail:
            self._explain_no_numbers(service_code, country_candidates)
        raise RuntimeError(
            f"依次尝试 {len(country_candidates)} 个候选国家全部失败: {detail}"
        ) from last_exc

    def _explain_no_numbers(self, service: str, countries: list[str]) -> None:
        """NO_NUMBERS 多半不是没货，是出价太低 —— 把挂牌价查出来摆在日志里。

        平台按出价撮合，出价压在挂牌价以下时既租不到号，偶尔撮合成功的也是被反复
        回收的号段。只查一次价，别让每轮换号都多打一次接口。
        """
        bid = self.fixed_price if self.fixed_price > 0 else self.max_price
        if bid <= 0 or self._warned_low_bid:
            return
        self._warned_low_bid = True
        for country in countries:
            try:
                prices = self.get_prices(service, country) or {}
                market = _safe_float(
                    ((prices.get(str(country)) or {}).get(service) or {}).get("cost"), 0
                )
            except Exception:
                continue
            if market > 0 and bid < market:
                logger.warning(
                    "租不到号多半是出价太低: %s 的 %s 挂牌价 %.3f，当前出价只有 %.3f"
                    "（约挂牌价的 %d%%）；把接码设置里的固定价/最高价提到 %.2f 以上再试",
                    country_label(str(country)),
                    service,
                    market,
                    bid,
                    round(bid / market * 100),
                    market,
                )
                return

    # ── 等码 / 状态查询 ──

    def get_status(self, activation_id: str) -> dict:
        return _parse_sms_status_text(
            self._request({"action": "getStatus", "id": activation_id}).text
        )

    def get_status_v2(self, activation_id: str) -> dict:
        # 少了 type=sms 平台只回 {"error":"Bad type parameter"}，而这个错解析下来
        # 长得和"还在等码"一模一样 —— 等于白等一整个窗口还以为一切正常。
        resp = self._request({"action": "getStatusV2", "id": activation_id, "type": "sms"})
        raw_text = (resp.text or "").strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_sms_status_text(raw_text)

        if isinstance(data, str):
            return _parse_sms_status_text(data)
        # raw 只放平台自己写在字段里的那句话（error / status_description），
        # 响应体原文不进这个 dict —— 它最后是要被打进任务日志的。
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": ""}

        if data.get("error"):
            return {"status": "unknown", "raw": _status_token(data.get("error"))}

        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_sms_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed

        candidate = _make_sms_candidate(activation_id, "getStatusV2.code", data.get("code"))
        if candidate:
            return candidate
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id, f"getStatusV2.{channel}", item.get("code")
                )
                if candidate:
                    return candidate

        description = str(data.get("status_description") or "").strip()
        if _safe_int(raw_status, -1) == SMS_STATUS_CANCELED:
            return {"status": "cancel", "raw": description}
        return {"status": "wait_code", "raw": description}

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int = 80,
        poll: int = 3,
    ) -> Optional[dict]:
        """等短信验证码：只轮询接码平台，不去催发。

        码是 OpenAI 那边一次性发出来的，催发既换不来第二条短信，还会把当前这条
        challenge 弄失效。超时返回 None，由上层 cancel 换号。
        """
        start = time.time()
        deadline = start + timeout
        used_codes = self._used_codes.setdefault(str(activation_id), set())

        last_seen = ""
        next_report = start + 30

        while time.time() < deadline:
            seen_this_round = []
            for source in ("v2", "v1"):
                try:
                    result = (
                        self.get_status_v2(activation_id)
                        if source == "v2"
                        else self.get_status(activation_id)
                    )
                except Exception as exc:
                    logger.debug("查询接码状态 %s 失败: %s", source, exc)
                    continue
                seen_this_round.append(self._describe_status(source, result))
                last_seen = " | ".join(seen_this_round)
                if result.get("status") == "cancel":
                    logger.info("平台报告号码已取消: %s", last_seen)
                    return None
                if result.get("status") == "ok":
                    code = str(result.get("code") or "")
                    if code and code not in used_codes:
                        self._db_mark_otp_received(activation_id)
                        self._set_activation_state(activation_id, "otp_received")
                        return {
                            "status": "ok",
                            "code": code,
                            "sms_key": result.get("sms_key") or "",
                        }

            # 「一直没码」到底是平台没收到短信、还是号被取消了，光看超时看不出来
            if time.time() >= next_report:
                logger.info(
                    "等码中 (已等 %ds/%ds)：平台状态 %s",
                    int(time.time() - start),
                    timeout,
                    last_seen or "(未取到)",
                )
                next_report = time.time() + 30

            time.sleep(poll)

        logger.warning(
            "等码 %ds 结束，平台始终没收到短信：最后状态 %s", timeout, last_seen or "(未取到)"
        )
        return None

    @staticmethod
    def _describe_status(source: str, result: dict) -> str:
        """只报状态和平台给的那句说明；短信正文不进日志。"""
        status = str(result.get("status") or "")
        detail = str(result.get("raw") or "").strip()
        parts = [f"{source}={status}"]
        if detail:
            parts.append(detail[:80])
        return " ".join(parts)

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        # 传进来的 timeout 就是真 timeout：号码有 20 分钟生命周期，但 OpenAI 那边的
        # phone-otp challenge 等不了那么久，超时就该让上层换号。
        candidate = self.wait_for_code(activation_id, timeout=timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # ── 状态报告 ──

    def cancel(self, activation_id: str) -> bool:
        ok = False
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            ok = resp.status_code == 204 or "ACCESS_CANCEL" in resp.text
        except Exception:
            pass
        if not ok:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
                ok = "ACCESS_CANCEL" in resp.text
            except Exception:
                pass
        self._used_codes.pop(str(activation_id), None)
        if ok:
            self._db_complete(activation_id, "refunded")
            self._forget_activation(activation_id)
        else:
            self._enqueue_cleanup(activation_id, "cancel", "取消未使用号码")
        return ok

    def report_success(self, activation_id: str) -> bool:
        self._set_activation_state(activation_id, "phone_verified")
        self._used_codes.pop(str(activation_id), None)
        return self._finish_activation(activation_id, reason="手机号验证成功")

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        if self.last_code_result and self.last_code_result.get("code"):
            self._used_codes.setdefault(str(activation_id), set()).add(
                str(self.last_code_result["code"])
            )

    def mark_send_succeeded(self, activation_id: str) -> None:
        self._set_activation_state(activation_id, "sms_sent")
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass

    def complete_activation(self, activation_id: str, reason: str = "") -> None:
        self._set_activation_state(activation_id, "account_created")
        self._used_codes.pop(str(activation_id), None)
        self._finish_activation(activation_id, reason=reason or "手机号已被业务侧占用")

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # 业务侧拒了这个号 → cancel 退款，号根本没用上，不能白花钱
        self.cancel(activation_id)


def cancel_expired_sms_activations(config: dict, *, proxy: Optional[str] = None) -> int:
    """Compatibility entry point for parent task watchdogs.

    Deadline ownership moved to the database scheduler.  Keeping this function
    lets existing callers trigger an immediate *database* scan without reading
    the retired JSON journal or making unbounded cancel calls in task threads.
    """
    settings = dict(config or {})
    if proxy and not settings.get("sms_proxy"):
        settings["sms_proxy"] = proxy
    try:
        from services.sms_refund_watchdog import schedule_due_refunds
        return schedule_due_refunds(settings)
    except Exception:
        logger.warning("扫描超时接码 activation 失败", exc_info=True)
        return 0


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    ``provider_key`` 取 ``smsbower`` / ``herosms``；配置字段见
    ``api/config.py`` 里的 ``sms_*`` 系列。
    """
    key = (provider_key or "").lower().strip().replace("_", "")
    meta = SMS_PROVIDERS.get(key)
    if meta is None:
        raise RuntimeError(f"未知接码服务: {provider_key}")

    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{meta['label']} 未配置 API Key")

    return SmsActivateProvider(
        api_key=api_key,
        base_url=meta["base_url"],
        default_service=str(config.get("sms_service") or "").strip() or SMS_DEFAULT_SERVICE,
        default_country=str(config.get("sms_country") or "").strip() or SMS_DEFAULT_COUNTRY,
        max_price=_safe_float(config.get("sms_max_price"), -1),
        fixed_price=_safe_float(config.get("sms_fixed_price"), -1),
        proxy=(str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None,
    )


class PhoneCallbackController:
    """把接码 provider 包装成两阶段回调，注入 ``AuthFlow`` 的 add-phone 流程。"""

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False
        self._warned_off_whitelist = False
        self._activation_rented_monotonic: Optional[float] = None

    def _ensure_provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def get_phone(self) -> str:
        """阶段 1：租手机号（返回带 + 的 E.164）。"""
        provider = self._ensure_provider()
        candidates = self._resolve_country_candidates(provider)
        preview = ",".join(
            f"{c}({SMS_COUNTRY_NAMES_CN.get(c, '?')})" for c in candidates[:5]
        )
        self.log(
            f"准备租号: provider={self.provider_key} service={self.service} "
            f"候选={preview}{' …' if len(candidates) > 5 else ''}"
        )

        try:
            self.activation = provider.get_number(
                service=self.service,
                country=candidates[0],
                country_candidates=candidates,
            )
        except Exception:
            self._release_lock()
            raise

        # Deadline starts when the provider has actually assigned an activation, not when
        # the OpenAI flow later begins polling it.
        self._activation_rented_monotonic = time.monotonic()
        used_country = self.activation.country or candidates[0]
        self.log(
            f"已租到号码: {self.activation.phone_number} "
            f"国家={country_label(used_country)} "
            f"(activation_id={self.activation.activation_id})"
        )
        # 白名单外的号段，OpenAI 常把验证改走 WhatsApp，接码平台就永远等不到短信。
        # 这时"发送成功但收不到码"看起来像平台的问题，其实是选错了国家。
        if used_country not in OPENAI_SMS_COUNTRIES and not self._warned_off_whitelist:
            self._warned_off_whitelist = True
            whitelist = "、".join(country_label(c) for c in sorted(OPENAI_SMS_COUNTRIES))
            self.log(
                f"提醒: {country_label(used_country)} 不在 OpenAI 纯短信白名单（{whitelist}）；"
                "这些号段 OpenAI 可能改用 WhatsApp 发码，会出现"
                "「发送成功但一直等不到短信」（白名单内的号也只是概率更高，不保证收得到）"
            )
        return self.activation.phone_number

    def _resolve_country_candidates(self, provider: BaseSmsProvider) -> list[str]:
        """只按价格上限从全平台当前有库存的国家中自动租号。"""
        if not isinstance(provider, SmsActivateProvider):
            return [self.country or SMS_DEFAULT_COUNTRY]
        if not self.auto_select_country:
            return [self.country or SMS_DEFAULT_COUNTRY]

        price_cap = _safe_float(
            self.config.get("sms_max_price") or self.config.get("sms_auto_max_price"),
            0,
        )
        self.log(
            "自动选号: 全平台按价格升序尝试"
            f"（价格上限 {price_cap if price_cap > 0 else '不限'}）"
        )
        try:
            rows = provider.get_top_countries(service=self.service)
        except Exception as exc:
            self.log(f"国家价格/库存查询失败（{exc}），无法自动租号")
            raise RuntimeError("无法获取接码国家价格与库存") from exc

        candidates: list[str] = []
        for row in rows:
            country = str(row.get("country") or "").strip()
            price = _safe_float(row.get("price"), -1)
            stock = _safe_int(row.get("count"), 0)
            if not country or price < 0 or stock <= 0:
                continue
            if price_cap > 0 and price > price_cap:
                continue
            candidates.append(country)
        if not candidates:
            if price_cap > 0:
                raise RuntimeError(f"没有单价不超过 {price_cap} 且当前有库存的可用国家")
            raise RuntimeError("当前服务暂无可用库存")
        preview = ",".join(f"{country_label(c)}" for c in candidates[:8])
        self.log(f"自动选号候选: {preview}{' …' if len(candidates) > 8 else ''}")
        return candidates

    def get_code(self, timeout: int = 180) -> str:
        """阶段 2：等待短信验证码。"""
        if not self.activation:
            raise RuntimeError("尚未租号，无法等待验证码")
        provider = self._ensure_provider()
        rented_at = self._activation_rented_monotonic
        remaining = SMS_WAIT_TIMEOUT_SECONDS if rented_at is None else max(
            0, int(SMS_WAIT_TIMEOUT_SECONDS - (time.monotonic() - rented_at))
        )
        effective_timeout = min(max(0, int(timeout)), remaining)
        self.log(
            f"等待短信验证码…(activation_id={self.activation.activation_id} timeout={effective_timeout}s，"
            f"租号后最多 {SMS_WAIT_TIMEOUT_SECONDS}s)"
        )
        code = provider.get_code(self.activation.activation_id, timeout=effective_timeout) if effective_timeout else ""
        if code:
            self.log(f"收到短信验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            elapsed = 0 if rented_at is None else time.monotonic() - rented_at
            if elapsed >= SMS_WAIT_TIMEOUT_SECONDS:
                activation_id = self.activation.activation_id
                self.log(
                    f"等待短信超过 {SMS_WAIT_TIMEOUT_SECONDS}s，自动取消并申请退款: "
                    f"activation_id={activation_id}"
                )
                # cancel() 自带持久化补偿队列；失败也绝不继续占着当前号码等待。
                try:
                    provider.cancel(activation_id)
                finally:
                    self.activation = None
                    self._activation_rented_monotonic = None
            else:
                self.log(f"未收到短信验证码: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as exc:
                logger.warning("上报号码成功失败: %s", exc)
            self.completed = True
            self.log(f"号码已标记完成: activation_id={self.activation.activation_id}")
        self._release_lock()

    def mark_otp_submitted(self) -> None:
        if self.activation and self.provider:
            setter = getattr(self.provider, "_set_activation_state", None)
            if callable(setter):
                setter(self.activation.activation_id, "otp_submitted")

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "") -> None:
        """OpenAI 明确未接受号码时取消 activation，并避免 cleanup 二次退号。"""
        if self.activation and self.provider:
            activation_id = self.activation.activation_id
            try:
                self.provider.mark_send_failed(activation_id, reason=reason)
            except Exception:
                logger.warning("取消不可用号码 activation 失败: %s", activation_id, exc_info=True)
            finally:
                # 无论平台请求是否抛异常，本次 activation 都不能再次使用；后续 cleanup
                # 也不再对同一个 activation 发送第二次 cancel。
                self.activation = None
                self._activation_rented_monotonic = None
                self._release_lock()

    def complete_activation(self, reason: str = "") -> None:
        """号码已经被 OpenAI 接受：结束结算并阻止 finally 再执行 cancel。"""
        if self.activation and self.provider:
            try:
                self.provider.complete_activation(self.activation.activation_id, reason=reason)
            except Exception:
                logger.warning("结束已使用号码 activation 失败", exc_info=True)
            self.completed = True
        self._release_lock()

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号并解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
            self.activation = None
            self._activation_rented_monotonic = None
        self._release_lock()

    def _release_lock(self) -> None:
        # 高并发模式不再持有全局手机验证锁；保留方法兼容既有调用点。
        self._verify_lock_acquired = False


def resolve_sms_settings(extra_config: Optional[dict] = None) -> dict:
    """把全局配置里的 ``sms_*`` 项和本次任务的覆盖合并成一份接码配置。"""
    from core.config_store import config_store

    settings = {
        key: value
        for key, value in (config_store.get_all() or {}).items()
        if key.startswith("sms_")
    }
    for key, value in (extra_config or {}).items():
        if key.startswith("sms_") and value not in (None, ""):
            settings[key] = value
    return settings


def build_phone_callback(
    settings: dict,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    proxy: Optional[str] = None,
) -> Optional[PhoneCallbackController]:
    """按配置构造接码控制器；未启用或缺 API Key 时返回 None。

    返回 None 意味着注册链路命中 add-phone 时会回退到手工号码路径
    （``OPENAI_PHONE_NUMBER`` 系列），行为跟没接接码时一致。
    """
    settings = dict(settings or {})
    if not _safe_bool(settings.get("sms_enabled"), False):
        return None

    if not str(settings.get("sms_api_key") or "").strip():
        logger.warning("已启用接码但未配置 sms_api_key，跳过接码")
        return None

    if proxy and not str(settings.get("sms_proxy") or "").strip():
        settings["sms_proxy"] = proxy

    # 服务码在租号和查国家排名两处都要用，必须同一个值：平台按服务码分库存，
    # 拿 "openai" 这种人类可读名去查排名只会得到空表。OpenAI 对应的是 dr。
    try:
        return PhoneCallbackController(
            provider_key=str(settings.get("sms_provider") or "smsbower"),
            config=settings,
            service=str(settings.get("sms_service") or "").strip() or SMS_DEFAULT_SERVICE,
            # 始终根据库存和价格自动选择国家；不再读取 sms_country / 白名单。
            country="",
            log_fn=log_fn,
            auto_select_country=True,
        )
    except Exception as exc:
        logger.warning("创建接码控制器失败: %s", exc)
        return None
