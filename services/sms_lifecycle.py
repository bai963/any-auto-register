"""Pure SMS activation lifecycle policy shared by cleanup callers."""

from __future__ import annotations

TERMINAL_STATES = frozenset({"finished", "refunded"})
# States that indicate the phone may already have been accepted must never be
# automatically refunded: a late network response is deliberately ambiguous.
PROTECTED_STATES = frozenset({"otp_received", "otp_submitted", "phone_verified", "account_created"})
REFUNDABLE_STATES = frozenset({"rented", "send_requested", "sms_sent"})


def is_auto_refundable(state: object) -> bool:
    return str(state or "").strip().lower() in REFUNDABLE_STATES


def due_refund_reason(
    state: object,
    *,
    rented_at: float | None,
    sms_sent_at: float | None,
    now: float,
    max_lifetime_seconds: float,
    code_wait_seconds: float,
) -> str:
    """Return a bounded cleanup reason, or an empty string when not due."""
    normalized = str(state or "").strip().lower()
    if normalized not in REFUNDABLE_STATES:
        return ""
    if normalized == "sms_sent":
        if sms_sent_at is not None and now >= sms_sent_at + code_wait_seconds:
            return "短信已发出但等待验证码超时"
        return ""
    if rented_at is not None and now >= rented_at + max_lifetime_seconds:
        return "号码租用后未成功发码超时"
    return ""
