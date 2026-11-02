# -*- coding: utf-8 -*-
"""APK 里特有的那几条路：起不了子进程、日志要能留住。

实测中 下午报的两件事：
  · 「守护模式运行全局体检会报错」—— Chaquopy 里没有 python 可执行文件，
    run_cycle 直接 Popen 出去，必然抛异常。
  · 「日志还在吗」—— 运行日志只存在内存，App 进程一换就没了。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune as A
import server as S


class NoFork(unittest.TestCase):
    def test_android_cannot_fork(self):
        # Chaquopy 才有 getandroidapilevel；有它就别指望能起 python 子进程
        with mock.patch.object(sys, "getandroidapilevel", lambda: 34, create=True):
            self.assertFalse(A._can_fork_python())

    def test_desktop_can_fork(self):
        self.assertTrue(A._can_fork_python())

    def test_cycle_goes_inprocess_when_cannot_fork(self):
        args = mock.Mock(wifi_only=False, lte_only=False, bands="default", ssid=None)
        with mock.patch.object(A, "_can_fork_python", return_value=False), \
                mock.patch.object(A, "subprocess") as sp, \
                mock.patch.object(A, "_run_cycle_inprocess", return_value=0) as run:
            self.assertEqual(A.run_cycle(args), 0)
        run.assert_called_once_with(args)
        sp.Popen.assert_not_called()        # 关键：不许再去 Popen

    def test_inprocess_argv_matches_cli(self):
        args = mock.Mock(wifi_only=False, lte_only=True, bands="3,8", ssid="X")
        seen = {}

        def fake_main():
            seen["argv"] = list(sys.argv)

        with mock.patch.object(A, "main", side_effect=fake_main):
            self.assertEqual(A._run_cycle_inprocess(args), 0)
        argv = seen["argv"]
        self.assertIn("--lte-only", argv)
        self.assertIn("--bands", argv)
        self.assertEqual(argv[argv.index("--bands") + 1], "3,8")
        self.assertEqual(argv[-1], "--apply")
        self.assertNotIn("--wifi-only", argv)
        # 跑完要把 argv 还回去，否则同一个进程里后面所有解析都乱
        self.assertNotEqual(sys.argv[0].endswith("mifi-autotune"), True)

    def test_inprocess_failure_returns_code(self):
        args = mock.Mock(wifi_only=False, lte_only=False, bands="default", ssid=None)
        with mock.patch.object(A, "main", side_effect=RuntimeError("体检炸了")):
            self.assertEqual(A._run_cycle_inprocess(args), 1)

    def test_inprocess_systemexit_is_passed_through(self):
        args = mock.Mock(wifi_only=False, lte_only=False, bands="default", ssid=None)
        with mock.patch.object(A, "main", side_effect=SystemExit(2)):
            self.assertEqual(A._run_cycle_inprocess(args), 2)


class VerifyPathIsRecorded(unittest.TestCase):
    """「切到记忆里那条实测」也是一次真锁定，必须进账本。

    实测：中午吃饭锁过 BAND 39，账本里没有 —— 走的就是这条没带 trace 的路。
    """

    def test_verify_passes_trace_with_kind(self):
        dev = mock.Mock()
        dev.get_state.return_value = {"signal": {"rsrp": -95}}
        trace = {"_key": "192-CMCC", "t": "2026-11-02T12:23:00",
                 "kind": "blind", "serving": 3, "pre": {"loss": 0, "avg": 90}}
        calls = []

        def fake_lock(d, band, trace=None):
            calls.append((band, dict(trace or {})))
            return False                    # 切不过去就返回 False，不再往下走

        with mock.patch.object(A, "_guard_lock_band", side_effect=fake_lock):
            A._guard_verify_memo(dev, 39, 3, "192-CMCC", None, "1.1.1.1", trace=trace)
        self.assertEqual(calls[0][0], 39)
        self.assertEqual(calls[0][1]["kind"], "verify")
        self.assertEqual(calls[0][1]["_key"], "192-CMCC")

    def test_without_trace_is_still_fine(self):
        dev = mock.Mock()
        dev.get_state.return_value = {"signal": {}}
        with mock.patch.object(A, "_guard_lock_band", return_value=False) as lock:
            A._guard_verify_memo(dev, 39, 3, "192-CMCC", None, "1.1.1.1")
        self.assertIsNone(lock.call_args.kwargs.get("trace"))


class LogSurvivesRestart(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.AUDIT = d / "run-log.txt"
        self._ps = [mock.patch.object(S, "AUDIT_LOG", self.AUDIT)]
        for p in self._ps:
            p.start()
        # 让 _tail 重新按新路径初始化
        S._TAIL = None

    def tearDown(self):
        for p in self._ps:
            p.stop()
        S._TAIL = None

    def test_writes_and_reads_back(self):
        S.log("11:00  锁定 BAND 3")
        S.log("11:30  监控中")
        self.assertTrue(self.AUDIT.exists())
        self.assertEqual(S.saved_log()[-2:], ["11:00  锁定 BAND 3", "11:30  监控中"])

    def test_new_process_continues_old_file(self):
        """App 重开 = 新进程，内存为空；磁盘那份要能接上。"""
        S.log("第一天的日志")
        S._TAIL = None                      # 模拟进程重启
        S.log("第二天的日志")
        rows = S.saved_log()
        self.assertIn("第一天的日志", rows)
        self.assertEqual(rows[-1], "第二天的日志")

    def test_caps_the_file(self):
        for i in range(S.AUDIT_KEEP + 20):
            S.log(f"行 {i}")
        rows = self.AUDIT.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), S.AUDIT_KEEP)
        self.assertEqual(rows[-1], f"行 {S.AUDIT_KEEP + 19}")


if __name__ == "__main__":
    unittest.main()
