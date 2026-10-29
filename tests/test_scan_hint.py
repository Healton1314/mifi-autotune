"""扫描失败时的「病因判断」测试。

背景：用户手机上跑 `mifi --scan`，只拿到一句
    termux-wifi-scaninfo 报错：TimeoutExpired: Command '[...]' timed out after 25 seconds

这等于什么都没说 —— 超时既可能是 Termux:API 这个 App 没装，也可能是没给定位
权限，两者修法完全不同。这些测试锁住「超时 → 先探桥 → 给对应动作」这条逻辑，
以及「第一轮就扫不到就别再等 4 轮」的行为。
"""

import os
import sys
import time
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


class TermuxFieldNames(unittest.TestCase):
    """Termux:API 有两个版本的 JSON 字段名，必须都认。

    2026-10-29 用户实机：退出码 0、API 桥通，但工具说「一个都没扫到」。
    真因是代码只读 `frequency` / `level`，而这台机器返回的是
    `frequency_mhz` / `rssi` —— 每条记录都被当成「没有频率」丢掉了。
    这个 bug 的表现和「没权限」一模一样，所以必须用真实返回锁住。
    """

    def _parse(self, payload):
        import json
        with mock.patch.object(scanner, "run_detail",
                               lambda *a, **k: (json.dumps(payload), "", 0)), \
                mock.patch.object(scanner.shutil, "which", lambda n: "/usr/bin/" + n):
            return scanner._scan_termux()

    def test_new_style_fields(self):
        now = time.time() * 1000
        aps = self._parse([
            {"bssid": "aa:bb", "ssid": "月亮糖", "frequency_mhz": 2437,
             "rssi": -55, "timestamp": now},
            {"bssid": "cc:dd", "ssid": "邻居", "frequency_mhz": 2462,
             "rssi": -70, "timestamp": now},
        ])
        self.assertEqual(len(aps), 2)
        self.assertEqual(aps[0]["ch"], 6)
        self.assertEqual(aps[1]["ch"], 11)
        self.assertGreater(aps[0]["sig"], aps[1]["sig"])   # -55dBm 强于 -70dBm

    def test_old_style_fields_still_work(self):
        now = time.time() * 1000
        aps = self._parse([{"bssid": "ee:ff", "ssid": "老格式", "frequency": 2412,
                            "level": -60, "timestamp": now}])
        self.assertEqual(len(aps), 1)
        self.assertEqual(aps[0]["ch"], 1)


class StaleCacheHint(unittest.TestCase):
    """只有缓存时的提示必须点名「缓存」，不能猜成「只返回当前连接的」。"""

    def _hint(self, payload):
        import json
        scanner._LAST_TERMUX.update(stdout=json.dumps(payload), stderr="", code=0,
                                    ts=time.time())
        return scanner._describe_termux_records()

    def test_2022_record_is_called_cache(self):
        # 用户截图里的原样数据
        text = self._hint([{"bssid": "ec.b9:70:dd:c6:13", "frequency_mhz": 5200,
                            "rssi": -83, "ssid": "", "timestamp": 1670139781770,
                            "channel_bandwidth_mhz": "40"}])
        self.assertIn("缓存", text)
        self.assertIn("位置信息", text)

    def test_fresh_5g_only_points_at_band(self):
        text = self._hint([{"bssid": "aa", "frequency_mhz": 5200, "rssi": -60,
                            "ssid": "5G网", "timestamp": time.time() * 1000}])
        self.assertIn("只扫到 5GHz", text)
        self.assertNotIn("缓存", text)


if __name__ == "__main__":
    unittest.main()