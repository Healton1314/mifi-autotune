"""扫描失败时的「病因判断」测试。

背景：用户手机上跑 `mifi --scan`，只拿到一句
    termux-wifi-scaninfo 报错：TimeoutExpired: Command '[...]' timed out after 25 seconds

这等于什么都没说 —— 超时既可能是 Termux:API 这个 App 没装，也可能是没给定位
权限，两者修法完全不同。这些测试锁住「超时 → 先探桥 → 给对应动作」这条逻辑，
以及「第一轮就扫不到就别再等 4 轮」的行为。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner  # noqa: E402

TIMEOUT_ERR = ("TimeoutExpired: Command '['termux-wifi-scaninfo']' "
               "timed out after 60 seconds")


class TermuxHintBase(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"})
        self._env.start()
        scanner._LAST_TERMUX.update(stdout="", stderr="", code=None,
                                    timeout=False, ts=0.0)
        scanner._BRIDGE_CACHE.update(ok=None, ts=0.0)

    def tearDown(self):
        self._env.stop()

    def _which(self, name):
        # 手机上的情形：termux-api 包装了（命令都在），但可能是对的也可能坏的
        return f"/data/data/com.termux/files/usr/bin/{name}"

    def _run_detail(self, cmd, timeout=60, bridge_out=""):
        if cmd[0] == "termux-battery-status":
            return bridge_out, "" if bridge_out else TIMEOUT_ERR, 0 if bridge_out else None
        return "", TIMEOUT_ERR, None


class TestScanHint(TermuxHintBase):
    def test_timeout_with_dead_bridge_blames_missing_app(self):
        """桥也超时 → 直接说「Termux:API 这个 App 没装」，不要绕到权限上。"""
        with mock.patch.object(scanner.shutil, "which", self._which), \
                mock.patch.object(scanner, "run_detail",
                                  lambda cmd, timeout=60: self._run_detail(cmd)):
            hint = scanner.scan_failure_hint()
        self.assertIn("Termux:API", hint)
        self.assertIn("没装", hint)
        # 不能只说「超时了」——用户已经知道超时了
        self.assertNotIn("TimeoutExpired", hint)

    def test_timeout_with_live_bridge_points_at_location_permission(self):
        """桥是通的，只有扫描超时 → 才是权限/扫描本身的问题。"""
        bridge = '{"percentage": 80, "status": "DISCHARGING"}'
        with mock.patch.object(scanner.shutil, "which", self._which), \
                mock.patch.object(scanner, "run_detail",
                                  lambda cmd, timeout=60: self._run_detail(cmd, bridge_out=bridge)):
            hint = scanner.scan_failure_hint()
        self.assertIn("定位", hint)
        self.assertIn("超时", hint)

    def test_bridge_probe_is_cached(self):
        """失败提示 + 原始信息会各问一次，桥探测只能真敲一遍。"""
        calls = []

        def fake(cmd, timeout=60):
            calls.append(cmd[0])
            if cmd[0] == "termux-battery-status":
                return "{}", "", 0
            return "", TIMEOUT_ERR, None

        with mock.patch.object(scanner.shutil, "which", self._which), \
                mock.patch.object(scanner, "run_detail", fake):
            scanner.scan_failure_hint()
            scanner.scan_diagnose()
        self.assertEqual(calls.count("termux-battery-status"), 1)
        # 扫描本身也只该跑一次（结果被复用）
        self.assertEqual(calls.count("termux-wifi-scaninfo"), 1)

    def test_diagnose_shows_bridge_state(self):
        with mock.patch.object(scanner.shutil, "which", self._which), \
                mock.patch.object(scanner, "run_detail",
                                  lambda cmd, timeout=60: self._run_detail(cmd)):
            text = scanner.scan_diagnose()
        self.assertIn("桥探测", text)
        self.assertIn("不通", text)


class TestScanRounds(unittest.TestCase):
    def test_stops_after_first_empty_round(self):
        """第一轮一个 AP 都没有时，不该再傻等剩下 4 轮（手机上就是干等两分钟）。"""
        calls = {"n": 0}

        def empty():
            calls["n"] += 1
            return []

        with mock.patch.object(scanner, "scan_once", empty), \
                mock.patch.object(scanner.time, "sleep", lambda s: None):
            aps = scanner.scan_2g(rounds=5)
        self.assertEqual(aps, [])
        self.assertEqual(calls["n"], 1)

    def test_keeps_merging_when_rounds_have_data(self):
        """有数据时仍然是多轮合并，别把幽灵 AP 过滤掉。"""
        seq = [[{"ssid": "a", "ch": 6, "sig": 60}]]
        calls = {"n": 0}

        def one():
            calls["n"] += 1
            return seq[0]

        with mock.patch.object(scanner, "scan_once", one), \
                mock.patch.object(scanner.time, "sleep", lambda s: None):
            aps = scanner.scan_2g(rounds=3, min_hits=2)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(aps[0]["ssid"], "a")


if __name__ == "__main__":
    unittest.main()
