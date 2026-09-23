"""Shared, provider-independent mailbox helpers.

These utilities deliberately know nothing about HTTP clients, provider settings or
persistence.  Keeping them separate makes mailbox providers independently
migratable from the legacy ``core.base_mailbox`` facade.
"""

from __future__ import annotations

import html
import quopri
import re
import time
from typing import Callable, Optional


def extract_verification_code(text: str, pattern: str | None = None) -> Optional[str]:
    """Legacy extraction semantics used by the original ``_safe_extract``."""
    text = str(text or "")
    if not text:
        return None
    patterns = ([pattern] if pattern else []) + [
        r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
        r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
        r"(?<!#)(?<!\d)(\d{6})(?!\d)",
    ]
    for regex in patterns:
        match = re.search(regex, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def extract_yyds_verification_code(text: str, pattern: str | None = None) -> Optional[str]:
    """Hardened YYDS extraction semantics that ignore URL identifiers."""
    text = re.sub(r"https?://\S+", "", str(text or ""))
    if not text:
        return None
    normalized_pattern = (
        r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])"
        if pattern in (r"\d{6}", r"(\d{6})") else pattern
    )
    patterns = ([normalized_pattern] if normalized_pattern else []) + [
        r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
        r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
        r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
    ]
    for regex in patterns:
        match = re.search(regex, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def decode_mail_content(raw: str, *, preserve_parsed_body: bool = False) -> str:
    """Decode a raw email body with selectable legacy or YYDS header handling."""
    text = str(raw or "")
    if not text:
        return ""
    has_headers = re.search(r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text)
    if (not preserve_parsed_body) or has_headers:
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
    try:
        text = quopri.decodestring(text).decode("utf-8", errors="ignore")
    except Exception:
        pass
    text = html.unescape(text)
    text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
    text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
    text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def polling_wait(
    *,
    timeout: int,
    poll_interval: float,
    poll_once: Callable[[], Optional[str]],
    checkpoint: Callable[[], None],
    sleep: Callable[[float], None],
    timeout_message: str | None = None,
) -> str:
    """Poll cooperatively until a code arrives or the deadline expires."""
    timeout_seconds = max(int(timeout or 0), 1)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        checkpoint()
        code = poll_once()
        if code:
            return code
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep(min(float(poll_interval), remaining))
    checkpoint()
    raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")
