from services.sms_lifecycle import due_refund_reason, is_auto_refundable


def test_only_pre_verification_states_are_automatically_refundable():
    assert is_auto_refundable("rented")
    assert is_auto_refundable("send_requested")
    assert is_auto_refundable("sms_sent")
    for state in ("otp_received", "otp_submitted", "phone_verified", "account_created", "finished", "refunded"):
        assert not is_auto_refundable(state)


def test_refund_deadlines_protect_ambiguous_otp_submission():
    assert due_refund_reason(
        "rented", rented_at=0, sms_sent_at=None, now=600,
        max_lifetime_seconds=600, code_wait_seconds=240,
    )
    assert due_refund_reason(
        "sms_sent", rented_at=0, sms_sent_at=100, now=340,
        max_lifetime_seconds=600, code_wait_seconds=240,
    )
    assert not due_refund_reason(
        "otp_submitted", rented_at=0, sms_sent_at=100, now=9999,
        max_lifetime_seconds=600, code_wait_seconds=240,
    )
