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
        self.assertIn("task_control", backfiller.call_args.kwargs)
        self.assertIn("attempt_id", backfiller.call_args.kwargs)

    def test_action_backfill_bound_email_updates_primary_account_and_marks_mailbox(self):
        from api.actions import _apply_action_result
        from core.db import AccountModel
        model = AccountModel(platform="chatgpt", email="+573196336329", password="pw")
        model.set_extra({"session_token": "st-old"})
        result = {
            "ok": True,
            "data": {"message": "补 RT 成功"},
            "account_email_patch": "bound@example.com",
            "mailbox_status_events": [
                {"email": "bound@example.com", "account_id": "42", "status": "used"}
            ],
            "account_extra_patch": {"refresh_token": "rt-new", "access_token": "at-new"},
        }
        with mock.patch("core.base_mailbox.apply_mailbox_status_events") as mark_used:
            _apply_action_result("chatgpt", "backfill_refresh_token", model, result, mock.Mock())
        self.assertEqual(model.email, "bound@example.com")
        self.assertEqual(model.token, "at-new")
        mark_used.assert_called_once_with(result["mailbox_status_events"])

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

    def test_lazy_add_email_provider_inherits_task_control_and_log_context(self):
        backfiller = RefreshTokenBackfiller(
            email="+573196336329", password="pw", phone_only=True,
            task_control="control", attempt_id=7, log_fn=lambda _message: None,
        )
        mailbox = mock.Mock()
        provider = mock.Mock()
        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox), \
             mock.patch("platforms.chatgpt.protocol.mailbox_adapter.MailboxProviderAdapter", return_value=provider):
            built = backfiller._build_email_bind_provider()
        self.assertIs(built, provider)
        provider.bind_task_control.assert_called_once_with(
            task_control="control", attempt_id=7, log_fn=backfiller._log_fn,
        )

    def test_phone_bind_provider_skips_slow_pre_send_mailbox_prime(self):
        backfiller = RefreshTokenBackfiller(email="+573196336329", password="pw", phone_only=True)
        with mock.patch("core.base_mailbox.create_mailbox", return_value=mock.Mock()), \
             mock.patch("platforms.chatgpt.protocol.mailbox_adapter.MailboxProviderAdapter") as adapter:
            backfiller._build_email_bind_provider()
        self.assertFalse(adapter.call_args.kwargs["prime_on_create"])

    def test_transport_failed_bind_returns_mailbox_to_available_pool(self):
        backfiller = RefreshTokenBackfiller(email="+573196336329", phone_only=True)
        result = BackfillResult(success=False)
        provider = mock.Mock()
        provider.account = mock.Mock(email="a@outlook.com", account_id="7")
        provider._email_bind_status = "failed"
        provider._email_bind_transport_failed = True
        flow = mock.Mock()
        flow.result = mock.Mock(bound_email="", refresh_token="", access_token="", session_token="", id_token="", cookie_header="")
        flow._email_bind_providers = [provider]

        backfiller._absorb(result, flow)

        self.assertEqual(
            [event["status"] for event in result.mailbox_status_events],
            ["available"],
        )

    def test_rejected_bind_marks_mailbox_failed(self):
        backfiller = RefreshTokenBackfiller(email="+573196336329", phone_only=True)
        result = BackfillResult(success=False)
        provider = mock.Mock()
        provider.account = mock.Mock(email="a@outlook.com", account_id="7")
        provider._email_bind_status = "failed"
        provider._email_bind_transport_failed = False
        flow = mock.Mock()
        flow.result = mock.Mock(bound_email="", refresh_token="", access_token="", session_token="", id_token="", cookie_header="")
        flow._email_bind_providers = [provider]

        backfiller._absorb(result, flow)

        self.assertEqual(
            [event["status"] for event in result.mailbox_status_events],
            ["failed"],
        )

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

class CodexOAuthAddEmailTests(unittest.TestCase):
    """Codex authorize 直接落到 /add-email 时也要先绑邮箱再拿 RT。"""

    def _flow(self):
        from platforms.chatgpt.protocol.auth_flow import AuthFlow, AuthResult

        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.result.email = "+573196556312"
        flow._codex_rt_attempted = False
        flow._sms_callback = None
        flow._env_flag = lambda key, default="0": default
        flow._build_codex_authorize = lambda: (
            "https://auth.openai.com/oauth/authorize?prompt=login",
            "state",
            "verifier",
            "http://localhost:1455/auth/callback",
            "client-id",
        )
        flow._is_add_phone_state = lambda page_type="", continue_url="": False
        flow._is_add_email_state = (
            lambda page_type="", continue_url="": "/add-email" in (continue_url or "")
        )
        flow._drop_query_keys = lambda url, keys: ""

        self.follow_calls = []
        self.exchange_calls = []

        def follow(start_url, redirect_uri, trace_prefix):
            self.follow_calls.append((start_url, trace_prefix))
            if len(self.follow_calls) == 1:
                return "", "https://auth.openai.com/add-email"
            return (
                "https://chatgpt.com/api/auth/callback/openai?code=abc&state=state",
                "https://chatgpt.com/",
            )

        def bind(provider, continue_url, mail_provider_factory=None):
            flow.result.bound_email = "new@example.com"
            return "https://auth.openai.com/workspace/select"

        def exchange(**kwargs):
            self.exchange_calls.append(kwargs)
            flow.result.refresh_token = "rt-after-bind"
            return True

        flow._follow_authorize_for_callback = follow
        flow._try_bind_email = bind
        flow._exchange_codex_callback_code = exchange
        return flow

    def test_direct_add_email_is_bound_before_callback_exchange(self):
        flow = self._flow()
        ok = flow.oauth_codex_rt_exchange(
            mail_provider=None,
            email_bind_provider_factory=lambda: object(),
            login_identifier_kind="phone_number",
        )

        self.assertTrue(ok)
        self.assertEqual(flow.result.bound_email, "new@example.com")
        self.assertEqual(flow.result.email, "new@example.com")
        self.assertEqual(flow.result.refresh_token, "rt-after-bind")
        self.assertEqual(len(self.follow_calls), 2)
        self.assertEqual(self.follow_calls[1][0], "https://auth.openai.com/workspace/select")
        self.assertEqual(len(self.exchange_calls), 1)
        self.assertEqual(
            self.exchange_calls[0]["callback_url"],
            "https://chatgpt.com/api/auth/callback/openai?code=abc&state=state",
        )


if __name__ == "__main__":
    unittest.main()
