"""Outlook 收信后端优先级：参考 auto_reg 的 imap_first 顺序，可用环境变量切换。"""
from __future__ import annotations

import unittest
from unittest import mock

from core.base_mailbox import OutlookMailbox


class OutlookBackendPriorityTests(unittest.TestCase):
    def test_default_order_follows_current_backend(self):
        mailbox = OutlookMailbox()
        with mock.patch.dict("os.environ", {}, clear=True):
            labels = [label for label, _scope in mailbox._oauth_scope_candidates("graph")]
            self.assertEqual(labels[0], "graph_default")
            labels_imap = [label for label, _scope in mailbox._oauth_scope_candidates("imap")]
            self.assertEqual(labels_imap[0], "imap_new")

    def test_configured_imap_priority_matches_reference_repo(self):
        mailbox = OutlookMailbox()
        with mock.patch.dict("os.environ", {"OUTLOOK_BACKEND_PRIORITY": "imap,graph"}):
            labels = [label for label, _scope in mailbox._oauth_scope_candidates("graph")]
        # imap_new (新 IMAP) → empty (老 IMAP, outlook.office365.com) → graph
        self.assertEqual(labels[:3], ["imap_new", "empty", "graph_default"])
        # 未列出的兜底 scope 仍保留，避免少试一条可用路径
        self.assertIn("outlook_default", labels)

    def test_configured_graph_priority_keeps_graph_first(self):
        mailbox = OutlookMailbox()
        with mock.patch.dict("os.environ", {"OUTLOOK_BACKEND_PRIORITY": "graph,imap"}):
            labels = [label for label, _scope in mailbox._oauth_scope_candidates("imap")]
        self.assertEqual(labels[0], "graph_default")
        self.assertEqual(labels[1], "imap_new")

    def test_unknown_tokens_are_ignored(self):
        mailbox = OutlookMailbox()
        with mock.patch.dict("os.environ", {"OUTLOOK_BACKEND_PRIORITY": "nonsense"}):
            self.assertEqual(mailbox._outlook_provider_priority(), [])
            labels = [label for label, _scope in mailbox._oauth_scope_candidates("graph")]
        self.assertEqual(labels[0], "graph_default")


if __name__ == "__main__":
    unittest.main()
