"""Pure task execution policies shared by HTTP and application layers."""

from __future__ import annotations

MAX_REGISTER_RETRY_TIMES = 10
DEFAULT_REGISTER_RETRY_TIMES = 1
REGISTER_ATTEMPT_TIMEOUT_SECONDS = 300
PHONE_REGISTER_ATTEMPT_TIMEOUT_SECONDS = 360


def uses_sms_register_flow(extra: dict | None) -> bool:
    """Return whether a registration flow needs the SMS concurrency budget."""
    extra = extra or {}
    method = str(extra.get("register_method") or "").strip().lower()
    flow = str(extra.get("chatgpt_register_flow") or "").strip().lower()
    return method in {"phone", "sms"} or flow in {"phone", "phone_with_email"}


def normalize_register_retry_times(value) -> int:
    """Normalize untrusted configuration to the supported retry range."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_RETRY_TIMES
    return max(0, min(parsed, MAX_REGISTER_RETRY_TIMES))


def attempt_timeout_seconds(platform: str, extra: dict | None) -> int | None:
    """Return the hard child-process deadline for a registration attempt."""
    if platform == "chatgpt":
        flow = str((extra or {}).get("chatgpt_register_flow") or "").strip().lower()
        if flow == "phone_with_email":
            return None
        if flow == "phone" or uses_sms_register_flow(extra):
            return PHONE_REGISTER_ATTEMPT_TIMEOUT_SECONDS
    return REGISTER_ATTEMPT_TIMEOUT_SECONDS
