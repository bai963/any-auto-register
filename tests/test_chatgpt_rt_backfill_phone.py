import unittest
from unittest import mock

from platforms.chatgpt.rt_backfill import (
    BackfillResult, RefreshTokenBackfiller, STRATEGY_LOGIN, STRATEGY_PHONE_LOGIN,
)
from services.chatgpt_rt_backfill import account_identifier_kind, apply_backfill_result, backfill_account_data


class PhoneOnlyBackfillRoutingTests(unittest.TestCase):

    def test_identifier_string_classification(self):
        self.assertEqual(account_identifier_kind(" +573196336329 "), "phone")
        self.assertEqual(account_identifier_kind("bound@example.com"), "email")
        self.assertEqual(account_identifier_kind("not-an-identifier"), "unknown")

    def test_phone_string_routes_even_without_registration_metadata(self):
        with mock.patch("services.chatgpt_otp_mailbox.resolve_otp_mail_provider") as mailbox, \
             mock.patch("services.chatgpt_rt_backfill.RefreshTokenBackfiller") as backfiller:
            backfiller.return_value.run.return_value = BackfillResult(success=False)
            backfill_account_data(email="+573196336329", password="pw", extra={})
        mailbox.assert_not_called()
        self.assertTrue(backfiller.call_args.kwargs["phone_only"])

    def test_bound_email_is_marked_used_in_mailbox_pool(self):
        from core.db import AccountModel
        result = BackfillResult(
            success=False, bound_email="bound@example.com",
            mailbox_status_events=[{"email": "bound@example.com", "account_id": "42", "status": "used"}],
        )
        model = AccountModel(platform="chatgpt", email="+573196336329")
        with mock.patch("core.base_mailbox.apply_mailbox_status_events") as mark_used:
            apply_backfill_result(model, result)
        self.assertEqual(model.email, "bound@example.com")
        mark_used.assert_called_once_with(result.mailbox_status_events)

    def test_phone_only_uses_phone_login_after_session_strategy(self):
        backfiller = RefreshTokenBackfiller(
            email="+573196336329", password="pw", session_token="session",
            phone_only=True,
        )
        calls = []
        backfiller._try_session = lambda: (_ for _ in ()).throw(RuntimeError("no RT"))
        backfiller._try_phone_login = lambda: calls.append("phone")
        result = backfiller.run()
        self.assertEqual(calls, ["phone"])
        self.assertEqual([item.strategy for item in result.attempts], ["session", STRATEGY_PHONE_LOGIN])
        self.assertNotIn(STRATEGY_LOGIN, [item.strategy for item in result.attempts])

    def test_phone_only_does_not_create_or_resolve_email_inbox(self):
        with mock.patch("services.chatgpt_otp_mailbox.resolve_otp_mail_provider") as mailbox, \
             mock.patch("services.chatgpt_rt_backfill.RefreshTokenBackfiller") as backfiller:
            backfiller.return_value.run.return_value = BackfillResult(success=False)
            backfill_account_data(
                email="+573196336329", password="pw",
                extra={},
            )
        mailbox.assert_not_called()
        self.assertTrue(backfiller.call_args.kwargs["phone_only"])
        self.assertTrue(backfiller.call_args.kwargs["allow_login"])

    def test_phone_with_email_keeps_email_backfill_path(self):
        with mock.patch("services.chatgpt_otp_mailbox.resolve_otp_mail_provider", return_value=(None, "x")) as mailbox, \
             mock.patch("services.chatgpt_rt_backfill.RefreshTokenBackfiller") as backfiller:
            backfiller.return_value.run.return_value = BackfillResult(success=False)
            backfill_account_data(
                email="bound@example.com", password="pw",
                extra={},
            )
        mailbox.assert_called_once()
        self.assertFalse(backfiller.call_args.kwargs["phone_only"])


if __name__ == "__main__":
    unittest.main()
