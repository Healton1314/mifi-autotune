# -*- coding: utf-8 -*-
"""APK 里特有的那几条路：起不了子进程、日志要能留住。

实测中 下午报的两件事：
  · 「守护模式运行全局体检会报错」—— Chaquopy 里没有 python 可执行文件，
    run_cycle 直接 Popen 出去，必然抛异常。
  · 「日志还在吗」—— 运行日志只存在内存，App 进程一换就没了。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune as A
import server as S

BRIDGE = Path(__file__).resolve().parents[1] / "android" / "app" / "src" / "apkpy" / "bridge.py"


def _load_bridge():
    """按文件路径加载 bridge.py —— 它不在包目录里，import 不到。"""
    spec = importlib.util.spec_from_file_location("mifi_bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
        # lte_only 也一起带过去：守护体检不扫 2.4G 信道（见 run_cycle 的说明）。
        run.assert_called_once_with(args, lte_only=False)
        sp.Popen.assert_not_called()        # 关键：不许再去 Popen

    def test_guard_cycle_skips_channel_scan(self):
        """守护的体检只测 LTE —— 信道是另一件事，扫 5 轮纯属白花时间。"""
        args = mock.Mock(wifi_only=False, lte_only=False, bands="default", ssid=None)
        with mock.patch.object(A, "_can_fork_python", return_value=False), \
                mock.patch.object(A, "_run_cycle_inprocess", return_value=0) as run:
            A.run_cycle(args, lte_only=True)
        run.assert_called_once_with(args, lte_only=True)

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


class BridgeReadsLivePlan(unittest.TestCase):
    """App 的「体检建议」必须读到真实的 plan.json。

    实机 2026-11-03：中午那次完整体检的结论（BAND 3 → BAND 40、实测
    15.7 对 8.8 Mbps、+78%）一次都没弹出来。原因是 bridge.plan() 读的是
    server.HERE —— Chaquopy 的解包目录，而运行期文件早搬到 files/data/ 了，
    那儿根本没有 plan.json。于是界面落到「体检没留下结论，看日志找原因」，
    可日志里结论写得清清楚楚。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _load_bridge()
        self._p = [mock.patch.object(A, "PLAN", self.tmp / "plan.json")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_reads_from_data_dir(self):
        A.PLAN.write_text(json.dumps({"changes": ["频段 BAND 3 → BAND 40"]},
                                     ensure_ascii=False), encoding="utf-8")
        out = json.loads(self.bridge.plan())
        self.assertEqual(out["plan"]["changes"], ["频段 BAND 3 → BAND 40"])

    def test_missing_plan_is_none_not_error(self):
        out = json.loads(self.bridge.plan())
        self.assertIsNone(out["plan"])
        self.assertNotIn("error", out)

    def test_never_uses_the_code_dir(self):
        """钉住写法：整个 bridge 不许再拿 server.HERE 去找运行期文件。

        锚点具体一点会漏 —— 11-03 就是只钉了 plan.json，别处再冒出同样的写法
        照样能过。运行期文件只有 autotune 的那几个全局路径是对的。
        """
        src = BRIDGE.read_text(encoding="utf-8")
        self.assertNotIn("server.HERE", src)
        self.assertIn("autotune.PLAN", src)


if __name__ == "__main__":
    unittest.main()


class DataDir(unittest.TestCase):
    """运行期文件必须放在「升级不会清空」的地方。

    2026-11-02 一天丢了两回：sites.json、config.json 里的 pin_band、刚攒的账本。
    原因是它们写在 Chaquopy 的解包目录里，而那个目录每次装新包都会被整个换掉。
    """

    NAMES = ("DATA_DIR", "STATE", "LOG", "CONFIG", "PLAN", "CHANNEL_ADVICE",
             "SITES", "DECISIONS")

    def setUp(self):
        self.old = {n: getattr(A, n) for n in self.NAMES}
        self.tmp = Path(tempfile.mkdtemp())
        self.old_dir = self.tmp / "code_dir"
        self.new_dir = self.tmp / "files" / "data"
        self.old_dir.mkdir(parents=True)

    def tearDown(self):
        for n, v in self.old.items():
            setattr(A, n, v)

    def test_paths_move(self):
        A.set_data_dir(self.new_dir)
        self.assertEqual(A.DATA_DIR, self.new_dir)
        self.assertEqual(A.SITES, self.new_dir / "sites.json")
        self.assertEqual(A.DECISIONS, self.new_dir / "decisions.json")
        self.assertEqual(A.CONFIG, self.new_dir / "config.json")
        self.assertEqual(A.PLAN, self.new_dir / "plan.json")

    def test_migrates_old_files(self):
        A.set_data_dir(self.old_dir)
        A.SITES.write_text('{"236-CMCC": {"best": 3}}', encoding="utf-8")
        A.CONFIG.write_text('{"pin_band": 3}', encoding="utf-8")
        A.set_data_dir(self.new_dir)
        self.assertEqual(A.SITES.read_text(encoding="utf-8"),
                         '{"236-CMCC": {"best": 3}}')
        self.assertEqual(A.CONFIG.read_text(encoding="utf-8"), '{"pin_band": 3}')

    def test_never_overwrites_existing(self):
        A.set_data_dir(self.new_dir)
        A.SITES.write_text('{"new": 1}', encoding="utf-8")
        A.set_data_dir(self.old_dir)
        (self.old_dir / "sites.json").write_text('{"old": 1}', encoding="utf-8")
        A.set_data_dir(self.new_dir)
        self.assertEqual(A.SITES.read_text(encoding="utf-8"), '{"new": 1}')

    def test_server_audit_log_moves_too(self):
        old = S.AUDIT_LOG
        try:
            S.AUDIT_LOG = self.old_dir / "run-log.txt"
            S.AUDIT_LOG.write_text("旧日志\n", encoding="utf-8")
            S._TAIL = None
            S.set_data_dir(self.new_dir)
            self.assertEqual(S.AUDIT_LOG, self.new_dir / "run-log.txt")
            self.assertIn("旧日志", S.AUDIT_LOG.read_text(encoding="utf-8"))
        finally:
            S.AUDIT_LOG = old
            S._TAIL = None
