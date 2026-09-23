"""导入期收信能力检测的接线（默认关闭，显式开启后拒绝收不到码的账号）。"""
from __future__ import annotations

import unittest
from unittest import mock

from services.mail_imports.microsoft_mailbox_import_service import (
    MicrosoftMailboxImportService,
    _verify_receive_enabled,
)
from services.mail_imports.microsoft_import_rules import MicrosoftMailImportRecord


class _StubMailbox:
    def __init__(self, oauth_ok=True, capability=None):
        self._oauth_ok = oauth_ok
        self._capability = capability

    def probe_oauth_availability(self, **_kwargs):
        if self._oauth_ok:
            return {"ok": True, "reason": "ok", "message": "ok", "access_token": "token"}
        return {"ok": False, "reason": "oauth_token_failed", "message": "微软邮箱可用性检测未通过"}

    def probe_receive_capability(self, **_kwargs):
        return dict(self._capability or {})


def _service(mailbox, *, verify_receive):
    return MicrosoftMailboxImportService(
        pool=mock.Mock(),
        probe_factory=lambda: mailbox,
        alias_expander=lambda records, **_kwargs: list(records),
        workers_resolver=lambda total: max(1, total),
        verify_receive=verify_receive,
    )


def _record():
    return MicrosoftMailImportRecord(
        line_number=1,
        email="demo@outlook.com",
        password="pw",
        client_id="cid",
        refresh_token="rt",
    )


class ReceiveVerificationWiringTests(unittest.TestCase):
    def test_defaults_to_disabled_without_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_verify_receive_enabled())
        with mock.patch.dict("os.environ", {"MAIL_IMPORT_VERIFY_RECEIVE": "1"}):
            self.assertTrue(_verify_receive_enabled())

    def test_disabled_keeps_token_only_behaviour(self):
        mailbox = _StubMailbox(capability={"ok": False, "message": "收信不可用"})
        with mock.patch.object(mailbox, "probe_receive_capability") as capability:
            verdict = _service(mailbox, verify_receive=False)._probe(_record())
        self.assertTrue(verdict["ok"])
        capability.assert_not_called()

    def test_enabled_rejects_account_that_cannot_receive_mail(self):
        mailbox = _StubMailbox(
            capability={
                "ok": False,
                "capability": "",
                "reason": "receive_unavailable",
                "message": "微软邮箱收信不可用（Graph 与 IMAP 均失败）",
            }
        )
        verdict = _service(mailbox, verify_receive=True)._probe(_record())
        self.assertFalse(verdict["ok"])
        self.assertIn("收信不可用", verdict["message"])

    def test_enabled_accepts_account_with_working_backend(self):
        mailbox = _StubMailbox(capability={"ok": True, "capability": "imap", "message": "ok"})
        verdict = _service(mailbox, verify_receive=True)._probe(_record())
        self.assertTrue(verdict["ok"])

    def test_mailapi_rows_skip_receive_verification(self):
        mailbox = _StubMailbox()
        record = MicrosoftMailImportRecord(
            line_number=1,
            email="mailapi@example.com",
            account_type="mailapi_url",
            mailapi_url="https://mailapi.example.com/key",
        )
        with mock.patch.object(mailbox, "probe_receive_capability") as capability, \
             mock.patch.object(mailbox, "probe_oauth_availability") as oauth:
            verdict = _service(mailbox, verify_receive=True)._probe(record)
        self.assertTrue(verdict["ok"])
        capability.assert_not_called()
        oauth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
