"""Typed configuration boundaries over the legacy string key-value store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def parse_config_bool(value, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def parse_config_int(value, *, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed) if minimum is not None else parsed


@dataclass(frozen=True, slots=True)
class SmsSettings:
    enabled: bool = False
    provider: str = "smsbower"
    api_key: str = ""
    service: str = "dr"
    max_price: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "SmsSettings":
        values = values or {}
        return cls(
            enabled=parse_config_bool(values.get("sms_enabled")),
            provider=str(values.get("sms_provider") or "smsbower").strip() or "smsbower",
            api_key=str(values.get("sms_api_key") or "").strip(),
            service=str(values.get("sms_service") or "dr").strip() or "dr",
            max_price=str(values.get("sms_max_price") or "").strip(),
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "sms_enabled": "1" if self.enabled else "0",
            "sms_provider": self.provider,
            "sms_api_key": self.api_key,
            "sms_service": self.service,
            "sms_max_price": self.max_price,
        }


@dataclass(frozen=True, slots=True)
class MailSettings:
    provider: str = "luckmail"
    otp_timeout_seconds: int = 120

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "MailSettings":
        values = values or {}
        provider = str(values.get("mail_provider") or "luckmail").strip().lower()
        if provider == "outlook":
            provider = "microsoft"
        return cls(
            provider=provider or "luckmail",
            otp_timeout_seconds=parse_config_int(
                values.get("mailbox_otp_timeout_seconds"), default=120, minimum=1
            ),
        )
