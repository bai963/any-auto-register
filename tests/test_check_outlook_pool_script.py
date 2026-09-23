"""池体检脚本的统计逻辑（不联网）。"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_outlook_pool.py"
_spec = importlib.util.spec_from_file_location("check_outlook_pool", _SCRIPT)
check_outlook_pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_outlook_pool)


class PoolSummaryTests(unittest.TestCase):
    def test_counts_each_capability(self):
        summary = check_outlook_pool.summarize([
            {"ok": True, "capability": "graph"},
            {"ok": True, "capability": "imap"},
            {"ok": True, "capability": "imap"},
            {"ok": True, "capability": "mailapi"},
            {"ok": False, "capability": ""},
        ])
        self.assertEqual(
            summary,
            {"total": 5, "graph": 1, "imap": 2, "mailapi": 1, "unavailable": 1},
        )

    def test_ok_without_capability_counts_as_unavailable(self):
        summary = check_outlook_pool.summarize([{"ok": True, "capability": ""}])
        self.assertEqual(summary["unavailable"], 1)
        self.assertEqual(summary["graph"] + summary["imap"] + summary["mailapi"], 0)


if __name__ == "__main__":
    unittest.main()
