# -*- coding: utf-8 -*-
"""「这地方从来没这条频段」要记住，下次别再白等 15 秒。

实测中在住处测了十几二十次，BAND 38/41 每次都没信号，却每次都要
等它们各自超时。这条记忆就是那时候加的：连续 DEAD_SKIP_MIN 次「无信号」且
从来没有过信号 → 下次体检直接跳过；但每 DEAD_REPROBE_EVERY 次强制全测一遍，
免得环境变了（新基站、搬家）还一直跳过。

2026-11-03 用户又问「这个功能现在还在吗」—— 那就用测试把它钉住，
顺带钉住「守护触发的体检也走同一条路」。
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import autotune as A


def _no_signal(band):
    return {"band": band, "ok": False, "reason": "no_signal", "mbps": 0,
            "avg": None, "loss": 100, "rsrp": None, "pci": None}


def _ok(band, mbps, rsrp=-90, pci=None):
    return {"band": band, "ok": True, "mbps": mbps, "avg": 80, "loss": 0,
            "rsrp": rsrp, "pci": pci or band * 100}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._ps = [mock.patch.object(A, "SITES", self.tmp / "sites.json"),
                    mock.patch.object(A, "CONFIG", self.tmp / "config.json"),
                    mock.patch.object(A, "DECISIONS", self.tmp / "decisions.json")]
        for p in self._ps:
            p.start()
        A.save_sites({})
        A.save_config({})

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def rec(self, key="350-CMCC"):
        return A.load_sites().get(key)


class DeadCounter(Base):
    def test_no_signal_bumps_dead(self):
        A.record_site("350-CMCC", [_no_signal(38), _no_signal(41)])
        rec = self.rec()
        self.assertEqual(rec["bands"]["38"]["dead"], 1)
        self.assertEqual(rec["bands"]["41"]["dead"], 1)

    def test_signal_resets_dead(self):
        A.record_site("350-CMCC", [_no_signal(38)])
        A.record_site("350-CMCC", [_no_signal(38)])
        self.assertEqual(self.rec()["bands"]["38"]["dead"], 2)
        A.record_site("350-CMCC", [_ok(38, 9.0)])
        v = self.rec()["bands"]["38"]
        self.assertEqual(v["dead"], 0)          # 有信号就清零，别一直压着
        self.assertEqual(v["ok"], 1)

    def test_dead_only_counts_no_signal(self):
        """测速失败、设备超时不算「这里没这条频段」—— 那是这台设备的问题。

        条目会被建出来（dead=0/ok=0 是默认值），但 dead 不许涨，
        否则一次驱动超时就能让这条频段下次被跳过。
        """
        A.record_site("350-CMCC", [{"band": 38, "ok": False,
                                    "reason": "device_error"}])
        self.assertEqual(self.rec()["bands"]["38"]["dead"], 0)

    def test_no_site_key_records_nothing(self):
        A.record_site("", [_no_signal(38)])
        self.assertEqual(A.load_sites(), {})


class BandPlanSkip(Base):
    def _seen(self, n):
        """把 seen 调到 n，模拟来过 n 次（避开 % 5 == 0 的抽查轮）。"""
        data = A.load_sites()
        data["350-CMCC"]["seen"] = n
        A.save_sites(data)

    def test_skips_after_three_dead(self):
        for _ in range(A.DEAD_SKIP_MIN):
            A.record_site("350-CMCC", [_no_signal(38), _ok(3, 8.0)])
        self._seen(3)
        bands, dead, slow, best = A.band_plan(
            self.rec(), [3, 8, 38, 39, 40, 41], serving=3)
        self.assertIn(38, dead)
        self.assertNotIn(38, bands)
        self.assertIn(3, bands)

    def test_two_dead_is_not_enough(self):
        for _ in range(A.DEAD_SKIP_MIN - 1):
            A.record_site("350-CMCC", [_no_signal(38), _ok(3, 8.0)])
        self._seen(2)
        bands, dead, slow, best = A.band_plan(
            self.rec(), [3, 8, 38], serving=3)
        self.assertEqual(dead, [])
        self.assertIn(38, bands)

    def test_band_that_once_had_signal_is_never_skipped(self):
        data = {"350-CMCC": {"seen": 9, "last": datetime.now().isoformat(timespec="seconds"),
                             "bands": {"38": {"dead": 9, "ok": 1, "best": 6.0}}}}
        bands, dead, slow, best = A.band_plan(data["350-CMCC"], [3, 38], serving=3)
        self.assertEqual(dead, [])
        self.assertIn(38, bands)

    def test_every_fifth_visit_retests_everything(self):
        for _ in range(A.DEAD_SKIP_MIN):
            A.record_site("350-CMCC", [_no_signal(38), _ok(3, 8.0)])
        # seen 正好是 DEAD_REPROBE_EVERY 的整数倍 → 抽查这一轮全测
        self._seen(A.DEAD_REPROBE_EVERY)
        bands, dead, slow, best = A.band_plan(
            self.rec(), [3, 8, 38], serving=3)
        self.assertEqual(dead, [])
        self.assertIn(38, bands)

    def test_stale_memory_skips_nothing(self):
        old = (datetime.now() - timedelta(days=A.SITE_STALE_DAYS + 1)
               ).isoformat(timespec="seconds")
        rec = {"seen": 9, "last": old,
               "bands": {"38": {"dead": 9, "ok": 0}}}
        bands, dead, slow, best = A.band_plan(rec, [3, 38], serving=3)
        self.assertEqual(dead, [])
        self.assertEqual(bands, [3, 38])


class GuardCheckupSharesTheSamePath(unittest.TestCase):
    """守护自己触发的体检，必须和手动体检记同一本账。

    守护走 run_cycle(args, lte_only=True) → _run_cycle_inprocess →
    同一套 main()，所以 band_plan 读的、record_site 写的都是同一个 sites.json。
    这里钉住的是「参数没有把它带偏」：bands 保持 default 才记账
    （main 里那句 `if args.bands in ("default", "all")`）。
    """

    def test_guard_argv_keeps_bands_default(self):
        args = mock.Mock(wifi_only=False, lte_only=False,
                         bands="default", ssid=None)
        seen = {}

        def fake_main():
            seen["argv"] = list(__import__("sys").argv)

        with mock.patch.object(A, "main", side_effect=fake_main):
            A._run_cycle_inprocess(args, lte_only=True)
        argv = seen["argv"]
        self.assertNotIn("--bands", argv)        # 默认就要默认，别写成显式列表
        self.assertIn("--lte-only", argv)
        self.assertIn("--apply", argv)

    def test_explicit_bands_would_skip_recording(self):
        """反证：显式 --bands 时体检不记账，所以守护绝不能带这个参数。"""
        import inspect
        src = inspect.getsource(A.run_preview)
        self.assertIn('args.bands in ("default", "all")', src)


if __name__ == "__main__":
    unittest.main()
