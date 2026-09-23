"""导入期收信能力检测：验证 token 之外的真实收信路径。"""
from __future__ import annotations

import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount, OutlookMailbox


class ProbeReceiveCapabilityTests(unittest.TestCase):
    def test_graph_permission_denied_falls_back_to_imap(self):
        mailbox = OutlookMailbox()
        with mock.patch.object(
            mailbox,
            "_get_oauth_access_token",
            side_effect=lambda _account, **_kwargs: "token",
        ), mock.patch.object(
            mailbox,
            "_graph_request_json",
            side_effect=RuntimeError("Outlook Graph 请求失败: HTTP 401"),
        ), mock.patch.object(mailbox, "_probe_imap_login") as imap_login:
            result = mailbox.probe_receive_capability(
                email="demo@outlook.com", client_id="cid", refresh_token="rt"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"], "imap")
        imap_login.assert_called_once()

    def test_graph_working_is_reported_without_imap_attempt(self):
        mailbox = OutlookMailbox()
        def token(_account, **kwargs):
            _account.extra["_oauth_backend_capability"] = "graph"
            return "graph-token"

        with mock.patch.object(mailbox, "_get_oauth_access_token", side_effect=token), \
             mock.patch.object(mailbox, "_graph_request_json", return_value={"value": []}), \
             mock.patch.object(mailbox, "_probe_imap_login") as imap_login:
            result = mailbox.probe_receive_capability(
                email="demo@outlook.com", client_id="cid", refresh_token="rt"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"], "graph")
        imap_login.assert_not_called()

    def test_both_backends_failing_reports_unavailable(self):
        mailbox = OutlookMailbox()
        with mock.patch.object(
            mailbox,
            "_get_oauth_access_token",
            side_effect=lambda _account, **_kwargs: "token",
        ), mock.patch.object(
            mailbox,
            "_graph_request_json",
            side_effect=RuntimeError("Outlook Graph 请求失败: HTTP 401"),
        ), mock.patch.object(
            mailbox,
            "_probe_imap_login",
            side_effect=RuntimeError("IMAP 登录失败: handshake operation timed out"),
        ):
            result = mailbox.probe_receive_capability(
                email="demo@outlook.com", client_id="cid", refresh_token="rt"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["capability"], "")
        self.assertIn("Graph 与 IMAP 均失败", result["message"])

    def test_missing_credentials_short_circuits(self):
        mailbox = OutlookMailbox()
        result = mailbox.probe_receive_capability(email="demo@outlook.com", client_id="", refresh_token="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_oauth_credentials")


if __name__ == "__main__":
    unittest.main()
