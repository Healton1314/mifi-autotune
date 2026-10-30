# -*- coding: utf-8 -*-
"""位置记忆：同一个地方第二次起，别再把没覆盖的频段重测一遍。

用户实机（2026-10-30 宿舍）：测了十几二十次，BAND 38/41 每次都没信号，
每次都白等 15 秒；剩下的频段也是从头重来。工具应该记住「这个地方哪些频段
从来收不到」，下次直接跳过 —— 但每 DEAD_REPROBE_EVERY 次强制全测一次，
免得环境变了（比如换了运营商、装了信号放大器）还一直跳过。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import autotune as A


def ago(**kw):
    return (datetime.now() - timedelta(**kw)).isoformat(timespec="seconds")


class SiteKey(unittest.TestCase):
    def test_pci_and_operator(self):
        st = {"signal": {"pci": "236", "operator": "CMCC"}}
        self.assertEqual(A._site_key(st), "236-CMCC")

    def test_no_pci_means_no_memory(self):
        # 读不到 PCI 就不记 —— 宁可这次不记忆，也不能把两个地方混成一个
        self.assertEqual(A._site_key({"signal": {}}), "")
        self.assertEqual(A._site_key({}), "")

    def test_missing_operator_still_works(self):
        self.assertEqual(A._site_key({"signal": {"pci": "12"}}), "12-?")


class MemoryAge(unittest.TestCase):
    """记忆按时间打折：太久没来的位置不能再拿旧结论跳着测。"""

    def test_age_hours(self):
        self.assertAlmostEqual(A.site_age_hours({"last": ago(hours=5)}), 5, delta=0.1)

    def test_age_unknown_when_no_timestamp(self):
        self.assertIsNone(A.site_age_hours({"seen": 3}))
        self.assertIsNone(A.site_age_hours({"last": "昨天下午"}))

    def test_future_timestamp_does_not_go_negative(self):
        # 系统时间被回拨过：未来时间戳不该算成「负年龄」再被判新鲜
        self.assertEqual(A.site_age_hours({"last": ago(hours=-3)}), 0.0)

    def test_states(self):
        self.assertEqual(A.site_memory_state(None), "none")
        self.assertEqual(A.site_memory_state({"last": ago(hours=2)}), "fresh")
        self.assertEqual(A.site_memory_state({"last": ago(days=4)}), "aging")
        self.assertEqual(A.site_memory_state({"last": ago(days=9)}), "stale")
        self.assertEqual(A.site_memory_state({"last": ""}), "stale")

    def test_fmt_age(self):
        self.assertEqual(A._fmt_age(None), "时间未知")
        self.assertEqual(A._fmt_age(0.4), "不到 1 小时前")
        self.assertEqual(A._fmt_age(5.2), "5 小时前")
        self.assertEqual(A._fmt_age(24 * 9), "9 天前")


class BandPlan(unittest.TestCase):
    def _rec(self, bands, seen=3, best=None, last=None):
        # last 缺省算「刚刚来过」：真实记录里 record_site 一定会写时间。
        return {"seen": seen, "bands": bands, "best": best,
                "last": last if last is not None else ago(minutes=10)}

    def test_no_record_measures_everything(self):
        keep, dead, slow, best = A.band_plan(None, [3, 8, 38, 39, 40, 41])
        self.assertEqual(keep, [3, 8, 38, 39, 40, 41])
        self.assertEqual(dead, [])
        self.assertEqual(slow, [])
        self.assertIsNone(best)

    def test_dead_bands_are_skipped(self):
        rec = self._rec({"38": {"dead": 12, "ok": 0}, "41": {"dead": 12, "ok": 0},
                         "40": {"dead": 0, "ok": 5}})
        keep, dead, _, _ = A.band_plan(rec, [3, 8, 38, 39, 40, 41])
        self.assertEqual(dead, [38, 41])
        self.assertEqual(keep, [3, 8, 39, 40])

    def test_band_that_worked_once_is_never_skipped(self):
        # 只死过 5 次但从成功过 1 次 → 仍然要测（说明那里是有覆盖的）
        rec = self._rec({"38": {"dead": 5, "ok": 1}})
        keep, dead, _, _ = A.band_plan(rec, [38, 40])
        self.assertEqual(dead, [])
        self.assertIn(38, keep)

    def test_below_threshold_still_measured(self):
        rec = self._rec({"38": {"dead": A.DEAD_SKIP_MIN - 1, "ok": 0}})
        _, dead, _, _ = A.band_plan(rec, [38])
        self.assertEqual(dead, [])

    def test_periodic_full_recheck(self):
        # 第 5 次强制全测，避免「环境变了还在跳过」
        rec = self._rec({"38": {"dead": 9, "ok": 0}}, seen=A.DEAD_REPROBE_EVERY)
        keep, dead, slow, _ = A.band_plan(rec, [38, 40])
        self.assertEqual(dead, [])
        self.assertEqual(slow, [])
        self.assertEqual(keep, [38, 40])

    def test_best_band_goes_first(self):
        # 前两名差距不大（1.02 倍）→ 不触发快速模式，全部都要测，只是顺序变
        rec = self._rec({"40": {"dead": 0, "ok": 5, "best": 30.0},
                         "3": {"dead": 0, "ok": 3, "best": 29.5},
                         "8": {"dead": 0, "ok": 3, "best": 6.0},
                         "39": {"dead": 0, "ok": 3, "best": 8.0}}, best=40)
        keep, _, slow, best = A.band_plan(rec, [3, 8, 39, 40], serving=40)
        self.assertEqual(keep[0], 40)
        self.assertEqual(best, 40)
        self.assertEqual(sorted(keep), [3, 8, 39, 40])   # 一个都不能丢
        self.assertEqual(slow, [])

    def test_fast_mode_skips_much_slower_bands(self):
        # 真实数据（2026-10-30）：43.2 / 34.5 / 10.7 / 8.2 → 只要 40 和 3
        rec = self._rec({"40": {"dead": 0, "ok": 5, "best": 43.2},
                         "3": {"dead": 0, "ok": 3, "best": 34.5},
                         "8": {"dead": 0, "ok": 3, "best": 10.7},
                         "39": {"dead": 0, "ok": 3, "best": 8.2}}, best=40, seen=3)
        keep, _, slow, _ = A.band_plan(rec, [3, 8, 39, 40], serving=40)
        self.assertEqual(keep, [40, 3])
        self.assertEqual(slow, [8, 39])

    def test_fast_mode_off_on_first_visit(self):
        # 只来过一次，历史不可信，必须全测
        rec = self._rec({"40": {"dead": 0, "ok": 1, "best": 43.0},
                         "8": {"dead": 0, "ok": 1, "best": 5.0}}, best=40, seen=1)
        keep, _, slow, _ = A.band_plan(rec, [3, 8, 39, 40], serving=40)
        self.assertEqual(sorted(keep), [3, 8, 39, 40])
        self.assertEqual(slow, [])

    def test_fast_mode_off_on_full_recheck(self):
        # 第 5 次强制全测，别让「快速」变成「永远不测」
        rec = self._rec({"40": {"dead": 0, "ok": 5, "best": 43.0},
                         "8": {"dead": 0, "ok": 5, "best": 5.0}}, best=40,
                        seen=A.DEAD_REPROBE_EVERY)
        keep, _, slow, _ = A.band_plan(rec, [3, 8, 39, 40], serving=40)
        self.assertEqual(sorted(keep), [3, 8, 39, 40])

    def test_stale_record_measures_everything(self):
        # 用户 2026-10-30：以前常去、隔了很久没去 → 记忆一条都不认，全部重测
        rec = self._rec({"38": {"dead": 12, "ok": 0},
                         "40": {"dead": 0, "ok": 5, "best": 43.0},
                         "3": {"dead": 0, "ok": 5, "best": 6.0}}, best=40,
                        seen=12, last=ago(days=9))
        keep, dead, slow, best = A.band_plan(rec, [3, 8, 38, 39, 40, 41], serving=40)
        self.assertEqual(keep, [3, 8, 38, 39, 40, 41])   # 死频段也要重测
        self.assertEqual(dead, [])
        self.assertEqual(slow, [])
        self.assertIsNone(best)                          # 旧的最优也不认了

    def test_aging_keeps_coverage_but_retests_speed(self):
        # 隔了 4 天：覆盖（有没有信号）还算稳定，速度必须重测
        rec = self._rec({"38": {"dead": 12, "ok": 0},
                         "40": {"dead": 0, "ok": 5, "best": 43.0},
                         "3": {"dead": 0, "ok": 5, "best": 34.0},
                         "8": {"dead": 0, "ok": 5, "best": 10.0},
                         "39": {"dead": 0, "ok": 5, "best": 8.0}}, best=40,
                        seen=6, last=ago(days=4))
        keep, dead, slow, _ = A.band_plan(rec, [3, 8, 38, 39, 40], serving=40)
        self.assertEqual(dead, [38])
        self.assertEqual(sorted(keep), [3, 8, 39, 40])   # 快速模式必须关掉
        self.assertEqual(slow, [])

    def test_missing_timestamp_is_treated_as_stale(self):
        # 老版本/手改的记录没有 last：宁可多测一遍，也别拿未知年龄的历史跳着测
        rec = {"seen": 9, "best": 40, "bands": {"38": {"dead": 9, "ok": 0},
                                                "40": {"dead": 0, "ok": 9, "best": 43.0},
                                                "8": {"dead": 0, "ok": 9, "best": 5.0}}}
        keep, dead, slow, _ = A.band_plan(rec, [3, 8, 38, 40], serving=40)
        self.assertEqual(keep, [3, 8, 38, 40])
        self.assertEqual((dead, slow), ([], []))


class RecordSite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_counts_dead_and_ok(self):
        A.record_site("236-CMCC", [
            {"band": 38, "ok": False, "reason": "no_signal"},
            {"band": 40, "ok": True, "mbps": 31.5},
        ], best_band=40)
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        rec = data["236-CMCC"]
        self.assertEqual(rec["seen"], 1)
        self.assertEqual(rec["bands"]["38"]["dead"], 1)
        self.assertEqual(rec["bands"]["40"]["ok"], 1)
        self.assertEqual(rec["best"], 40)

    def test_ok_resets_dead_counter(self):
        # 之前一直死，这次通了 → 死亡计数必须清零，否则下次还会被跳
        A.record_site("k", [{"band": 38, "ok": False, "reason": "no_signal"}])
        A.record_site("k", [{"band": 38, "ok": True, "mbps": 5}])
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["k"]
        self.assertEqual(rec["bands"]["38"]["dead"], 0)
        self.assertEqual(rec["bands"]["38"]["ok"], 1)

    def test_no_key_does_not_write(self):
        A.record_site("", [{"band": 40, "ok": True, "mbps": 10}])
        self.assertFalse(self.tmp.exists())

    def test_seen_accumulates(self):
        for _ in range(3):
            A.record_site("k", [{"band": 40, "ok": True, "mbps": 10}])
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["k"]
        self.assertEqual(rec["seen"], 3)

    def test_best_is_max_by_default(self):
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 43}])
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 12}])
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["k"]
        self.assertEqual(rec["bands"]["40"]["best"], 43)

    def test_reset_best_replaces_old_record(self):
        # 记忆过期后那次是全测：历史那个偏高的 43 必须让位给这次的 12，
        # 否则下次 fresh 档的快速模式还会被旧数字骗一次。
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 43}])
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 12}], reset_best=True)
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["k"]
        self.assertEqual(rec["bands"]["40"]["best"], 12)
        self.assertEqual(rec["seen"], 2)      # 来过几次仍然照实累计


class PruneSites(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_drops_stale_sites(self):
        self.tmp.write_text(json.dumps({
            "new": {"seen": 1, "last": "2026-10-30T10:00:00"},
            "old": {"seen": 9, "last": "2020-01-01T00:00:00"},
        }), encoding="utf-8")
        data, kept = A.prune_sites(days=30)
        self.assertIn("new", kept)
        self.assertNotIn("old", kept)
        self.assertEqual(len(data), 2)


class PreviewUsesSiteMemory(unittest.TestCase):
    """走一遍真实入口：确认 run_preview 真的会清旧位置、并按记忆年龄决定测几个。"""

    class FakeDev:
        name = "fake"
        description = "假设备"
        band = 0
        channel = 6

        def set_band(self, band):
            self.band = band

        def get_state(self):
            return {"band": self.band, "channel": self.channel,
                    "signal": {"rsrp": "-90dBm", "pci": "236", "operator": "CMCC",
                               "band": "3"}}

        def capabilities(self):
            return {"bands": [3, 8, 38, 40], "channels": list(range(1, 14)),
                    "can_lock_band": True, "can_set_channel": True}

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.sites = tmp / "sites.json"
        self.plan = tmp / "plan.json"
        self._p = [mock.patch.object(A, "SITES", self.sites),
                   mock.patch.object(A, "PLAN", self.plan)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _run(self, rec):
        self.sites.write_text(json.dumps({"236-CMCC": rec,
                                          "999-OLD": {"seen": 4,
                                                      "last": "2019-01-01T00:00:00"}}),
                              encoding="utf-8")
        measured = []

        def fake_measure(dev, band, **kw):
            measured.append(band)
            return {"band": band, "ok": True, "mbps": 20, "mbps_all": [20],
                    "mbps_min": 20, "mbps_max": 20, "avg": 50, "max": 60,
                    "jitter": 5, "loss": 0, "rounds": 1}

        args = type("Args", (), dict(wifi_only=False, lte_only=True, rounds=1,
                                     bands="default", ssid=None, scan_rounds=1,
                                     fresh=False))()
        dev = self.FakeDev()
        with mock.patch.object(A, "measure_band", fake_measure), \
                mock.patch.object(A.time, "sleep", lambda s: None):
            A.run_preview(dev, dev.capabilities(), dev.get_state(), args, {}, None, None)
        return measured, json.loads(self.sites.read_text(encoding="utf-8"))

    def test_fresh_memory_skips_dead_bands(self):
        measured, _ = self._run({"seen": 3, "best": 40, "last": ago(minutes=30),
                                 "bands": {"38": {"dead": 5, "ok": 0},
                                           "40": {"dead": 0, "ok": 3, "best": 43},
                                           "3": {"dead": 0, "ok": 3, "best": 42}}})
        self.assertNotIn(38, measured)      # 这个位置从来没信号 → 跳过

    def test_stale_memory_measures_everything(self):
        measured, data = self._run({"seen": 12, "best": 40, "last": ago(days=20),
                                    "bands": {"38": {"dead": 12, "ok": 0},
                                              "40": {"dead": 0, "ok": 12, "best": 43},
                                              "3": {"dead": 0, "ok": 12, "best": 42}}})
        self.assertEqual(sorted(set(measured)), [3, 8, 38, 40])   # 20 天没来 → 全测
        self.assertNotIn("999-OLD", data)                   # 3 年前的位置被清掉
        self.assertIn("236-CMCC", data)


if __name__ == "__main__":
    unittest.main()
