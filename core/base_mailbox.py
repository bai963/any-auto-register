from __future__ import annotations

"""邮箱池基类 - 抽象临时邮箱/收件服务"""

import json
import os
import random
import socket
import threading
import time
from urllib.parse import urlsplit, unquote

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any, Callable
from .proxy_utils import build_requests_proxy_config
from .mailbox_utils import (
    decode_mail_content,
    extract_verification_code,
    extract_yyds_verification_code,
    polling_wait,
)


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
    ) -> str:
        return polling_wait(
            timeout=timeout,
            poll_interval=poll_interval,
            poll_once=poll_once,
            checkpoint=self._checkpoint,
            sleep=self._sleep_with_checkpoint,
            timeout_message=timeout_message,
        )

    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """Backward-compatible provider facade for shared extraction logic."""
        return extract_verification_code(text, pattern)

    def _decode_raw_content(self, raw: str) -> str:
        """Backward-compatible provider facade for shared decoding logic."""
        return decode_mail_content(raw)

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...
    def _yyds_safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """Legacy alias retained while providers migrate to mailbox_utils."""
        return extract_yyds_verification_code(text, pattern)

    def _yyds_decode_raw_content(self, raw: str) -> str:
        """Legacy alias retained while providers migrate to mailbox_utils."""
        return decode_mail_content(raw, preserve_parsed_body=True)

def create_mailbox(
    provider: str, extra: dict = None, proxy: str = None
) -> "BaseMailbox":
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    extra = extra or {}
    if provider == "tempmail_lol":
        return TempMailLolMailbox(proxy=proxy)
    elif provider == "skymail":
        return SkyMailMailbox(
            api_base=extra.get("skymail_api_base", "https://api.skymail.ink"),
            auth_token=extra.get("skymail_token", ""),
            domain=extra.get("skymail_domain", ""),
            proxy=proxy,
        )
    elif provider == "cloudmail":
        timeout_raw = extra.get("cloudmail_timeout", extra.get("timeout", 30))
        try:
            timeout_value = int(timeout_raw)
        except (TypeError, ValueError):
            timeout_value = 30
        return CloudMailMailbox(
            api_base=extra.get("cloudmail_api_base")
            or extra.get("base_url")
            or "",
            admin_email=extra.get("cloudmail_admin_email")
            or extra.get("admin_email")
            or "",
            admin_password=extra.get("cloudmail_admin_password")
            or extra.get("admin_password")
            or extra.get("api_key")
            or "",
            domain=extra.get("cloudmail_domain") or extra.get("domain") or "",
            subdomain=extra.get("cloudmail_subdomain")
            or extra.get("subdomain")
            or "",
            timeout=timeout_value,
            proxy=proxy,
        )
    elif provider == "duckmail":
        return DuckMailMailbox(
            api_url=(extra.get("duckmail_api_url") or "https://www.duckmail.sbs"),
            provider_url=(
                extra.get("duckmail_provider_url") or "https://api.duckmail.sbs"
            ),
            bearer=(extra.get("duckmail_bearer") or "kevin273945"),
            domain=extra.get("duckmail_domain", ""),
            api_key=extra.get("duckmail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "freemail":
        return FreemailMailbox(
            api_url=extra.get("freemail_api_url", ""),
            admin_token=extra.get("freemail_admin_token", ""),
            username=extra.get("freemail_username", ""),
            password=extra.get("freemail_password", ""),
            domain=extra.get("freemail_domain", ""),
            proxy=proxy,
        )
    elif provider == "moemail":
        return MoeMailMailbox(
            api_url=extra.get("moemail_api_url", "https://sall.cc"),
            api_key=extra.get("moemail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "maliapi":
        return MaliAPIMailbox(
            api_url=extra.get("maliapi_base_url", "https://maliapi.215.im/v1"),
            api_key=extra.get("maliapi_api_key", ""),
            domain=extra.get("maliapi_domain", ""),
            auto_domain_strategy=extra.get("maliapi_auto_domain_strategy", ""),
            proxy=proxy,
        )
    elif provider == "gptmail":
        return GPTMailMailbox(
            api_url=extra.get("gptmail_base_url", "https://mail.chatgpt.org.uk"),
            api_key=extra.get("gptmail_api_key", ""),
            domain=extra.get("gptmail_domain", ""),
            proxy=proxy,
        )
    elif provider == "applemail":
        return AppleMailMailbox(
            api_url=extra.get("applemail_base_url", "https://www.appleemail.top"),
            pool_file=extra.get("applemail_pool_file", ""),
            pool_dir=extra.get("applemail_pool_dir", "mail"),
            mailboxes=extra.get("applemail_mailboxes", "INBOX,Junk"),
            proxy=proxy,
        )
    elif provider == "opentrashmail":
        return OpenTrashMailMailbox(
            api_url=extra.get("opentrashmail_api_url", ""),
            domain=extra.get("opentrashmail_domain", ""),
            password=extra.get("opentrashmail_password", ""),
            proxy=proxy,
        )
    elif provider == "cfworker":
        return CFWorkerMailbox(
            api_url=extra.get("cfworker_api_url", ""),
            admin_token=extra.get("cfworker_admin_token", ""),
            domain=extra.get("cfworker_domain", ""),
            domain_override=extra.get("cfworker_domain_override", ""),
            domains=extra.get("cfworker_domains", ""),
            enabled_domains=extra.get("cfworker_enabled_domains", ""),
            subdomain=extra.get("cfworker_subdomain", ""),
            domain_level_count=extra.get("email_domain_level_count", 2),
            random_subdomain=extra.get("cfworker_random_subdomain", False),
            random_name_subdomain=extra.get("cfworker_random_name_subdomain", False),
            fingerprint=extra.get("cfworker_fingerprint", ""),
            custom_auth=extra.get("cfworker_custom_auth", ""),
            proxy=proxy,
        )
    elif provider == "luckmail":
        return LuckMailMailbox(
            base_url=extra.get("luckmail_base_url") or "https://mails.luckyous.com/",
            api_key=extra.get("luckmail_api_key", ""),
            project_code=extra.get("luckmail_project_code", ""),
            email_type=extra.get("luckmail_email_type", ""),
            domain=extra.get("luckmail_domain", ""),
            proxy=proxy,
        )
    elif provider in {"outlook", "microsoft"}:
        return OutlookMailbox(
            imap_server=extra.get("outlook_imap_server", ""),
            imap_port=extra.get("outlook_imap_port", ""),
            token_endpoint=extra.get("outlook_token_endpoint", ""),
            backend=extra.get("outlook_backend", ""),
            graph_api_base=extra.get("outlook_graph_api_base", ""),
            mail_import_source=extra.get("mail_import_source", ""),
            proxy=proxy,
        )
    else:  # laoudo
        return LaoudoMailbox(
            auth_token=extra.get("laoudo_auth", ""),
            email=extra.get("laoudo_email", ""),
            account_id=extra.get("laoudo_account_id", ""),
        )


class AppleMailMailbox(BaseMailbox):
    """小苹果取件邮箱服务，基于本地邮箱池文件轮转邮箱账号"""

    def __init__(
        self,
        api_url: str = "https://www.appleemail.top",
        pool_file: str = "",
        pool_dir: str = "mail",
        mailboxes: str = "INBOX,Junk",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.appleemail.top").rstrip("/")
        self.pool_file = str(pool_file or "").strip()
        self.pool_dir = str(pool_dir or "mail").strip() or "mail"
        self.mailboxes = self._normalize_mailboxes(mailboxes)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._selected_record = None
        self._selected_pool_path = None

    @staticmethod
    def _normalize_mailboxes(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            items = [str(item or "").strip() for item in value]
        else:
            raw = str(value or "INBOX,Junk").strip() or "INBOX,Junk"
            items = [item.strip() for item in raw.split(",")]

        result = []
        seen = set()
        for item in items:
            if not item:
                continue
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result or ["INBOX", "Junk"]

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=payload,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            data = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"AppleMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(data, dict):
                message = (
                    data.get("detail")
                    or data.get("message")
                    or data.get("error")
                    or response.text
                )
            else:
                message = response.text
            raise RuntimeError(
                f"AppleMail API {path} 失败: {str(message or f'HTTP {response.status_code}').strip()}"
            )

        if isinstance(data, dict) and data.get("success") is False:
            message = (
                data.get("message")
                or data.get("detail")
                or data.get("error")
                or "unknown error"
            )
            raise RuntimeError(f"AppleMail API {path} 失败: {str(message).strip()}")

        return data

    @staticmethod
    def _unwrap_message_payload(payload: Any) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "result", "results", "messages", "mails", "emails", "items", "list"):
                if key in payload:
                    nested = AppleMailMailbox._unwrap_message_payload(payload.get(key))
                    if nested:
                        return nested
            if any(
                key in payload
                for key in (
                    "id",
                    "message_id",
                    "uid",
                    "mail_id",
                    "subject",
                    "content",
                    "text",
                    "html",
                    "body",
                    "preview",
                    "verification_code",
                    "code",
                    "otp",
                )
            ):
                return [payload]

            collected = []
            for value in payload.values():
                collected.extend(AppleMailMailbox._unwrap_message_payload(value))
            return collected
        return []

    @staticmethod
    def _resolve_message_id(message: dict[str, Any], mailbox: str) -> str:
        import hashlib

        for key in ("id", "message_id", "uid", "mail_id", "mid", "_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return value

        raw = json.dumps(message, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(f"{mailbox}:{raw}".encode("utf-8")).hexdigest()
        return f"{mailbox}:{digest}"

    def _build_search_text(self, message: dict[str, Any]) -> str:
        parts = []
        for key in (
            "subject",
            "from",
            "from_address",
            "sender",
            "preview",
            "text",
            "content",
            "body",
            "html",
            "html_content",
            "raw",
            "raw_content",
            "mail_text",
        ):
            value = message.get(key)
            if value:
                parts.append(str(value))

        if not parts:
            parts.append(json.dumps(message, ensure_ascii=False))

        text = " ".join(parts).strip()
        return self._decode_raw_content(text) or text

    def _extract_code_from_message(
        self,
        message: dict[str, Any],
        code_pattern: str = None,
    ) -> Optional[str]:
        for key in ("verification_code", "code", "otp", "captcha", "verify_code"):
            value = str(message.get(key) or "").strip()
            if value:
                code = self._safe_extract(value, code_pattern)
                if code:
                    return code
        return self._safe_extract(self._build_search_text(message), code_pattern)

    def _resolve_mailboxes_for_account(self, account: MailboxAccount) -> list[str]:
        account_mailbox = ""
        if isinstance(account.extra, dict):
            account_mailbox = str(account.extra.get("mailbox") or "").strip()

        result = []
        seen = set()
        for mailbox in ([account_mailbox] if account_mailbox else []) + list(self.mailboxes):
            name = str(mailbox or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(name)
        return result or ["INBOX"]

    def _build_request_payload(self, account: MailboxAccount, mailbox: str) -> dict[str, Any]:
        extra = account.extra or {}
        refresh_token = str(extra.get("refresh_token") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        if not refresh_token or not client_id:
            raise RuntimeError("AppleMail 邮箱记录缺少 refresh_token 或 client_id")

        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "email": account.email,
            "mailbox": mailbox,
        }

    def _list_messages(self, account: MailboxAccount, mailbox: str) -> list[dict[str, Any]]:
        data = self._request_json(
            "GET",
            "/api/mail-all",
            payload=self._build_request_payload(account, mailbox),
            timeout=15,
        )
        if isinstance(data, dict):
            new_refresh_token = str(data.get("new_refresh_token") or "").strip()
            if new_refresh_token:
                if account.extra is None:
                    account.extra = {}
                account.extra["refresh_token"] = new_refresh_token
        return self._unwrap_message_payload(data)

    def get_email(self) -> MailboxAccount:
        from .applemail_pool import take_next_applemail_record

        pool_path, record = take_next_applemail_record(
            pool_file=self.pool_file,
            pool_dir=self.pool_dir,
        )
        self._selected_pool_path = pool_path
        self._selected_record = record
        self._email = record["email"]
        self._log(f"[AppleMail] 使用邮箱池: {pool_path.name}")
        self._log(f"[AppleMail] 分配邮箱: {record['email']}")
        return MailboxAccount(
            email=record["email"],
            account_id=record["email"],
            extra={
                "provider": "applemail",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
                "mailbox": record.get("mailbox") or "INBOX",
                "pool_file": pool_path.name,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        ids = set()
        for mailbox in self._resolve_mailboxes_for_account(account):
            try:
                messages = self._list_messages(account, mailbox)
            except Exception:
                continue
            ids.update(
                self._resolve_message_id(message, mailbox)
                for message in messages
            )
        return ids

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            for mailbox in self._resolve_mailboxes_for_account(account):
                try:
                    messages = self._list_messages(account, mailbox)
                except Exception:
                    continue

                for message in messages:
                    message_id = self._resolve_message_id(message, mailbox)
                    if message_id in seen:
                        continue
                    seen.add(message_id)

                    search_text = self._build_search_text(message)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._extract_code_from_message(message, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[AppleMail] {mailbox} 收到验证码: {code}")
                        return code
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""

    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError(
                "Laoudo 邮箱未配置或已失效，请检查 laoudo_auth、laoudo_email、laoudo_account_id 配置，"
                "或切换到 tempmail_lol（无需配置）"
            )
        return MailboxAccount(email=self._email, account_id=self._account_id)

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests

        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={
                    "accountId": account.account_id,
                    "allReceive": 0,
                    "emailId": 0,
                    "timeSort": 1,
                    "size": 50,
                    "type": 0,
                },
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15,
                impersonate="chrome131",
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {
                    m.get("id") or m.get("emailId")
                    for m in mails
                    if m.get("id") or m.get("emailId")
                }
        except Exception:
            pass
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from curl_cffi import requests as curl_requests

        seen = set(before_ids) if before_ids else set()
        h = {"authorization": self.auth, "user-agent": self._ua}

        def poll_once() -> Optional[str]:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={
                        "accountId": account.account_id,
                        "allReceive": 0,
                        "emailId": 0,
                        "timeSort": 1,
                        "size": 50,
                        "type": 0,
                    },
                    headers=h,
                    timeout=15,
                    impersonate="chrome131",
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (
                            str(mail.get("subject", ""))
                            + " "
                            + str(mail.get("content") or mail.get("html") or "")
                        )
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=4,
            poll_once=poll_once,
        )


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""

    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/emails", params={"email": account.email}, timeout=10
            )
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids) if before_ids else set()
        last_check = None

        def poll_once() -> Optional[str]:
            nonlocal last_check
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(
                        f"{self.api}/emails",
                        params={"email": account.email},
                        timeout=10,
                    )
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests

        r = requests.post(
            f"{self.api}/inbox/create", json={}, proxies=self.proxy, timeout=15
        )
        data = r.json()
        email = data.get("address") or data.get("email", "")
        if not email:
            raise RuntimeError(f"tempmail.lol API 返回空邮箱: {data}")
        self._email = email
        self._token = data.get("token", "")
        print(f"[TempMailLol] 生成邮箱: {self._email}")
        return MailboxAccount(email=self._email, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy,
                timeout=10,
            )
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")

        def poll_once() -> Optional[str]:
            try:
                r = requests.get(
                    f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy,
                    timeout=10,
                )
                for mail in sorted(
                    r.json().get("emails", []),
                    key=lambda x: x.get("date", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    if otp_sent_at and mail.get("date", 0) / 1000 < otp_sent_at:
                        continue
                    seen.add(mid)
                    text = (
                        mail.get("subject", "")
                        + " "
                        + mail.get("body", "")
                        + " "
                        + mail.get("html", "")
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class SkyMailMailbox(BaseMailbox):
    """SkyMail / CloudMail 自建邮箱服务"""

    def __init__(self, api_base: str, auth_token: str, domain: str, proxy: str = None):
        self.api = (api_base or "").rstrip("/")
        self.auth_token = auth_token or ""
        self.domain = domain or ""
        self.proxy = build_requests_proxy_config(proxy)

    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": self.auth_token,
        }

    def _ensure_config(self) -> None:
        if not self.api or not self.auth_token or not self.domain:
            raise RuntimeError(
                "SkyMail 未配置完整：请设置 skymail_api_base、skymail_token、skymail_domain"
            )

    def _gen_prefix(self) -> str:
        import random
        import string

        length = random.randint(8, 13)
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def get_email(self) -> MailboxAccount:
        import requests

        self._ensure_config()
        email = f"{self._gen_prefix()}@{self.domain}"
        payload = {"list": [{"email": email}]}
        r = requests.post(
            f"{self.api}/api/public/addUser",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {r.status_code} {r.text[:200]}")

        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {data}")

        self._log(f"[SkyMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _list_mails(self, email: str) -> list:
        import requests

        payload = {
            "toEmail": email,
            "num": 1,
            "size": 20,
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_mails(account.account_id or account.email)
            ids = set()
            for i, msg in enumerate(mails):
                mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                if mid:
                    ids.add(str(mid))
                else:
                    digest = (
                        str(msg.get("date") or msg.get("time") or "")
                        + "|"
                        + str(msg.get("subject") or "")
                    )
                    ids.add(f"idx-{i}-{digest}")
            return ids
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for i, msg in enumerate(mails):
                    mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                    if not mid:
                        digest = (
                            str(msg.get("date") or msg.get("time") or "")
                            + "|"
                            + str(msg.get("subject") or "")
                        )
                        mid = f"idx-{i}-{digest}"
                    mid = str(mid)
                    if mid in seen:
                        continue
                    seen.add(mid)

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue

                    code = self._safe_extract(content, code_pattern)
                    if code:
                        self._log(f"[SkyMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CloudMailMailbox(BaseMailbox):
    """CloudMail 自建邮箱服务（genToken + emailList）"""

    _token_lock = threading.Lock()
    _token_cache: dict[str, tuple[str, float]] = {}
    _seen_ids_lock = threading.Lock()
    _seen_ids: dict[str, set[str]] = {}

    def __init__(
        self,
        api_base: str,
        admin_email: str,
        admin_password: str,
        domain: Any = "",
        subdomain: str = "",
        timeout: int = 30,
        proxy: str = None,
    ):
        self.api = str(api_base or "").rstrip("/")
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.domain = domain
        self.subdomain = str(subdomain or "").strip()
        self.timeout = max(int(timeout or 30), 5)
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _extract_domain_from_url(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or ""))
        host = (parsed.netloc or parsed.path.split("/")[0] or "").strip()
        if ":" in host:
            host = host.split(":", 1)[0].strip()
        return host

    @staticmethod
    def _normalize_domain(value: str) -> str:
        domain = str(value or "").strip().lstrip("@")
        if "://" in domain:
            domain = CloudMailMailbox._extract_domain_from_url(domain)
        return domain.strip()

    def _domain_candidates(self) -> list[str]:
        candidates: list[str] = []

        if isinstance(self.domain, (list, tuple, set)):
            iterable = self.domain
        else:
            raw = str(self.domain or "").strip()
            parsed = None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
            if isinstance(parsed, list):
                iterable = parsed
            elif raw:
                normalized = (
                    raw.replace(";", "\n")
                    .replace(",", "\n")
                    .replace("|", "\n")
                    .splitlines()
                )
                iterable = [item for item in normalized if item]
            else:
                iterable = []

        for item in iterable:
            normalized = self._normalize_domain(item)
            if normalized:
                candidates.append(normalized)

        if not candidates:
            inferred = self._normalize_domain(self._extract_domain_from_url(self.api))
            if inferred:
                candidates.append(inferred)
        return candidates

    def _resolve_admin_email(self) -> str:
        if self.admin_email:
            return self.admin_email
        domains = self._domain_candidates()
        if domains:
            return f"admin@{domains[0]}"
        return "admin@example.com"

    def _cache_key(self) -> str:
        return f"{self.api}|{self._resolve_admin_email()}|{self.admin_password}"

    def _ensure_config(self) -> None:
        if not self.api or not self.admin_password:
            raise RuntimeError(
                "CloudMail 未配置完整：请设置 cloudmail_api_base 与 cloudmail_admin_password"
            )

    def _headers(self, token: str = "") -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if token:
            headers["authorization"] = token
        return headers

    def _generate_token(self) -> str:
        import requests

        self._ensure_config()
        payload = {
            "email": self._resolve_admin_email(),
            "password": self.admin_password,
        }
        r = requests.post(
            f"{self.api}/api/public/genToken",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"CloudMail 生成 token 失败: {r.status_code} {str(r.text or '')[:200]}"
            )

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            raise RuntimeError(f"CloudMail 生成 token 失败: {data}")
        token = ((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("CloudMail 生成 token 失败: 响应未返回 token")
        return token

    def _get_token(self, *, force_refresh: bool = False) -> str:
        cache_key = self._cache_key()
        now = time.time()
        with CloudMailMailbox._token_lock:
            if not force_refresh:
                cached = CloudMailMailbox._token_cache.get(cache_key)
                if cached and now < cached[1]:
                    return cached[0]

            token = self._generate_token()
            CloudMailMailbox._token_cache[cache_key] = (token, now + 3600)
            return token

    def _list_mails(self, email: str, *, retry_auth: bool = True) -> list:
        import requests

        token = self._get_token()
        payload = {
            "toEmail": email,
            "timeSort": "desc",
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(token),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code == 401 and retry_auth:
            token = self._get_token(force_refresh=True)
            r = requests.post(
                f"{self.api}/api/public/emailList",
                json=payload,
                headers=self._headers(token),
                proxies=self.proxy,
                timeout=self.timeout,
            )
        if r.status_code != 200:
            return []

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def _gen_prefix(self) -> str:
        import random
        import string

        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        return first + rest

    def _build_email(self) -> str:
        domains = self._domain_candidates()
        if not domains:
            raise RuntimeError("CloudMail 未配置可用域名")
        domain = random.choice(domains)
        if self.subdomain:
            domain = f"{self.subdomain}.{domain}"
        return f"{self._gen_prefix()}@{domain}"

    @staticmethod
    def _parse_message_timestamp(message: dict) -> Optional[float]:
        from datetime import datetime

        keys = [
            "time",
            "date",
            "created",
            "createdAt",
            "created_at",
            "receivedAt",
            "received_at",
            "sendTime",
            "timestamp",
        ]
        for key in keys:
            value = message.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                numeric = float(value)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            text = str(value).strip()
            if not text:
                continue
            try:
                numeric = float(text)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            except (TypeError, ValueError):
                pass
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod
    def _mail_id(message: dict, index: int = 0) -> str:
        for key in ("emailId", "id", "mailId", "messageId"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        digest = (
            str(message.get("date") or message.get("time") or "")
            + "|"
            + str(message.get("subject") or "")
        )
        return f"idx-{index}-{digest}"

    def _remember_seen_id(self, email: str, message_id: str) -> None:
        with CloudMailMailbox._seen_ids_lock:
            CloudMailMailbox._seen_ids.setdefault(email, set()).add(message_id)

    def _load_seen_ids(self, email: str) -> set[str]:
        with CloudMailMailbox._seen_ids_lock:
            return set(CloudMailMailbox._seen_ids.get(email, set()))

    def get_email(self) -> MailboxAccount:
        self._ensure_config()
        email = self._build_email()
        self._log(f"[CloudMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        target = account.account_id or account.email
        try:
            mails = self._list_mails(target)
            return {self._mail_id(msg, idx) for idx, msg in enumerate(mails)}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or set())
        seen.update(self._load_seen_ids(target))
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for idx, msg in enumerate(mails):
                    mid = self._mail_id(msg, idx)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    self._remember_seen_id(target, mid)

                    msg_ts = self._parse_message_timestamp(msg)
                    if otp_sent_at and msg_ts and msg_ts < float(otp_sent_at):
                        continue

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    code = self._safe_extract(content, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[CloudMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(
        self,
        api_url: str = "https://www.duckmail.sbs",
        provider_url: str = "https://api.duckmail.sbs",
        bearer: str = "kevin273945",
        domain: str = "",
        api_key: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.duckmail.sbs").rstrip("/")
        self.provider_url = (provider_url or "https://api.duckmail.sbs").rstrip("/")
        self.bearer = bearer or "kevin273945"
        self.domain = str(domain or "").strip()
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._address = None
        # 如果配置了 API Key，直接请求 DuckMail API；否则走前端代理
        self._direct = bool(self.api_key)

    def _proxy_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def _direct_headers(self, token: str = "") -> dict:
        auth = token or self.api_key
        return {
            "authorization": f"Bearer {auth}",
            "content-type": "application/json",
        }

    def _request(self, method: str, endpoint: str, token: str = "", **kwargs):
        """统一请求方法，根据模式选择直连或代理"""
        import requests

        if self._direct:
            url = f"{self.provider_url}{endpoint}"
            headers = self._direct_headers(token)
        else:
            from urllib.parse import quote

            url = f"{self.api}/api/mail?endpoint={quote(endpoint, safe='')}"
            headers = (
                self._proxy_headers()
                if not token
                else {
                    "authorization": f"Bearer {token}",
                    "x-api-provider-base-url": self.provider_url,
                }
            )
        r = requests.request(
            method, url, headers=headers, proxies=self.proxy, timeout=15, **kwargs
        )
        return r

    def get_email(self) -> MailboxAccount:
        import random, string

        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.domain or self.provider_url.replace("https://api.", "").replace(
            "https://", ""
        )
        address = f"{username}@{domain}"
        print(f"[DuckMail] 创建账号: {address} direct={self._direct}")
        # 创建账号
        r = self._request(
            "POST", "/accounts", json={"address": address, "password": password}
        )
        if r.status_code >= 400 or not r.text.strip().startswith("{"):
            raise RuntimeError(
                f"[DuckMail] 创建账号失败: HTTP {r.status_code} body={r.text[:300]}"
            )
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = self._request(
            "POST", "/token", json={"address": self._address, "password": password}
        )
        if r2.status_code >= 400 or not r2.text.strip().startswith(("{", "[")):
            raise RuntimeError(
                f"[DuckMail] 登录失败: HTTP {r2.status_code} body={r2.text[:300]}"
            )
        self._token = r2.json().get("token", "")
        return MailboxAccount(email=self._address, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._request("GET", "/messages?page=1", token=account.account_id)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from datetime import datetime
        import re

        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")

        def _parse_message_timestamp(*values) -> Optional[float]:
            for value in values:
                if value in (None, ""):
                    continue
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                text = str(value).strip()
                if not text:
                    continue
                try:
                    numeric = float(text)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                except (TypeError, ValueError):
                    pass
                try:
                    normalized = text.replace("Z", "+00:00")
                    return datetime.fromisoformat(normalized).timestamp()
                except ValueError:
                    continue
            return None

        def poll_once() -> Optional[str]:
            try:
                r = self._request("GET", "/messages?page=1", token=account.account_id)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = self._request(
                            "GET", f"/messages/{mid}", token=account.account_id
                        )
                        detail = r2.json()
                        body = (
                            str(detail.get("text") or "")
                            + " "
                            + str(detail.get("subject") or "")
                        )
                    except Exception:
                        detail = {}
                        body = str(msg.get("subject") or "")
                    message_ts = _parse_message_timestamp(
                        detail.get("createdAt"),
                        detail.get("created_at"),
                        detail.get("receivedAt"),
                        detail.get("received_at"),
                        detail.get("date"),
                        detail.get("created"),
                        msg.get("createdAt"),
                        msg.get("created_at"),
                        msg.get("receivedAt"),
                        msg.get("received_at"),
                        msg.get("date"),
                        msg.get("created"),
                    )
                    if otp_sent_at and message_ts and message_ts < float(otp_sent_at):
                        continue
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class MaliAPIMailbox(BaseMailbox):
    """YYDS Mail / MaliAPI 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://maliapi.215.im/v1",
        api_key: str = "",
        domain: str = "",
        auto_domain_strategy: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = str(domain or "").strip()
        self.auto_domain_strategy = str(auto_domain_strategy or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._temp_token = None

    def _headers(self, bearer: str = "") -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict = None,
        params: dict = None,
        bearer: str = "",
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            headers=self._headers(bearer),
            json=json_body,
            params=params,
            proxies=self.proxy,
            timeout=15,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code >= 400:
            error = response.text or f"HTTP {response.status_code}"
            error_code = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or error).strip()
                error_code = str(payload.get("errorCode") or "").strip()
            if error_code:
                raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
            raise RuntimeError(f"MaliAPI 请求失败: {str(error).strip()}")

        if isinstance(payload, dict):
            if payload.get("success") is False:
                error = str(payload.get("error") or "unknown error").strip()
                error_code = str(payload.get("errorCode") or "").strip()
                if error_code:
                    raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
                raise RuntimeError(f"MaliAPI 请求失败: {error}")
            if "data" in payload:
                return payload.get("data")
        return payload

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("MaliAPI 未配置：请在全局设置中填写 maliapi_api_key")

    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        data = self._request("GET", "/messages", params={"address": account.email})
        if isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict:
        data = self._request("GET", f"/messages/{message_id}")
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        self._ensure_api_key()
        body = {}
        if self.domain:
            body["domain"] = self.domain
        if self.auto_domain_strategy:
            body["autoDomainStrategy"] = self.auto_domain_strategy

        data = self._request("POST", "/accounts", json_body=body)
        if not isinstance(data, dict):
            raise RuntimeError(f"MaliAPI 返回异常: {data}")

        email = str(data.get("address") or data.get("email") or "").strip()
        temp_token = str(
            data.get("tempToken") or data.get("temp_token") or data.get("token") or ""
        ).strip()
        inbox_id = str(data.get("id") or "").strip()
        if not email:
            raise RuntimeError(f"MaliAPI 返回空邮箱: {data}")

        self._email = email
        self._temp_token = temp_token
        self._log(f"[MaliAPI] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=temp_token or inbox_id or email,
            extra={
                "provider": "maliapi",
                "temp_token": temp_token,
                "inbox_id": inbox_id,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        self._ensure_api_key()
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        self._ensure_api_key()
        seen = {str(mid) for mid in (before_ids or set())}

        def poll_once() -> Optional[str]:
            try:
                for message in self._list_messages(account):
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = message

                    search_text = " ".join(
                        [
                            str(detail.get("subject") or message.get("subject") or ""),
                            str(detail.get("text") or ""),
                            str(detail.get("html") or ""),
                            str(message.get("subject") or ""),
                            str(message.get("snippet") or ""),
                        ]
                    ).strip()
                    search_text = self._yyds_decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._yyds_safe_extract(search_text, code_pattern)
                    if code:
                        self._log(f"[MaliAPI] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class GPTMailMailbox(BaseMailbox):
    """GPTMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = self._normalize_domain(domain)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=params,
            json=json_body,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"GPTMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else ""
            message = str(error or response.text or f"HTTP {response.status_code}").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {message}")

        if isinstance(payload, dict) and payload.get("success") is False:
            error = str(payload.get("error") or "unknown error").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {error}")

        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _list_messages(self, email: str) -> list[dict]:
        data = self._request_json("GET", "/api/emails", params={"email": email}, timeout=10)
        if isinstance(data, dict):
            messages = data.get("emails", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/email/{message_id}", timeout=10)
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._email = email
            self._log(f"[GPTMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={"provider": "gptmail", "domain": self.domain, "local_address": True},
            )

        data = self._request_json("GET", "/api/generate-email")
        if not isinstance(data, dict):
            raise RuntimeError(f"GPTMail 返回异常: {data}")

        email = str(data.get("email") or "").strip()
        if not email:
            raise RuntimeError(f"GPTMail 返回空邮箱: {data}")

        self._email = email
        self._log(f"[GPTMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "gptmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = {}

                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from_address") or ""),
                            str(message.get("content") or ""),
                            str(message.get("html_content") or ""),
                            str(detail.get("subject") or ""),
                            str(detail.get("content") or ""),
                            str(detail.get("html_content") or ""),
                            str(detail.get("raw_headers") or ""),
                        ]
                    ).strip()
                    search_text = self._decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[GPTMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class OpenTrashMailMailbox(BaseMailbox):
    """OpenTrashMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "",
        domain: str = "",
        password: str = "",
        proxy: str = None,
    ):
        self.api = str(api_url or "").strip().rstrip("/")
        self.domain = self._normalize_domain(domain)
        self.password = str(password or "").strip()
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=8))
        suffix = "".join(random.choices(string.digits, k=2))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json, text/plain, */*"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        timeout: int = 15,
    ):
        import requests

        request_params = dict(params or {})
        if self.password and "password" not in request_params:
            request_params["password"] = self.password

        return requests.request(
            method,
            f"{self.api}{path}",
            params=request_params or None,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )

    def _require_api(self) -> None:
        if not self.api:
            raise RuntimeError(
                "OpenTrashMail 未配置 API URL，请检查 opentrashmail_api_url"
            )

    def _build_email_path(self, email: str) -> str:
        from urllib.parse import quote

        return quote(str(email or "").strip(), safe="@")

    def _parse_random_email(self, html_text: str) -> str:
        import re

        text = str(html_text or "")
        if not text:
            return ""

        match = re.search(r"/address/([^\"'<>\s]+@[^\"'<>\s]+)", text, re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()

        match = re.search(
            r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
            text,
            re.IGNORECASE,
        )
        if match:
            return str(match.group(1) or "").strip()
        return ""

    def _list_messages(self, email: str) -> list[dict[str, Any]]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}",
            timeout=10,
        )
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 收件箱返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 收件箱查询失败: {str(error).strip()}")

        if not payload:
            return []

        messages: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for message_id, item in payload.items():
                if not isinstance(item, dict):
                    continue
                message = dict(item)
                message.setdefault("id", str(message_id))
                messages.append(message)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    messages.append(item)
        return messages

    def _get_message_detail(self, email: str, message_id: str) -> dict[str, Any]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}/{message_id}",
            timeout=10,
        )
        if response.status_code == 404:
            return {}
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 邮件详情返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 邮件详情查询失败: {str(error).strip()}")

        return payload if isinstance(payload, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._log(f"[OpenTrashMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={
                    "provider": "opentrashmail",
                    "domain": self.domain,
                    "local_address": True,
                },
            )

        self._require_api()
        response = self._request("GET", "/api/random", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenTrashMail 随机邮箱生成失败: HTTP {response.status_code}"
            )

        email = self._parse_random_email(response.text)
        if not email:
            preview = (response.text or "")[:200]
            raise RuntimeError(f"OpenTrashMail 未能解析随机邮箱: {preview}")

        self._log(f"[OpenTrashMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "opentrashmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    detail = self._get_message_detail(account.email, message_id)
                    parsed = detail.get("parsed") if isinstance(detail, dict) else {}
                    if not isinstance(parsed, dict):
                        parsed = {}

                    decoded_raw = self._decode_raw_content(detail.get("raw") or "")
                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from") or ""),
                            str(message.get("body") or ""),
                            str(detail.get("from") or ""),
                            str(parsed.get("subject") or ""),
                            str(parsed.get("body") or ""),
                            str(parsed.get("htmlbody") or ""),
                            decoded_raw,
                        ]
                    ).strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[OpenTrashMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        domain_override: str = "",
        domains: Any = None,
        enabled_domains: Any = None,
        subdomain: str = "",
        domain_level_count: Any = 2,
        random_subdomain: Any = False,
        random_name_subdomain: Any = False,
        fingerprint: str = "",
        custom_auth: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = self._normalize_domain(domain)
        self.domain_override = self._normalize_domain(domain_override)
        self.domains = self._parse_domains(domains)
        raw_enabled_domains = self._parse_domains(enabled_domains)
        if self.domains:
            allowed = set(self.domains)
            self.enabled_domains = [d for d in raw_enabled_domains if d in allowed]
        else:
            self.enabled_domains = raw_enabled_domains
        self.subdomain = self._normalize_subdomain(subdomain)
        self.domain_level_count = self._parse_domain_level_count(domain_level_count)
        self.random_subdomain = self._to_bool(random_subdomain)
        self.random_name_subdomain = self._to_bool(random_name_subdomain)
        self.fingerprint = fingerprint
        self.custom_auth = custom_auth
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None

    def _headers(self) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        if self.custom_auth:
            h["x-custom-auth"] = self.custom_auth
        return h

    def _ensure_api_configured(self) -> None:
        if not self.api:
            raise RuntimeError("CF Worker API URL 未配置")

    def _read_json(self, response, action: str):
        try:
            return response.json()
        except Exception:
            body = (response.text or "").strip()
            snippet = body[:200] if body else "<empty>"
            raise RuntimeError(
                f"CF Worker {action} 返回非 JSON 响应: HTTP {response.status_code}, body={snippet}"
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: int = 15,
    ):
        import requests

        url = f"{self.api}{path}"
        response = requests.request(
            method,
            url,
            params=params,
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        body = (response.text or "").strip()
        preview = body[:200] or "<empty>"

        if response.status_code >= 400:
            if "private site password" in body.lower():
                raise RuntimeError(
                    "CFWorker API 需要私有站点密码，请配置 cfworker_custom_auth"
                )
            raise RuntimeError(
                f"CFWorker API {path} 失败: HTTP {response.status_code} {preview}"
            )

        try:
            return response.json()
        except Exception as e:
            raise RuntimeError(
                f"CFWorker API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from e

    def _generate_local_part(self) -> str:
        import string

        # 避免纯数字开头，提高邮箱格式“像真人”的程度
        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        value = str(domain or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value

    @staticmethod
    def _normalize_subdomain(value: Any) -> str:
        sub = str(value or "").strip().lower().strip(".")
        if sub.startswith("@"):
            sub = sub[1:]
        parts = [part for part in sub.split(".") if part]
        return ".".join(parts)

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_domain_level_count(value: Any) -> int:
        try:
            parsed = int(str(value or "").strip() or "2")
        except (TypeError, ValueError):
            return 2
        return parsed if parsed >= 2 else 2

    @classmethod
    def _parse_domains(cls, value: Any) -> list[str]:
        if not value:
            return []

        items: list[Any]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [
                    part for chunk in text.splitlines() for part in chunk.split(",")
                ]
        else:
            items = [value]

        domains: list[str] = []
        seen = set()
        for item in items:
            domain = cls._normalize_domain(item)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
        return domains

    def _pick_domain(self) -> str:
        if self.domain_override:
            return self.domain_override
        if self.enabled_domains:
            return random.choice(self.enabled_domains)
        return self.domain

    def _generate_subdomain_label(self, length: int = 6) -> str:
        import string

        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _compose_domain(self, base_domain: str) -> str:
        domain = self._normalize_domain(base_domain)
        if not domain:
            return ""

        sub_parts: list[str] = []
        if self.random_name_subdomain:
            try:
                import names
                import random

                name_func = random.choice([names.get_first_name, names.get_last_name])
                sub_parts.append(name_func().lower().replace(" ", ""))
            except ImportError:
                sub_parts.append(self._generate_subdomain_label())
        elif self.random_subdomain:
            sub_parts.append(self._generate_subdomain_label())
        if self.subdomain:
            sub_parts.append(self.subdomain)

        base_level_count = len([part for part in domain.split(".") if part])
        expected_total_levels = max(self.domain_level_count, 2)
        missing_levels = max(expected_total_levels - (base_level_count + len(sub_parts)), 0)
        if missing_levels > 0:
            fillers = [self._generate_subdomain_label() for _ in range(missing_levels)]
            sub_parts = fillers + sub_parts

        if not sub_parts:
            return domain
        return f"{'.'.join(sub_parts)}.{domain}"

    def get_email(self) -> MailboxAccount:
        self._ensure_api_configured()
        name = self._generate_local_part()
        payload = {"enablePrefix": True, "name": name}
        selected_domain = self._compose_domain(self._pick_domain())
        if selected_domain:
            payload["domain"] = selected_domain
            self._log(f"[CFWorker] 本次使用域名: {selected_domain}")
        data = self._request_json(
            "POST", "/admin/new_address", payload=payload, timeout=15
        )
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        if not email or not token:
            raise RuntimeError(
                f"CFWorker API /admin/new_address 返回缺少 email/jwt: {data}"
            )
        self._token = token
        print(
            f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}..."
        )
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={"cfworker_domain": selected_domain} if selected_domain else None,
        )

    def _get_mails(self, email: str) -> list:
        self._ensure_api_configured()
        data = self._request_json(
            "GET",
            "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            timeout=10,
        )
        return data.get("results", data) if isinstance(data, dict) else data

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account.email)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        from datetime import datetime, timezone

        seen = set(before_ids or [])
        exclude_codes = set(kwargs.get("exclude_codes") or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None

        def poll_once() -> Optional[str]:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue

                    created_at = str(mail.get("created_at", "") or "").strip()
                    if otp_cutoff and created_at:
                        try:
                            mail_ts = (
                                datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                                .replace(tzinfo=timezone.utc)
                                .timestamp()
                            )
                            if mail_ts < otp_cutoff:
                                self._log(
                                    f"[CFWorker] \u8df3\u8fc7\u65e7\u90ae\u4ef6 id={mid} created_at={created_at}"
                                )
                                continue
                        except Exception:
                            pass

                    # 仅在通过时间边界筛选后再标记为已处理，避免边界邮件被过早加入 seen。
                    seen.add(mid)

                    raw = str(mail.get("raw", ""))
                    subject = str(mail.get("subject", ""))
                    search_text = f"{subject} {self._decode_raw_content(raw)}".strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    search_text = re.sub(r"m=\+\d+\.\d+", "", search_text)
                    search_text = re.sub(r"\bt=\d+\b", "", search_text)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        self._log(
                            f"[CFWorker] \u8df3\u8fc7\u5df2\u7528\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        continue
                    if code:
                        self._log(
                            f"[CFWorker] \u547d\u4e2d\u65b0\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=f"\u7b49\u5f85\u9a8c\u8bc1\u7801\u8d85\u65f6 ({timeout}s)",
        )


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self, api_url: str = "https://sall.cc", api_key: str = "", proxy: str = None
    ):
        self.api = api_url.rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._session_token = None
        self._email = None

    def _api_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"X-API-Key": self.api_key}

    def _register_and_login(self) -> str:
        import requests, random, string

        s = requests.Session()
        s.proxies = self.proxy
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update(
            {"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"}
        )
        s.headers.update(self._api_headers())
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        print(f"[MoeMail] 注册账号: {username} / {password}")
        r_reg = s.post(
            f"{self.api}/api/auth/register",
            json={"username": username, "password": password, "turnstileToken": ""},
            timeout=15,
        )
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        # 获取 CSRF
        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        s.post(
            f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={self.api}",
            allow_redirects=True,
            timeout=15,
        )
        self._session = s
        for cookie in s.cookies:
            if "session-token" in cookie.name:
                self._session_token = cookie.value
                print(f"[MoeMail] 登录成功")
                return cookie.value
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        return ""

    def get_email(self) -> MailboxAccount:
        # 每次调用都重新注册新账号，保证邮箱唯一
        self._session_token = None
        self._register_and_login()
        import random, string

        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，随机选一个
        domain = "sall.cc"
        try:
            cfg_r = self._session.get(
                f"{self.api}/api/config", headers=self._api_headers(), timeout=10
            )
            domains = [
                d.strip()
                for d in cfg_r.json().get("emailDomains", "sall.cc").split(",")
                if d.strip()
            ]
            if domains:
                domain = random.choice(domains)
        except Exception:
            pass
        r = self._session.post(
            f"{self.api}/api/emails/generate",
            headers=self._api_headers(),
            json={"name": name, "domain": domain, "expiryTime": 86400000},
            timeout=15,
        )
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(
            f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}"
        )
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
        if email_id:
            self._email_count = getattr(self, "_email_count", 0) + 1
        return MailboxAccount(email=self._email, account_id=str(email_id))

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails/{account.account_id}",
                headers=self._api_headers(),
                timeout=10,
            )
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails/{account.account_id}",
                    headers=self._api_headers(),
                    timeout=10,
                )
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(
                            msg.get("content")
                            or msg.get("text")
                            or msg.get("body")
                            or msg.get("html")
                            or ""
                        )
                        + " "
                        + str(msg.get("subject") or "")
                    )
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class LuckMailMailbox(BaseMailbox):
    """LuckMail 混合模式：ChatGPT 走购买邮箱，其他平台走订单接码"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_code: str = "",
        email_type: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        if not base_url or not api_key:
            raise RuntimeError(
                "LuckMail 未配置：请在全局设置中填写 luckmail_base_url 和 luckmail_api_key"
            )
        from .luckmail import LuckMailClient

        self._client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy,
        )
        self._project_code = project_code
        self._email_type = email_type or None
        self._domain = domain or None
        self._order_no = None
        self._token = None
        self._email = None

    def _use_purchase_mode(self, account: MailboxAccount = None) -> bool:
        if (
            account
            and account.account_id
            and str(account.account_id).startswith("tok_")
        ):
            return True
        if self._token:
            return True
        return self._project_code == "openai"

    def _resolve_token(self, account: MailboxAccount = None) -> str:
        token = (account.account_id if account else "") or self._token
        if token:
            self._token = token
            return token

        email = (account.email if account else "") or self._email
        if not email:
            return ""

        try:
            purchases = self._client.user.get_purchases(
                page=1,
                page_size=100,
                keyword=email,
            )
        except Exception:
            return ""

        email_lower = str(email).strip().lower()
        for item in purchases.list:
            if str(item.email_address).strip().lower() == email_lower and item.token:
                self._token = item.token
                self._email = item.email_address
                return item.token
        return ""

    def _cancel_order_silently(self, order_no: str) -> None:
        if not order_no:
            return
        try:
            self._client.user.cancel_order(order_no)
            self._log(f"[LuckMail] 已取消订单: {order_no}")
        except Exception:
            pass

    def _extract_code_from_token_mails(
        self,
        token: str,
        code_pattern: str = None,
        before_ids: set = None,
        exclude_codes: set = None,
    ) -> Optional[str]:
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception:
            return None

        seen = {str(mid) for mid in (before_ids or set())}
        excluded = {str(code) for code in (exclude_codes or set()) if code}
        for mail in mail_list.mails:
            message_id = str(mail.message_id or "")
            if message_id and message_id in seen:
                continue
            body = " ".join(
                [
                    str(mail.subject or ""),
                    str(mail.body or ""),
                    str(mail.html_body or ""),
                ]
            )
            code = self._safe_extract(body, code_pattern)
            if code and code in excluded:
                continue
            if code:
                return code
        return None

    def get_email(self) -> MailboxAccount:
        if not self._project_code:
            raise RuntimeError("LuckMail 未设置 project_code，无法创建邮箱")

        if self._use_purchase_mode():
            self._log(
                f"[LuckMail] 分支: ChatGPT + LuckMail -> 购买邮箱接口 "
                f"(project_code={self._project_code}, email_type={self._email_type or '-'}, domain={self._domain or '-'})"
            )
            try:
                result = self._client.user.purchase_emails(
                    project_code=self._project_code,
                    quantity=1,
                    email_type=self._email_type,
                    domain=self._domain,
                )
            except Exception as e:
                raise RuntimeError(f"LuckMail 购买邮箱失败: {e}") from e

            purchases = (result or {}).get("purchases") or []
            if not purchases:
                raise RuntimeError(f"LuckMail 购买邮箱返回为空: {result}")

            item = purchases[0]
            email = str(item.get("email_address") or "").strip()
            token = str(item.get("token") or "").strip()
            if not email or not token:
                raise RuntimeError(f"LuckMail 返回缺少 email/token: {item}")

            self._email = email
            self._token = token
            self._log(f"[LuckMail] 已购邮箱: {email}")
            if item.get("warranty_until"):
                self._log(f"[LuckMail] 质保到期: {item.get('warranty_until')}")
            return MailboxAccount(
                email=email,
                account_id=token,
                extra={
                    "provider": "luckmail",
                    "token": token,
                    "project_code": self._project_code,
                },
            )

        self._log(
            f"[LuckMail] 分支: 其他平台 + LuckMail -> 创建订单/订单接码 "
            f"(project_code={self._project_code}, email_type={self._email_type or '-'})"
        )
        try:
            body = {"project_code": self._project_code}
            if self._email_type:
                body["email_type"] = self._email_type
            order = self._client.user._sync_create_order(body)
        except Exception as e:
            raise RuntimeError(f"LuckMail 创建订单失败: {e}") from e
        self._order_no = order.order_no
        email = order.email_address
        self._email = email
        self._log(f"[LuckMail] 订单 {order.order_no} 分配邮箱: {email}")
        self._log(f"[LuckMail] 超时时间: {order.expired_at}")
        return MailboxAccount(email=email, account_id=order.order_no)

    def get_current_ids(self, account: MailboxAccount) -> set:
        if not self._use_purchase_mode(account):
            return set()
        token = self._resolve_token(account)
        if not token:
            return set()
        try:
            mail_list = self._client.user.get_token_mails(token)
            return {str(m.message_id) for m in (mail_list.mails or []) if m.message_id}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        if not self._use_purchase_mode(account):
            self._log("[LuckMail] 等验证码分支: 订单接码")
            order_no = account.account_id or self._order_no
            if not order_no:
                raise RuntimeError("LuckMail 未创建订单，无法等待验证码")

            def on_poll_order(result):
                self._log(f"[LuckMail] 轮询中... 状态: {result.status}")

            deadline = time.monotonic() + max(int(timeout or 0), 1)
            last_status = "pending"
            try:
                while time.monotonic() < deadline:
                    self._checkpoint()
                    remaining = max(1, int(deadline - time.monotonic()))
                    slice_timeout = min(remaining, 6)
                    try:
                        code_result = self._client.user._sync_wait_for_code(
                            order_no=order_no,
                            timeout=slice_timeout,
                            interval=3.0,
                            on_poll=on_poll_order,
                        )
                    except Exception as e:
                        raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

                    last_status = str(code_result.status or "pending")
                    if code_result.status == "success" and code_result.verification_code:
                        code = code_result.verification_code
                        self._log(f"[LuckMail] 收到验证码: {code}")
                        return code
                    if code_result.status in {"cancelled", "timeout"}:
                        break
            except Exception:
                self._cancel_order_silently(order_no)
                raise

            self._cancel_order_silently(order_no)
            raise TimeoutError(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: {last_status}"
            )

        token = self._resolve_token(account)
        if not token:
            raise RuntimeError("LuckMail 未找到已购邮箱 Token，无法等待验证码")
        self._log("[LuckMail] 等验证码分支: 已购邮箱 Token 收码")

        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        seen_message_ids = {str(mid) for mid in (before_ids or set()) if mid}
        if before_ids is None:
            seen_message_ids = self.get_current_ids(account)
            if seen_message_ids:
                self._log(
                    f"[LuckMail] 已建立旧邮件基线，先跳过 {len(seen_message_ids)} 封历史邮件"
                )

        saw_new_mail = False

        def poll_once() -> Optional[str]:
            nonlocal saw_new_mail
            found_new_mail = False
            try:
                mail_list = self._client.user.get_token_mails(token)
            except Exception as e:
                raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

            for mail in mail_list.mails:
                message_id = str(mail.message_id or "").strip()
                if message_id and message_id in seen_message_ids:
                    continue

                found_new_mail = True
                saw_new_mail = True
                if message_id:
                    seen_message_ids.add(message_id)

                body = " ".join(
                    [
                        str(mail.subject or ""),
                        str(mail.body or ""),
                        str(mail.html_body or ""),
                    ]
                )
                code = self._safe_extract(body, code_pattern)
                if code and code in exclude_codes:
                    self._log(
                        f"[LuckMail] 跳过已使用验证码 message_id={message_id or '-'} code={code}"
                    )
                    continue
                if code:
                    self._log(f"[LuckMail] 收到验证码: {code}")
                    return code

            self._log(
                f"[LuckMail] 轮询中... 新邮件: {'是' if found_new_mail else '否'}"
            )

            if found_new_mail:
                self._log("[LuckMail] 新邮件还不是可用验证码，继续等下一封...")
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: "
                f"has_new_mail={saw_new_mail}"
            ),
        )


class OutlookMailboxBackend(ABC):
    """Outlook 收信后端策略。"""

    backend_name: str = ""

    def __init__(self, mailbox: "OutlookMailbox"):
        self.mailbox = mailbox

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        ...


class OutlookImapMailboxBackend(OutlookMailboxBackend):
    backend_name = "imap"

    def __init__(self, mailbox):
        super().__init__(mailbox)
        # 某些 Outlook IMAP 服务并没有 Deleted Items / Trash，失败后不应在每轮
        # 5 秒轮询中重复刷错误日志。进程内缓存即可，重新启动后会重新探测。
        self._unsupported_folders: set[str] = set()
        self._folder_lock = threading.Lock()

    @staticmethod
    def _imap_mailbox_argument(folder: str) -> str:
        """为 IMAP mailbox 参数加引号。

        imaplib.select("Deleted Items") 会直接发出 ``SELECT Deleted Items``，服务端
        将空格后的 Items 视为第二个命令参数并返回 BAD Command Argument Error。
        这里按 IMAP quoted-string 规则转义后发 ``SELECT "Deleted Items"``。
        """
        name = str(folder or "")
        if not name:
            return name
        if any(char.isspace() for char in name) or any(char in name for char in ('"', '\\')):
            return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return name

    def _select_folder(self, imap_conn, folder: str):
        """以正确的 mailbox 参数选择文件夹；只读失败时再尝试读写 SELECT。"""
        mailbox_arg = self._imap_mailbox_argument(folder)
        try:
            return imap_conn.select(mailbox_arg, readonly=True)
        except Exception as readonly_error:
            self.mailbox._log(
                f"[微软邮箱][IMAP] EXAMINE folder={folder} 被拒绝，改用 SELECT: {readonly_error}"
            )
            try:
                return imap_conn.select(mailbox_arg, readonly=False)
            except Exception as select_error:
                # 将两个错误组合抛给调用方，只记录一次，而不是每轮两条 EXAMINE/SELECT。
                raise RuntimeError(
                    f"EXAMINE: {readonly_error}; SELECT: {select_error}"
                ) from select_error

    def _is_unsupported(self, folder: str) -> bool:
        with self._folder_lock:
            return folder in self._unsupported_folders

    def _mark_unsupported(self, folder: str, error: Exception | str) -> None:
        with self._folder_lock:
            if folder in self._unsupported_folders:
                return
            self._unsupported_folders.add(folder)
        self.mailbox._log(
            f"[微软邮箱][IMAP] folder={folder} 不可用，后续轮询跳过: {error}"
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        imap_conn = None
        try:
            imap_conn = self.mailbox._open_imap(account)
            seen: set[str] = set()
            for folder in self.mailbox._imap_folder_names:
                if self._is_unsupported(folder):
                    continue
                try:
                    status, _ = self._select_folder(imap_conn, folder)
                except Exception as exc:
                    self._mark_unsupported(folder, exc)
                    continue
                if status != "OK":
                    continue
                status, data = imap_conn.uid("search", None, "ALL")
                if status != "OK":
                    continue
                ids = data[0].split() if data and data[0] else []
                for uid in ids[-100:]:
                    uid_str = (
                        uid.decode("utf-8", errors="ignore")
                        if isinstance(uid, bytes)
                        else str(uid)
                    )
                    if uid_str:
                        seen.add(f"{folder}:{uid_str}")
            return seen
        finally:
            try:
                if imap_conn:
                    imap_conn.logout()
            except Exception:
                pass

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        from email import message_from_bytes
        from email.utils import parsedate_to_datetime
        from email.policy import default as email_default_policy

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")
        try:
            otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None
        except (TypeError, ValueError):
            otp_cutoff = None
        keyword_lower = str(keyword or "").strip().lower()

        def poll_once() -> Optional[str]:
            for folder in self.mailbox._imap_folder_names:
                if self._is_unsupported(folder):
                    continue
                imap_conn = None
                try:
                    self.mailbox._log(f"[微软邮箱][IMAP] folder={folder} 开始轮询")
                    imap_conn = self.mailbox._open_imap(account)
                    self.mailbox._log(f"[微软邮箱][IMAP] folder={folder} IMAP 登录成功")
                    try:
                        status, _ = self._select_folder(imap_conn, folder)
                    except Exception as exc:
                        self._mark_unsupported(folder, exc)
                        continue
                    if status != "OK":
                        self._mark_unsupported(folder, f"SELECT status={status}")
                        continue
                    status, data = imap_conn.uid("search", None, "ALL")
                    if status != "OK":
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} search 失败: status={status}"
                        )
                        continue
                    ids = data[0].split() if data and data[0] else []
                    if len(ids) > 50:
                        ids = ids[-50:]
                    new_uids = []
                    for uid in ids:
                        uid_str = (
                            uid.decode("utf-8", errors="ignore")
                            if isinstance(uid, bytes)
                            else str(uid)
                        )
                        seen_key = f"{folder}:{uid_str}"
                        if not uid_str or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_uids.append(uid)
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} uid_total={len(ids)} new_uid_count={len(new_uids)}"
                    )
                    for uid in new_uids:
                        status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                        if status != "OK":
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 失败: uid={uid!r} status={status}"
                            )
                            continue
                        raw = None
                        for item in msg_data or []:
                            if isinstance(item, tuple) and item[1]:
                                raw = item[1]
                                break
                        if not raw:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 空响应: uid={uid!r}"
                            )
                            continue
                        msg = message_from_bytes(raw, policy=email_default_policy)
                        subject = self.mailbox._decode_header_value(msg.get("Subject", ""))
                        text = self.mailbox._extract_message_text(msg)
                        if otp_cutoff:
                            try:
                                message_ts = parsedate_to_datetime(
                                    msg.get("Date", "") or ""
                                ).timestamp()
                            except (TypeError, ValueError, OverflowError):
                                message_ts = 0
                            if message_ts and message_ts < otp_cutoff:
                                self.mailbox._log(
                                    f"[微软邮箱][IMAP] folder={folder} 跳过发码前旧邮件: uid={uid_str}"
                                )
                                continue
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 跳过已尝试验证码: {code}"
                            )
                            continue
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} 验证码提取成功: {code}"
                        )
                        return code
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} IMAP 查询异常: {exc}"
                    )
                    # A connection/authentication failure happens before a
                    # folder is selected. Retrying every folder and every poll
                    # would turn one dead IMAP route into several minutes of
                    # add-email OTP waiting. Surface it to the provider so the
                    # binding flow can stop/requeue promptly.
                    if imap_conn is None:
                        raise RuntimeError(f"微软邮箱 IMAP 不可用: {exc}") from exc
                    continue
                finally:
                    try:
                        if imap_conn:
                            imap_conn.logout()
                    except Exception:
                        pass
            return None

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )


class _GraphUnauthorizedError(RuntimeError):
    """Graph access token was issued but rejected by the API (missing scope).

    This is not a transient token expiry: a forced refresh returns a token with
    the same missing permission. Callers must fall back to IMAP or fail fast
    instead of polling every folder until the OTP deadline expires.
    """


class OutlookGraphMailboxBackend(OutlookMailboxBackend):
    backend_name = "graph"

    def get_current_ids(self, account: MailboxAccount) -> set:
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() == "imap":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].get_current_ids(account)
        access_token = self.mailbox._get_oauth_access_token(
            account,
            preferred_backend=self.backend_name,
        )
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() != "graph":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].get_current_ids(account)
        seen: set[str] = set()
        for folder in self.mailbox._graph_folder_names:
            try:
                messages = self.mailbox._graph_list_messages(
                    access_token=access_token,
                    folder=folder,
                )
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if message_id:
                        seen.add(f"{folder}:{message_id}")
            except RuntimeError as exc:
                if "HTTP 401" in str(exc):
                    # 401 → token 失效，强制刷新后重试一次
                    self.mailbox._log(
                        f"[微软邮箱][Graph] get_current_ids folder={folder} 遇到 401，强制刷新 token"
                    )
                    _cache = (account.extra or {}).get("_oauth_token_cache")
                    if isinstance(_cache, dict):
                        _cache.pop(
                            self.mailbox._normalize_backend_name(self.backend_name), None
                        )
                    access_token = self.mailbox._get_oauth_access_token(
                        account,
                        preferred_backend=self.backend_name,
                    )
                    try:
                        messages = self.mailbox._graph_list_messages(
                            access_token=access_token,
                            folder=folder,
                        )
                        for message in messages:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                seen.add(f"{folder}:{message_id}")
                    except Exception as retry_exc:
                        # Same token permission problem as wait_for_code: mark
                        # IMAP-only so later polls skip the doomed Graph calls.
                        extra = account.extra if isinstance(account.extra, dict) else {}
                        account.extra = extra
                        extra["_oauth_backend_capability"] = "imap"
                        self.mailbox._log(
                            "[微软邮箱] Graph 接口 401（token 缺少 Mail.Read 权限），"
                            f"标记为仅 IMAP: {retry_exc}"
                        )
                else:
                    raise
        return seen

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() == "imap":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )
        self.mailbox._get_oauth_access_token(account, preferred_backend=self.backend_name)
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() != "graph":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        # 标记是否已做过一次 401 强制刷 token，避免无限循环
        _token_refreshed = False

        def _force_refresh_token() -> str:
            """清除 OAuth 缓存，强制重新获取 access token。"""
            _cache = (account.extra or {}).get("_oauth_token_cache")
            if isinstance(_cache, dict):
                _cache.pop(
                    self.mailbox._normalize_backend_name(self.backend_name), None
                )
            return self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )

        def poll_once() -> Optional[str]:
            nonlocal _token_refreshed
            access_token = self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )
            for folder in self.mailbox._graph_folder_names:
                try:
                    self.mailbox._log(f"[微软邮箱][Graph] folder={folder} 开始轮询")
                    messages = self.mailbox._graph_list_messages(
                        access_token=access_token,
                        folder=folder,
                    )
                    new_messages = []
                    for message in messages:
                        message_id = str(message.get("id") or "").strip()
                        seen_key = f"{folder}:{message_id}"
                        if not message_id or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_messages.append(message)
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} message_total={len(messages)} new_count={len(new_messages)}"
                    )
                    for message in new_messages:
                        subject = str(message.get("subject") or "").strip()
                        text = self.mailbox._graph_message_text(message)
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                detail = self.mailbox._graph_get_message(
                                    access_token=access_token,
                                    message_id=message_id,
                                )
                                text = self.mailbox._graph_message_text(detail)
                                code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 跳过已尝试验证码: {code}"
                            )
                            continue
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 验证码提取成功: {code}"
                        )
                        return code
                except Exception as exc:
                    exc_str = str(exc)
                    # 401 → token 失效，强制刷新后重试一次
                    if "HTTP 401" in exc_str and not _token_refreshed:
                        _token_refreshed = True
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 遇到 401，强制刷新 token 后重试"
                        )
                        try:
                            access_token = _force_refresh_token()
                            messages = self.mailbox._graph_list_messages(
                                access_token=access_token,
                                folder=folder,
                            )
                            new_messages = []
                            for message in messages:
                                message_id = str(message.get("id") or "").strip()
                                seen_key = f"{folder}:{message_id}"
                                if not message_id or seen_key in seen:
                                    continue
                                seen.add(seen_key)
                                new_messages.append(message)
                            for message in new_messages:
                                subject = str(message.get("subject") or "").strip()
                                text = self.mailbox._graph_message_text(message)
                                if keyword_lower and keyword_lower not in text.lower():
                                    continue
                                code = self.mailbox._safe_extract(text, code_pattern)
                                if not code:
                                    mid = str(message.get("id") or "").strip()
                                    if mid:
                                        detail = self.mailbox._graph_get_message(
                                            access_token=access_token,
                                            message_id=mid,
                                        )
                                        text = self.mailbox._graph_message_text(detail)
                                        code = self.mailbox._safe_extract(text, code_pattern)
                                if code and code not in exclude_codes:
                                    self.mailbox._log(
                                        f"[微软邮箱][Graph] folder={folder} 刷新 token 后验证码提取成功: {code}"
                                    )
                                    return code
                        except Exception as retry_exc:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 刷新 token 后仍然失败: {retry_exc}"
                            )
                            if "HTTP 401" in str(retry_exc):
                                raise _GraphUnauthorizedError(str(retry_exc)) from retry_exc
                        continue
                    if "HTTP 401" in exc_str:
                        # A second, freshly minted token was rejected as well.
                        raise _GraphUnauthorizedError(exc_str) from exc
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} 查询异常: {exc}"
                    )
                    continue
            return None

        try:
            return self.mailbox._run_polling_wait(
                timeout=timeout,
                poll_interval=5,
                poll_once=poll_once,
            )
        except _GraphUnauthorizedError as exc:
            # Do not keep polling a permission-less Graph token: mark the
            # account as IMAP-only and hand this wait over to the IMAP backend.
            extra = account.extra if isinstance(account.extra, dict) else {}
            account.extra = extra
            extra["_oauth_backend_capability"] = "imap"
            extra.pop("_oauth_token_cache", None)
            self.mailbox._log(
                "[微软邮箱] Graph 接口 401（token 缺少 Mail.Read 权限），"
                f"自动切换 IMAP 收码: {exc}"
            )
            return self.mailbox._backends["imap"].wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )


class MailApiUrlOtpBackend(OutlookMailboxBackend):
    backend_name = "mailapi_url"

    @staticmethod
    def _code_key(code: str) -> str:
        return f"mailapi_code:{str(code or '').strip()}"

    def _fetch_mailapi_text(self, account: MailboxAccount) -> str:
        import requests

        extra = account.extra or {}
        url = str(extra.get("mailapi_url") or "").strip()
        if not url:
            raise RuntimeError("mailapi_url 为空，无法轮询取码")
        response = requests.get(
            url,
            timeout=15,
            proxies=getattr(self.mailbox, "_proxy", None),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"MailAPI 取码请求失败: HTTP {response.status_code}"
            )
        return str(response.text or "")

    def _extract_code(self, text: str, code_pattern: str | None) -> str:
        # MailAPI 返回的是网页/JSON，不是原始邮件：按邮件头切分会从第一个空行处
        # 把正文腰斩（分享页的 <style> 里就有空行），码常常正好落在被砍掉的那半边。
        # 提取也走剥链接的那版，免得把 SendGrid 追踪链接里的数字当成验证码。
        normalized_text = self.mailbox._yyds_decode_raw_content(text) or str(text or "")
        return str(self.mailbox._yyds_safe_extract(normalized_text, code_pattern) or "").strip()

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            text = self._fetch_mailapi_text(account)
            code = self._extract_code(text, None)
            return {self._code_key(code)} if code else set()
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        def poll_once() -> Optional[str]:
            try:
                text = self._fetch_mailapi_text(account)
            except Exception as exc:
                self.mailbox._log(f"[MailAPI] 拉取失败: {exc}")
                return None

            if keyword_lower and keyword_lower not in str(text).lower():
                return None
            code = self._extract_code(text, code_pattern)
            if not code:
                return None
            if code in exclude_codes:
                self.mailbox._log(f"[MailAPI] 跳过已尝试验证码: {code}")
                return None
            code_key = self._code_key(code)
            if code_key in seen:
                return None
            seen.add(code_key)
            self.mailbox._log(f"[MailAPI] 收到验证码: {code}")
            return code

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class OutlookMailbox(BaseMailbox):
    """微软邮箱（Outlook / Hotmail）本地账号池（Graph / IMAP 策略）"""

    # 类级别锁：保证多线程并发时取号互斥，防止多个实例取到同一个邮箱
    _pop_lock = threading.Lock()

    def __init__(
        self,
        imap_server: str = "",
        imap_port: int | str = 993,
        token_endpoint: str = "",
        backend: str = "graph",
        graph_api_base: str = "",
        mail_import_source: str = "",
        proxy: str = None,
    ):
        self._lock = threading.Lock()
        self._mail_import_source = str(mail_import_source or "").strip().lower()
        self._pool_account_type = self._resolve_pool_account_type(mail_import_source)
        self._proxy = build_requests_proxy_config(proxy)
        self._imap_proxy_url = str(proxy or "").strip()
        self._imap_servers = []
        if imap_server:
            self._imap_servers.append(str(imap_server).strip())
        else:
            try:
                from platforms.chatgpt.constants import OUTLOOK_IMAP_SERVERS

                self._imap_servers.extend(
                    [
                        str(OUTLOOK_IMAP_SERVERS.get("NEW") or "").strip(),
                        str(OUTLOOK_IMAP_SERVERS.get("OLD") or "").strip(),
                    ]
                )
            except Exception:
                self._imap_servers.extend(
                    ["outlook.live.com", "outlook.office365.com"]
                )
        self._imap_servers = [
            host for host in self._imap_servers if isinstance(host, str) and host
        ]
        try:
            self._imap_port = int(imap_port or 993)
        except (TypeError, ValueError):
            self._imap_port = 993
        self._token_endpoint = str(token_endpoint or "").strip()
        self._backend_name = self._normalize_backend_name(backend)
        self._graph_api_base = (
            str(graph_api_base or "").strip() or "https://graph.microsoft.com/v1.0"
        )
        self._imap_folder_names = ["INBOX", "Junk", "Deleted Items", "Trash"]
        self._graph_folder_names = ["inbox", "junkemail", "deleteditems"]
        self._backends: dict[str, OutlookMailboxBackend] = {
            "imap": OutlookImapMailboxBackend(self),
            "graph": OutlookGraphMailboxBackend(self),
            "mailapi_url": MailApiUrlOtpBackend(self),
        }

    @staticmethod
    def _normalize_backend_name(value: Any) -> str:
        backend = str(value or "graph").strip().lower() or "graph"
        return backend if backend in {"graph", "imap"} else "graph"

    @staticmethod
    def _normalize_account_type(value: Any) -> str:
        account_type = str(value or "").strip().lower()
        if account_type in {"mailapi_url", "microsoft_oauth"}:
            return account_type
        return "microsoft_oauth"

    @staticmethod
    def _resolve_pool_account_type(mail_import_source: Any) -> str:
        # The core runtime must not import the mail-import application layer
        # merely to select Outlook/Hotmail/MailAPI entries from its pool.
        from .microsoft_mail_source import resolve_microsoft_pool_account_type

        return resolve_microsoft_pool_account_type(mail_import_source)

    @staticmethod
    def _describe_pool_account_type(account_type: Any) -> str:
        from services.mail_imports.import_source import describe_pool_account_type

        return describe_pool_account_type(account_type)

    @staticmethod
    def _account_type_matches(account_type: Any, wanted: str):
        """老库里 account_type 可能是 NULL 或空串，那些行都是 OAuth 号。"""
        from sqlalchemy import func, or_

        normalized = func.lower(func.trim(func.coalesce(account_type, "")))
        if wanted == "microsoft_oauth":
            return or_(normalized == "microsoft_oauth", normalized == "")
        return normalized == wanted

    def _is_mailapi_account(self, account: MailboxAccount) -> bool:
        extra = getattr(account, "extra", None) or {}
        account_type = self._normalize_account_type(extra.get("account_type"))
        if account_type == "mailapi_url":
            return True
        return bool(str(extra.get("mailapi_url") or "").strip())

    def _pop_account(self) -> dict:
        """原子领取一个 Outlook 池邮箱，跨 spawn 子进程也绝不重复。

        PostgreSQL 用 ``FOR UPDATE SKIP LOCKED``，并发 worker 不会互相等待；
        SQLite 用 ``BEGIN IMMEDIATE`` 抢到唯一写锁后再挑选和置为 in_use。两种
        情况均在同一短事务内完成状态转换，不能只依赖进程内 threading.Lock。
        """
        from sqlalchemy import func, or_
        from sqlmodel import Session, select
        from core.db import engine, AccountModel, OutlookAccountModel, _utcnow

        wanted_type = self._pool_account_type
        if wanted_type:
            self._log(
                "[微软邮箱] 号池筛选: "
                f"mail_import_source={self._mail_import_source or '(未设置)'} "
                f"只取 account_type={wanted_type}（{self._describe_pool_account_type(wanted_type)}）"
            )
        else:
            self._log("[微软邮箱] 号池筛选: 未指定导入类型，整池取号")

        # Compatibility facade: the repository owns the transaction and
        # cross-process claim semantics while this class retains its API/logs.
        from .outlook_pool_repository import OutlookPoolRepository
        return OutlookPoolRepository().claim_available(
            wanted_type=wanted_type,
            type_label=self._describe_pool_account_type(wanted_type) if wanted_type else "",
        )

        # SQLite 的 IMMEDIATE 会等待 busy_timeout；PG 的 SKIP LOCKED 则直接跳过竞争行。
        with Session(engine) as session:
            if backend == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                registered = select(func.lower(AccountModel.email))
                query = (
                    select(OutlookAccountModel)
                    .where(OutlookAccountModel.enabled == True)
                    .where(or_(
                        OutlookAccountModel.status == "available",
                        OutlookAccountModel.status == None,
                        OutlookAccountModel.status == "",
                    ))
                    .where(func.lower(OutlookAccountModel.email).notin_(registered))
                    .order_by(OutlookAccountModel.id)
                    .limit(1)
                )
                if wanted_type:
                    query = query.where(self._account_type_matches(
                        OutlookAccountModel.account_type, wanted_type
                    ))
                if backend == "postgresql":
                    query = query.with_for_update(skip_locked=True)
                account = session.exec(query).first()
                if not account:
                    # 仅在可领取候选全被已注册账号排除时给出专门提示。
                    blocked_query = (
                        select(func.count())
                        .select_from(OutlookAccountModel)
                        .where(OutlookAccountModel.enabled == True)
                        .where(or_(
                            OutlookAccountModel.status == "available",
                            OutlookAccountModel.status == None,
                            OutlookAccountModel.status == "",
                        ))
                        .where(func.lower(OutlookAccountModel.email).in_(registered))
                    )
                    if wanted_type:
                        blocked_query = blocked_query.where(self._account_type_matches(
                            OutlookAccountModel.account_type, wanted_type
                        ))
                    registered_count = session.exec(blocked_query).one()
                    if registered_count:
                        session.rollback()
                        raise RuntimeError("微软邮箱账号池中剩余的 available 邮箱都已经注册过了，请导入新的邮箱")
                    # 指定视图而池中只有另一类型时，不能错误兜底发号。
                    if wanted_type:
                        other_type_query = (
                            select(func.count())
                            .select_from(OutlookAccountModel)
                            .where(OutlookAccountModel.enabled == True)
                            .where(or_(
                                OutlookAccountModel.status == "available",
                                OutlookAccountModel.status == None,
                                OutlookAccountModel.status == "",
                            ))
                            .where(~self._account_type_matches(
                                OutlookAccountModel.account_type, wanted_type
                            ))
                        )
                        other_type_count = session.exec(other_type_query).one()
                        if other_type_count:
                            session.rollback()
                            expected_type = self._describe_pool_account_type(wanted_type)
                            raise RuntimeError(
                                f"微软邮箱账号池当前没有 {expected_type} 可领取；其他类型账号不会拿来顶替"
                            )
                    session.rollback()
                    raise RuntimeError("微软邮箱账号池没有可领取的 available 邮箱，请导入新的邮箱")

                payload = {
                    "id": account.id, "email": account.email, "password": account.password,
                    "client_id": account.client_id, "refresh_token": account.refresh_token,
                    "account_type": getattr(account, "account_type", "microsoft_oauth"),
                    "mailapi_url": getattr(account, "mailapi_url", ""),
                }
                account.status = "in_use"
                account.last_used = _utcnow()
                account.updated_at = _utcnow()
                session.add(account)
                session.commit()
                return payload
            except Exception:
                session.rollback()
                raise

    def get_email(self) -> MailboxAccount:
        payload = self._pop_account()
        email = str(payload.get("email") or "").strip()
        if not email:
            raise RuntimeError("微软邮箱账号邮箱为空")
        password = str(payload.get("password") or "")
        client_id = str(payload.get("client_id") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        account_type = self._normalize_account_type(payload.get("account_type"))
        mailapi_url = str(payload.get("mailapi_url") or "").strip()
        auth_mode = (
            "mailapi_url"
            if account_type == "mailapi_url"
            else ("oauth" if client_id and refresh_token else "password")
        )
        self._log(f"[微软邮箱] 取出账号: {email}（已从本地池移除）")
        self._log(
            "[微软邮箱] 账号认证信息: "
            f"has_password={bool(password)} "
            f"has_client_id={bool(client_id)} "
            f"has_refresh_token={bool(refresh_token)} "
            f"has_mailapi_url={bool(mailapi_url)} "
            f"account_type={account_type} "
            f"auth_mode={auth_mode}"
        )
        return MailboxAccount(
            email=email,
            account_id=str(payload.get("id") or ""),
            extra={
                "provider": "microsoft",
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "account_type": account_type,
                "mailapi_url": mailapi_url,
                "outlook_backend": self._backend_name,
            },
        )

    def requeue_account(self, account: MailboxAccount) -> None:
        from sqlmodel import Session, select
        from core.db import engine, OutlookAccountModel, _utcnow

        email = str(getattr(account, "email", "") or "").strip()
        extra = getattr(account, "extra", None) or {}
        if not email:
            return

        password = str(extra.get("password") or "")
        client_id = str(extra.get("client_id") or "")
        refresh_token = str(extra.get("refresh_token") or "")
        account_type = self._normalize_account_type(extra.get("account_type"))
        mailapi_url = str(extra.get("mailapi_url") or "")
        from .outlook_pool_repository import OutlookPoolRepository

        with self._lock:
            OutlookPoolRepository().requeue(
                email=email, password=password, client_id=client_id,
                refresh_token=refresh_token, account_type=account_type, mailapi_url=mailapi_url,
            )
            return
            with Session(engine) as session:
                existing = session.exec(
                    select(OutlookAccountModel).where(OutlookAccountModel.email == email)
                ).first()
                if existing:
                    existing.password = password
                    existing.client_id = client_id
                    existing.refresh_token = refresh_token
                    existing.account_type = account_type
                    existing.mailapi_url = mailapi_url
                    existing.enabled = True
                    existing.status = "available"
                    existing.updated_at = _utcnow()
                    session.add(existing)
                else:
                    session.add(
                        OutlookAccountModel(
                            email=email,
                            password=password,
                            client_id=client_id,
                            refresh_token=refresh_token,
                            account_type=account_type,
                            mailapi_url=mailapi_url,
                            enabled=True,
                            status="available",
                            created_at=_utcnow(),
                            updated_at=_utcnow(),
                        )
                    )
                session.commit()
        self._log(f"[微软邮箱] 账号已回退到本地池: {email}")

    def set_account_status(self, account: MailboxAccount, status: str) -> None:
        """更新已领取邮箱的生命周期状态，保留原始导入记录。"""
        allowed = {"available", "in_use", "used", "failed"}
        normalized = str(status or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(f"未知邮箱池状态: {status}")
        from sqlmodel import Session, select
        from core.db import engine, OutlookAccountModel, _utcnow

        email = str(getattr(account, "email", "") or "").strip()
        account_id = str(getattr(account, "account_id", "") or "").strip()
        from .outlook_pool_repository import OutlookPoolRepository
        with self._lock:
            OutlookPoolRepository().set_status(account_id=account_id, email=email, status=normalized)
            return
            with Session(engine) as session:
                row = None
                if account_id.isdigit():
                    row = session.get(OutlookAccountModel, int(account_id))
                if row is None and email:
                    row = session.exec(
                        select(OutlookAccountModel).where(OutlookAccountModel.email == email)
                    ).first()
                if row is None:
                    return
                row.status = normalized
                row.updated_at = _utcnow()
                session.add(row)
                session.commit()

    def _token_endpoints(self) -> list[str]:
        if self._token_endpoint:
            return [self._token_endpoint]
        try:
            from platforms.chatgpt.constants import MICROSOFT_TOKEN_ENDPOINTS

            return [
                MICROSOFT_TOKEN_ENDPOINTS.get("CONSUMERS", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("LIVE", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("COMMON", ""),
            ]
        except Exception:
            return [
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "https://login.live.com/oauth20_token.srf",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            ]

    # 参考实现（auto_reg / any-auto-register 同源）给出的优先级常量：
    # OUTLOOK_PROVIDER_PRIORITY = ["imap_new", "imap_old", "graph_api"]
    # imap_old 就是“不带 scope 参数”的老 IMAP 端点，对应这里的空 scope。
    _PRIORITY_LABELS = {
        "imap_new": "imap_new",
        "imap_old": "empty",
        "graph_api": "graph_default",
    }

    def _outlook_provider_priority(self) -> list[str]:
        """返回 scope 尝试顺序，可用 OUTLOOK_BACKEND_PRIORITY 覆盖。

        未配置时保持原有行为（按当前后端优先）；显式配置 ``imap`` 即采用参考
        实现的优先级：先 IMAP（new → old），Graph 作为兜底。
        """
        raw = str(os.getenv("OUTLOOK_BACKEND_PRIORITY", "")).strip().lower()
        if not raw:
            return []
        labels: list[str] = []
        for token in raw.replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if token == "imap":
                # 参考实现的 "imap" 指整条 IMAP 链：新 IMAP 再老 IMAP。
                labels.extend(["imap_new", "empty"])
            elif token == "imap_new":
                labels.append("imap_new")
            elif token in {"imap_old", "old"}:
                labels.append("empty")
            elif token in {"graph", "graph_api", "graph_default"}:
                labels.append("graph_default")
            elif token == "outlook_default":
                labels.append("outlook_default")
        return labels

    def _oauth_scope_candidates(
        self,
        preferred_backend: str | None = None,
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        try:
            from platforms.chatgpt.constants import MICROSOFT_SCOPES

            scope_map = {
                "imap_new": str(MICROSOFT_SCOPES.get("IMAP_NEW") or "").strip(),
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": str(MICROSOFT_SCOPES.get("GRAPH_API") or "").strip(),
                "empty": "",
            }
        except Exception:
            scope_map = {
                "imap_new": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": "https://graph.microsoft.com/.default",
                "empty": "",
            }

        backend = self._normalize_backend_name(preferred_backend or self._backend_name)
        ordered_labels = (
            ["graph_default", "outlook_default", "imap_new", "empty"]
            if backend == "graph"
            else ["imap_new", "outlook_default", "graph_default", "empty"]
        )
        configured = self._outlook_provider_priority()
        if configured:
            # Configured order first, remaining labels keep working as fallback.
            ordered_labels = configured + [label for label in ordered_labels if label not in configured]
        raw_candidates = [(label, scope_map.get(label, "")) for label in ordered_labels]

        seen = set()
        for label, scope in raw_candidates:
            key = (str(label or "").strip(), str(scope or "").strip())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(key)
        return candidates

    def probe_oauth_availability(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        if not client_id or not refresh_token:
            self._log(
                f"[微软邮箱] OAuth token 跳过: email={email} has_client_id={bool(client_id)} has_refresh_token={bool(refresh_token)}"
            )
            return {
                "ok": False,
                "reason": "missing_oauth_credentials",
                "message": "缺少 client_id 或 refresh_token，无法通过微软邮箱可用性检测",
            }

        import requests

        last_error = ""
        for endpoint in self._token_endpoints():
            endpoint = str(endpoint or "").strip()
            if not endpoint:
                continue
            for scope_label, scope in self._oauth_scope_candidates(preferred_backend):
                payload = {
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
                if scope:
                    payload["scope"] = scope
                try:
                    self._log(
                        "[微软邮箱] OAuth token 请求: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} has_scope={bool(scope)}"
                    )
                    resp = requests.post(
                        endpoint,
                        data=payload,
                        timeout=20,
                        proxies=self._proxy,
                    )
                    self._log(
                        "[微软邮箱] OAuth token 响应: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} status={resp.status_code}"
                    )
                except Exception as exc:
                    last_error = str(exc)
                    self._log(
                        "[微软邮箱] OAuth token 请求异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={exc}"
                    )
                    continue

                body_text = str(resp.text or "")[:500]
                if resp.status_code >= 400:
                    self._log(f"[微软邮箱] OAuth token 失败响应: {body_text[:200]}")
                    lowered = body_text.lower()
                    if "invalid_grant" in lowered and "service abuse mode" in lowered:
                        return {
                            "ok": False,
                            "reason": "service_abuse_mode",
                            "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                            "status_code": resp.status_code,
                            "endpoint": endpoint,
                            "scope_label": scope_label,
                        }
                    last_error = body_text or f"HTTP {resp.status_code}"
                    continue

                try:
                    data = resp.json() if resp.content else {}
                    access_token = str(data.get("access_token") or "").strip()
                    if access_token:
                        expires_in = data.get("expires_in")
                        try:
                            expires_in_value = max(int(expires_in or 0), 0)
                        except (TypeError, ValueError):
                            expires_in_value = 0
                        self._log(
                            f"[微软邮箱] OAuth access token 获取成功: {email} (scope_label={scope_label})"
                        )
                        return {
                            "ok": True,
                            "reason": "ok",
                            "message": "微软邮箱可用性检测通过",
                            "access_token": access_token,
                            "scope_label": scope_label,
                            "endpoint": endpoint,
                            "expires_in": expires_in_value,
                        }

                    self._log(
                        f"[微软邮箱] OAuth token 响应未包含 access_token: keys={sorted(list(data.keys()))[:10]}"
                    )
                    last_error = body_text or "OAuth 响应未包含 access_token"
                except Exception as exc:
                    last_error = body_text or str(exc) or "OAuth 响应解析失败"
                    self._log(
                        "[微软邮箱] OAuth token 响应解析异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={exc}"
                    )
                    continue

        return {
            "ok": False,
            "reason": "oauth_token_failed",
            "message": f"微软邮箱可用性检测未通过: {last_error or 'OAuth token 获取失败'}",
        }

    def probe_receive_capability(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        prefer_imap: bool = False,
    ) -> dict[str, Any]:
        """检测账号是否真的能收到邮件，而不是只换到了 token。

        微软在 refresh_token 只被授予部分权限时，仍会为 ``.default`` scope 返回
        200 和 access token，但该 token 调 Graph 会一路 401；反过来有些账号只在
        IMAP 可用。只验证“拿到 token”会在导入阶段放进一批收不到码的账号，
        运行期只能靠超时才发现。
        """
        if not client_id or not refresh_token:
            return {
                "ok": False,
                "capability": "",
                "reason": "missing_oauth_credentials",
                "message": "缺少 client_id 或 refresh_token，无法检测收信能力",
            }
        account = MailboxAccount(
            email=str(email or "").strip(),
            extra={
                "provider": "microsoft",
                "client_id": str(client_id or "").strip(),
                "refresh_token": str(refresh_token or "").strip(),
                "account_type": "microsoft_oauth",
            },
        )
        order = ["imap", "graph"] if prefer_imap else ["graph", "imap"]
        reasons: list[str] = []
        for backend in order:
            if backend == "graph":
                try:
                    token = self._get_oauth_access_token(account, preferred_backend="graph")
                    capability = str(
                        (account.extra or {}).get("_oauth_backend_capability") or ""
                    ).strip().lower()
                    if capability != "graph":
                        reasons.append("graph: token 不含 Graph 邮件权限")
                        continue
                    self._graph_request_json(
                        method="GET",
                        path="/me/mailFolders/inbox/messages",
                        access_token=token,
                        params={"$top": "1", "$select": "id"},
                    )
                    return {
                        "ok": True,
                        "capability": "graph",
                        "reason": "ok",
                        "message": "微软邮箱 Graph 收信可用",
                    }
                except Exception as exc:
                    reasons.append(f"graph: {exc}")
            else:
                try:
                    token = self._get_oauth_access_token(account, preferred_backend="imap")
                    self._probe_imap_login(account, access_token=token)
                    return {
                        "ok": True,
                        "capability": "imap",
                        "reason": "ok",
                        "message": "微软邮箱 IMAP 收信可用",
                    }
                except Exception as exc:
                    reasons.append(f"imap: {exc}")
        return {
            "ok": False,
            "capability": "",
            "reason": "receive_unavailable",
            "message": "微软邮箱收信不可用（Graph 与 IMAP 均失败）: " + "；".join(reasons),
        }

    def _probe_imap_login(self, account: MailboxAccount, *, access_token: str) -> None:
        """只验证 IMAP 能否登录并选中收件箱，不拉取邮件。"""
        import imaplib

        last_error: Exception | None = None
        for host in self._imap_servers:
            if not host:
                continue
            try:
                connection = self._connect_imap_ssl(imaplib, host, timeout=15)
                try:
                    if access_token:
                        self._imap_auth_oauth(connection, email=account.email, access_token=access_token)
                    password = str((account.extra or {}).get("password") or "")
                    if not access_token and password:
                        connection.login(account.email, password)
                    status, _ = self._select_folder(connection, "INBOX")
                    if status == "OK":
                        return
                    last_error = RuntimeError(f"SELECT INBOX status={status}")
                finally:
                    try:
                        connection.logout()
                    except Exception:
                        pass
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"IMAP 登录失败: {last_error}")

    def _fetch_oauth_token_bundle(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        probe = self.probe_oauth_availability(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        if probe.get("ok"):
            return {
                "access_token": str(probe.get("access_token") or ""),
                "scope_label": probe.get("scope_label", ""),
                "expires_in": probe.get("expires_in", 0),
                "endpoint": probe.get("endpoint", ""),
            }
        self._log(f"[微软邮箱] OAuth token 获取失败，回退密码登录: {email}")
        return {"reason": str(probe.get("reason") or "")}

    def _fetch_oauth_token(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> str:
        bundle = self._fetch_oauth_token_bundle(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        return str(bundle.get("access_token") or "").strip()

    def _get_oauth_access_token(
        self,
        account: MailboxAccount,
        *,
        preferred_backend: str | None = None,
    ) -> str:
        extra = account.extra or {}
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()
        email_addr = str(account.email or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("微软邮箱 OAuth 凭据缺失，无法获取 access token")

        cache = extra.setdefault("_oauth_token_cache", {})
        cache_key = self._normalize_backend_name(preferred_backend or self._backend_name)
        cached = cache.get(cache_key) if isinstance(cache, dict) else None
        now = time.time()
        if isinstance(cached, dict):
            access_token = str(cached.get("access_token") or "").strip()
            expires_at = float(cached.get("expires_at") or 0)
            if access_token and expires_at > now + 60:
                return access_token

        bundle = self._fetch_oauth_token_bundle(
            email=email_addr,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=cache_key,
        )
        access_token = str(bundle.get("access_token") or "").strip()
        if cache_key == "graph":
            extra["_oauth_backend_capability"] = (
                "graph" if bundle.get("scope_label") == "graph_default" else "imap"
            )
        if not access_token:
            reason = bundle.get("reason", "")
            suffix = f" [{reason}]" if reason else ""
            raise RuntimeError(f"微软邮箱 OAuth access token 获取失败{suffix}")

        expires_in = bundle.get("expires_in")
        try:
            expires_in_value = max(int(expires_in or 0), 0)
        except (TypeError, ValueError):
            expires_in_value = 0
        if isinstance(cache, dict):
            cache[cache_key] = {
                "access_token": access_token,
                "expires_at": now + expires_in_value if expires_in_value else now + 300,
                "scope_label": bundle.get("scope_label", ""),
            }
        return access_token

    def _imap_auth_oauth(self, imap_conn, *, email: str, access_token: str) -> None:
        auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        imap_conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))

    @staticmethod
    def _is_proxy_transport_failure(exc: BaseException) -> bool:
        text = str(exc).lower()
        return (
            isinstance(exc, (TimeoutError, socket.timeout))
            or "timed out" in text
            or "unexpected_eof" in text
            or "eof occurred" in text
            or "connection reset" in text
        )

    def _connect_imap_ssl(self, imaplib, host: str, *, timeout: int = 30, use_proxy: bool = True):
        """Open IMAP through the task proxy, retrying direct only on timeout."""
        proxy_url = self._imap_proxy_url if use_proxy else ""
        if not proxy_url:
            return imaplib.IMAP4_SSL(host, self._imap_port, timeout=timeout)
        try:
            import socks
            import ssl

            parts = urlsplit(proxy_url)
            proxy_type = socks.PROXY_TYPE_HTTP if parts.scheme.lower().startswith("http") else socks.PROXY_TYPE_SOCKS5
            sock = socks.socksocket()
            sock.set_proxy(
                proxy_type,
                parts.hostname,
                parts.port,
                username=unquote(parts.username) if parts.username else None,
                password=unquote(parts.password) if parts.password else None,
                rdns=parts.scheme.lower() == "socks5h",
            )
            sock.settimeout(timeout)
            sock.connect((host, self._imap_port))
            context = ssl.create_default_context()
            tls_sock = context.wrap_socket(sock, server_hostname=host)

            class ConnectedImap(imaplib.IMAP4_SSL):
                def open(self, _host, _port, timeout=None):
                    self.sock = tls_sock
                    self.file = self.sock.makefile("rb")

            self._log(f"[微软邮箱][IMAP] 通过代理连接: {host}")
            return ConnectedImap(host, self._imap_port, timeout=timeout)
        except Exception as exc:
            if not self._is_proxy_transport_failure(exc):
                raise
            self._log(f"[微软邮箱][IMAP] 代理连接/TLS 传输失败，改用直连: {host}")
            return imaplib.IMAP4_SSL(host, self._imap_port, timeout=timeout)

    def _open_imap(self, account: MailboxAccount):
        import imaplib

        email_addr = str(account.email or "").strip()
        extra = account.extra or {}
        password = str(extra.get("password") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()

        access_token = ""
        if client_id and refresh_token:
            access_token = self._get_oauth_access_token(
                account,
                preferred_backend="imap",
            )

        last_error = None
        for host in self._imap_servers:
            if not host:
                continue
            if access_token:
                try:
                    imap_conn = self._connect_imap_ssl(imaplib, host, timeout=30)
                    self._imap_auth_oauth(
                        imap_conn, email=email_addr, access_token=access_token
                    )
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass
                    # A SOCKS/HTTP tunnel can complete TLS but break during
                    # IMAP XOAUTH2. Retry the whole connection+auth direct once.
                    # If direct also fails, OAuth is authoritative for this
                    # imported account: do not repeat the same slow route via
                    # password and do not walk a second host.
                    if self._imap_proxy_url and self._is_proxy_transport_failure(exc):
                        try:
                            self._log(f"[微软邮箱][IMAP] 代理认证/TLS 传输失败，改用直连: {host}")
                            imap_conn = self._connect_imap_ssl(imaplib, host, timeout=30, use_proxy=False)
                            self._imap_auth_oauth(imap_conn, email=email_addr, access_token=access_token)
                            return imap_conn
                        except Exception as direct_exc:
                            last_error = direct_exc
                            try:
                                imap_conn.logout()
                            except Exception:
                                pass
                            raise RuntimeError(f"微软邮箱 IMAP 代理及直连均不可用: {direct_exc}") from direct_exc
                    if access_token:
                        raise RuntimeError(f"微软邮箱 IMAP OAuth 认证失败: {exc}") from exc
            if password:
                try:
                    imap_conn = self._connect_imap_ssl(imaplib, host, timeout=30)
                    imap_conn.login(email_addr, password)
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass
                    if self._imap_proxy_url and self._is_proxy_transport_failure(exc):
                        try:
                            self._log(f"[微软邮箱][IMAP] 代理认证/TLS 传输失败，改用直连: {host}")
                            imap_conn = self._connect_imap_ssl(imaplib, host, timeout=30, use_proxy=False)
                            imap_conn.login(email_addr, password)
                            return imap_conn
                        except Exception as direct_exc:
                            last_error = direct_exc
                            try:
                                imap_conn.logout()
                            except Exception:
                                pass

        raise RuntimeError(f"微软邮箱 IMAP 登录失败: {last_error}")

    def _resolve_backend(self, account: MailboxAccount) -> OutlookMailboxBackend:
        extra = account.extra if isinstance(account.extra, dict) else {}
        account.extra = extra
        if self._is_mailapi_account(account):
            return self._backends["mailapi_url"]
        override = self._normalize_backend_name(
            extra.get("outlook_backend") or self._backend_name
        )
        if override == "graph":
            has_oauth = bool(
                str(extra.get("client_id") or "").strip()
                and str(extra.get("refresh_token") or "").strip()
            )
            if not has_oauth:
                self._log(
                    "[微软邮箱] Graph 后端需要 OAuth 凭据，当前账号缺少 client_id/refresh_token，自动切换 IMAP"
                )
                override = "imap"
        return self._backends.get(override) or self._backends["graph"]

    def _graph_headers(self, *, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"',
        }

    def _graph_request_json(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import requests

        url = f"{self._graph_api_base.rstrip('/')}/{path.lstrip('/')}"
        resp = requests.request(
            method,
            url,
            headers=self._graph_headers(access_token=access_token),
            params=params or None,
            timeout=20,
            proxies=self._proxy,
        )
        if resp.status_code >= 400:
            preview = (resp.text or "")[:300]
            raise RuntimeError(
                f"Outlook Graph 请求失败: HTTP {resp.status_code} {preview}"
            )
        return resp.json() if resp.content else {}

    def _graph_list_messages(
        self,
        *,
        access_token: str,
        folder: str,
    ) -> list[dict[str, Any]]:
        data = self._graph_request_json(
            method="GET",
            path=f"/me/mailFolders/{folder}/messages",
            access_token=access_token,
            params={
                "$top": "25",
                "$orderby": "receivedDateTime DESC",
                "$select": "id,subject,bodyPreview,body,receivedDateTime,from,internetMessageId",
            },
        )
        value = data.get("value") or []
        return value if isinstance(value, list) else []

    def _graph_get_message(
        self,
        *,
        access_token: str,
        message_id: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        return self._graph_request_json(
            method="GET",
            path=f"/me/messages/{quote(str(message_id or '').strip(), safe='')}",
            access_token=access_token,
            params={
                "$select": "id,subject,bodyPreview,body,uniqueBody,receivedDateTime,from,internetMessageId",
            },
        )

    def _graph_message_text(self, message: dict[str, Any]) -> str:
        subject = str((message or {}).get("subject") or "").strip()
        preview = str((message or {}).get("bodyPreview") or "").strip()

        body = (message or {}).get("body") or {}
        body_content = (
            str(body.get("content") or "").strip() if isinstance(body, dict) else ""
        )
        unique_body = (message or {}).get("uniqueBody") or {}
        unique_body_content = (
            str(unique_body.get("content") or "").strip()
            if isinstance(unique_body, dict)
            else ""
        )
        combined = " ".join(
            part for part in [subject, preview, body_content, unique_body_content] if part
        )
        return self._decode_raw_content(combined)

    def _decode_header_value(self, value: str) -> str:
        from email.header import decode_header

        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="ignore"))
                except Exception:
                    decoded.append(part.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    def _extract_message_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))

        combined = (subject + " " + " ".join(body_chunks)).strip()
        return self._decode_raw_content(combined)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            backend = self._resolve_backend(account)
            self._log(f"[微软邮箱] 当前收信后端: {backend.backend_name}")
            return backend.get_current_ids(account)
        except Exception as exc:
            self._log(f"[微软邮箱] 获取当前邮件 ID 失败: {exc}")
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        backend = self._resolve_backend(account)
        self._log(f"[微软邮箱] OTP 收信后端: {backend.backend_name}")
        return backend.wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
            **kwargs,
        )


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        username: str = "",
        password: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.domain = str(domain or "").strip().lstrip("@")
        self.proxy = build_requests_proxy_config(proxy)
        self._session = None
        self._email = None
        self._domains = None

    def _get_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(
                f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15,
            )
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()

        target_domain = self.domain
        domain_index = 0
        if target_domain:
            domains = self._ensure_domains()
            if domains:
                lookup = str(target_domain).lower()
                for idx, domain in enumerate(domains):
                    if str(domain or "").strip().lower() == lookup:
                        domain_index = idx
                        break

        params = {"domainIndex": domain_index} if target_domain else {}
        r = self._session.get(f"{self.api}/api/generate", params=params, timeout=15)
        data = r.json()
        email = str(data.get("email", "") or "")
        if target_domain and email and "@" in email:
            actual_domain = email.split("@", 1)[1].strip().lower()
            if actual_domain != target_domain.lower():
                self._log(
                    f"[Freemail] 指定域名 {target_domain} 未命中，实际返回 {actual_domain}"
                )

        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _ensure_domains(self) -> list:
        if self._domains is not None:
            return self._domains
        self._domains = []
        if not self._session:
            self._get_session()
        try:
            r = self._session.get(f"{self.api}/api/domains", timeout=15)
            payload = r.json()
            normalized = []
            def _append_domain(value):
                domain = str(value or "").strip().lstrip("@")
                if domain and domain not in normalized:
                    normalized.append(domain)
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        _append_domain(
                            item.get("domain")
                            or item.get("name")
                            or item.get("value")
                        )
                    else:
                        _append_domain(item)
            elif isinstance(payload, dict):
                candidates = payload.get("domains") or payload.get("data") or []
                if isinstance(candidates, list):
                    for item in candidates:
                        if isinstance(item, dict):
                            _append_domain(
                                item.get("domain")
                                or item.get("name")
                                or item.get("value")
                            )
                        else:
                            _append_domain(item)
            self._domains = normalized
        except Exception:
            self._domains = []
        return self._domains

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50},
                timeout=10,
            )
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20},
                    timeout=10,
                )
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "").strip()
                    if code and code != "None":
                        if code in exclude_codes:
                            continue
                        return code
                    # 兜底：从 preview 提取
                    text = (
                        str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    )
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        if code in exclude_codes:
                            continue
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


_mailbox_status_write_lock = threading.Lock()


def apply_mailbox_status_events(events) -> None:
    """在任务父进程提交邮箱状态事件。

    spawn 注册子进程只返回事件，避免 SQLite 多进程同时写状态表。SQLite 在此
    使用单写者锁；PostgreSQL 保持并行事务能力。无效或非 Outlook 事件忽略。
    """
    from .outlook_pool_repository import OutlookPoolRepository
    OutlookPoolRepository().apply_status_events(events)
    return

    rows = [row for row in (events or []) if isinstance(row, dict) and row.get("status")]
    if not rows:
        return
    from sqlmodel import Session, select
    from core.db import engine, OutlookAccountModel, _utcnow

    lock = _mailbox_status_write_lock if engine.url.get_backend_name() == "sqlite" else None
    if lock:
        lock.acquire()
    try:
        with Session(engine) as session:
            for item in rows:
                status = str(item.get("status") or "").strip().lower()
                if status not in {"available", "in_use", "used", "failed"}:
                    continue
                email = str(item.get("email") or "").strip()
                account_id = str(item.get("account_id") or "").strip()
                record = session.get(OutlookAccountModel, int(account_id)) if account_id.isdigit() else None
                if record is None and email:
                    record = session.exec(select(OutlookAccountModel).where(
                        OutlookAccountModel.email == email
                    )).first()
                if record is not None:
                    record.status = status
                    record.updated_at = _utcnow()
                    session.add(record)
            session.commit()
    finally:
        if lock:
            lock.release()
