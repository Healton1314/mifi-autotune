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
        serving = "3"
        pci = "236"

        def set_band(self, band):
            self.band = band

        def get_state(self):
            return {"band": self.band, "channel": self.channel,
                    "signal": {"rsrp": "-90dBm", "pci": self.pci, "operator": "CMCC",
                               "band": self.serving}}

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

    def _run(self, rec, speeds=None, serving="3", pci="236"):
        self.sites.write_text(json.dumps({"236-CMCC": rec,
                                          "999-OLD": {"seen": 4,
                                                      "last": "2019-01-01T00:00:00"}}),
                              encoding="utf-8")
        measured = []
        speeds = speeds or {}

        def fake_measure(dev, band, **kw):
            measured.append(band)
            mbps = speeds.get(band, 20)
            return {"band": band, "ok": True, "mbps": mbps, "mbps_all": [mbps],
                    "mbps_min": mbps, "mbps_max": mbps, "avg": 50, "max": 60,
                    "jitter": 5, "loss": 0, "rounds": 1}

        args = type("Args", (), dict(wifi_only=False, lte_only=True, rounds=1,
                                     bands="default", ssid=None, scan_rounds=1,
                                     fresh=False))()
        dev = self.FakeDev()
        dev.serving = serving
        dev.pci = pci
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

    def test_pci_change_after_band_switch_reuses_memory(self):
        """切过频段 PCI 变了，但人没动：仍然沿用同一份记忆（不白等死频段）。"""
        measured, data = self._run({"seen": 3, "best": 40, "last": ago(minutes=5),
                                    "bands": {"38": {"dead": 5, "ok": 0},
                                              "40": {"dead": 0, "ok": 3, "best": 43},
                                              "3": {"dead": 0, "ok": 3, "best": 42}}},
                                   pci="350")     # 设备现在报 350，记忆存在 236 上
        self.assertNotIn(38, measured)
        self.assertEqual(list(data), ["236-CMCC"])
        self.assertIn("350-CMCC", data["236-CMCC"]["aliases"])

    def test_flappy_band_is_demoted_for_a_close_alternative(self):
        """走真实入口：BAND 3 更快但历史老掉包，BAND 40 只慢一点 → 选 40 并说明。"""
        bad = [[12, 46, 55, 90], [12, 45, 40, 88], [12, 44, 0, 70]]
        _, _ = self._run({"seen": 6, "best": 3, "last": ago(hours=2),
                          "bands": {"3": {"dead": 0, "ok": 6, "best": 46, "samples": bad},
                                    "40": {"dead": 0, "ok": 6, "best": 42,
                                           "samples": [[12, 41, 0, 60], [12, 42, 0, 58]]}}},
                         speeds={3: 46.0, 40: 41.0, 8: 10.0}, serving="8")
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(plan["target"]["band"], 40)
        self.assertIn("丢包", plan["flap"])


class RollingSamples(unittest.TestCase):
    """滚动样本：记住「几点、多快、丢包多少」，用来发现看不见的周期性掉包。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _samples(self, key="k", band="40"):
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))[key]
        return rec["bands"][band]["samples"]

    def test_sample_has_hour_speed_loss_ping(self):
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 31.5,
                             "loss": 40, "avg": 88, "rsrp": -77}])
        hour, mbps, loss, ping, rsrp = self._samples()[0]
        self.assertEqual(mbps, 31.5)
        self.assertEqual((loss, ping), (40, 88))
        self.assertEqual(hour, datetime.now().hour)   # 跨时段比较要用
        self.assertEqual(rsrp, -77)                   # 锁频段前要比信号

    def test_rsrp_median_survives_old_samples(self):
        # 老样本只有 4 位（没有 RSRP）。它们不能参与 RSRP 中位数 ——
        # 拿「没记录」当 0dBm 会把信号算成神仙级别。
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 10, "loss": 0,
                             "avg": 50, "rsrp": -100}])
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        data["k"]["bands"]["40"]["samples"].insert(0, [9, 12.0, 0, 40])   # 老格式
        self.tmp.write_text(json.dumps(data), encoding="utf-8")
        A.record_site("k", [{"band": 40, "ok": True, "mbps": 20, "loss": 0,
                             "avg": 50, "rsrp": -80}])
        v = json.loads(self.tmp.read_text(encoding="utf-8"))["k"]["bands"]["40"]
        self.assertEqual(v["rsrp"], -80)
        self.assertEqual(v["rsrp_med"], -90)          # median(-100, -80)
        self.assertEqual(len(v["samples"][0]), 4)     # 老数据没被改写

    def test_samples_are_capped(self):
        # 只留最近几次：文件不能随运行次数一直长
        for i in range(20):
            A.record_site("k", [{"band": 40, "ok": True, "mbps": i, "loss": 0, "avg": 50}])
        rows = self._samples()
        self.assertEqual(len(rows), A.SITE_SAMPLES)
        self.assertEqual(rows[-1][1], 19)             # 最新的在最后

    def test_no_signal_does_not_add_sample(self):
        # 没信号是「覆盖」问题，dead 计数管；样本只记测得出速度的那次
        A.record_site("k", [{"band": 38, "ok": False, "reason": "no_signal"}])
        self.assertNotIn("samples", json.loads(
            self.tmp.read_text(encoding="utf-8"))["k"]["bands"]["38"])


class FlapDetection(unittest.TestCase):
    def _v(self, losses):
        return {"samples": [[12, 30.0, loss, 60] for loss in losses]}

    def test_periodic_loss_is_detected(self):
        # 宿舍实测：BAND 40 三五分钟掉一次，54 秒的体检根本碰不到
        flap, ratio, n = A.band_flap(self._v([0, 45, 0, 50, 0]))
        self.assertTrue(flap)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(ratio, 0.4)

    def test_occasional_loss_is_not_flap(self):
        flap, _, _ = A.band_flap(self._v([0, 0, 0, 30, 0]))
        self.assertFalse(flap)          # 5 次里 1 次，还不够格

    def test_single_bad_sample_never_convicts(self):
        """3 条里 1 条坏 = 33%，刚过占比线，但只有 1 次 → 不判掉包。

        2026-10-31 实测发现的敏感度问题：守护只要连续两分钟丢包 ≥20% 就写
        一条坏样本，而样本池只有 8 条。若只看占比，隔壁开一会儿微波炉、
        电梯里过一下留下的那一条，就能把一条好频段打成「会周期性掉包」；
        洗掉它又要等 8 次体检（守护链路正常时不写样本）。一次偶发不能定罪。
        """
        flap, ratio, n = A.band_flap(self._v([0, 0, 40]))
        self.assertFalse(flap)
        self.assertAlmostEqual(ratio, 1 / 3)
        self.assertEqual(n, 3)

    def test_two_bad_samples_do_convict(self):
        # 反过来也要保住：真的反复掉包，样本再少也得抓出来，不能因为加门槛
        # 就把「今晚这事」放过去（BAND 40 两条样本都 100% 丢包）。
        flap, ratio, n = A.band_flap(self._v([100, 100]))
        self.assertTrue(flap)
        self.assertEqual(ratio, 1.0)
        self.assertEqual(n, 2)

    def test_single_sample_is_not_enough(self):
        flap, _, n = A.band_flap(self._v([60]))
        self.assertFalse(flap)
        self.assertEqual(n, 1)

    def test_missing_or_broken_samples(self):
        self.assertFalse(A.band_flap({})[0])
        self.assertFalse(A.band_flap({"samples": [[1, 2]]})[0])   # 短数组忽略


class PreferStable(unittest.TestCase):
    def _usable(self, *pairs):
        return [{"band": b, "mbps": m, "avg": 50} for b, m in pairs]

    def test_swaps_when_alternative_is_close(self):
        hist = {"3": {"samples": [[12, 46, 50, 90], [12, 45, 40, 88], [12, 44, 0, 70]]},
                "40": {"samples": [[12, 41, 0, 60], [12, 42, 0, 58]]}}
        best, note = A.prefer_stable_over_flap(self._usable((3, 46.0), (40, 41.0)), hist, 3)
        self.assertEqual(best, 40)
        self.assertIn("丢包", note)

    def test_keeps_fast_one_when_alternative_is_much_slower(self):
        hist = {"3": {"samples": [[12, 46, 50, 90], [12, 45, 40, 88], [12, 44, 0, 70]]},
                "8": {"samples": [[12, 10, 0, 60], [12, 11, 0, 58]]}}
        best, note = A.prefer_stable_over_flap(self._usable((3, 46.0), (8, 10.7)), hist, 3)
        self.assertEqual(best, 3)        # 慢一半就别换，宁可提醒一句
        self.assertIn("心理准备", note)

    def test_no_change_without_history(self):
        best, note = A.prefer_stable_over_flap(self._usable((3, 46.0)), {}, 3)
        self.assertEqual(best, 3)
        self.assertIsNone(note)

    def test_both_flappy_keeps_fastest(self):
        bad = [[12, 40, 50, 90], [12, 40, 30, 88], [12, 40, 40, 70]]
        hist = {"3": {"samples": bad}, "40": {"samples": bad}}
        best, _ = A.prefer_stable_over_flap(self._usable((3, 46.0), (40, 44.0)), hist, 3)
        self.assertEqual(best, 3)


class SiteMerge(unittest.TestCase):
    """切频段会换服务小区：同一张桌子 236 ↔ 350，不能记成两个地方。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _data(self, last_hours_ago=0.1):
        return {"236-CMCC": {"seen": 3, "last": ago(hours=last_hours_ago),
                             "bands": {"40": {"dead": 0, "ok": 3, "best": 40.0}}}}

    def test_recent_same_operator_is_the_same_place(self):
        data = self._data(0.1)                       # 6 分钟前刚测过
        self.assertEqual(A.find_site(data, "350-CMCC"), "236-CMCC")

    def test_long_ago_is_a_new_place(self):
        data = self._data(3)
        self.assertEqual(A.find_site(data, "350-CMCC"), "350-CMCC")

    def test_different_operator_is_a_new_place(self):
        data = self._data(0.1)
        self.assertEqual(A.find_site(data, "350-CTCC"), "350-CTCC")

    def test_existing_key_is_itself(self):
        data = self._data(0.1)
        self.assertEqual(A.find_site(data, "236-CMCC"), "236-CMCC")

    def test_record_site_merges_and_remembers_alias(self):
        self.tmp.write_text(json.dumps(self._data(0.1)), encoding="utf-8")
        A.record_site("350-CMCC", [{"band": 40, "ok": True, "mbps": 33.0}])
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["236-CMCC"])   # 没多出一条「新地方」
        self.assertEqual(data["236-CMCC"]["seen"], 4)
        self.assertIn("350-CMCC", data["236-CMCC"]["aliases"])


class SiteCells(unittest.TestCase):
    """位置 = 一组「见过的小区」，不是一个 PCI。

    实机 2026-10-30：宿舍锁 BAND 3 时是 PCI 350（记着 BAND 3 有 46.8 Mbps），
    解锁后设备自己驻留 BAND 40 变成 PCI 236，于是新建了一条空记录 ——
    20 分钟的并档窗口早就过了，46.8 那条历史再也没被看到。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _rich(self, last_hours_ago=30):
        return {"350-CMCC": {
            "seen": 9, "last": ago(hours=last_hours_ago), "best": 3,
            "bands": {"3": {"dead": 0, "ok": 9, "best": 46.8}},
            "cells": [{"pci": "350", "band": 3, "n": 9},
                      {"pci": "236", "band": 40, "n": 2}],
        }}

    def test_cell_list_overrides_merge_window(self):
        # 隔一天了（早就超出 20 分钟窗口），但 236 在这处的 cells 里 → 还是同一处
        data = self._rich(30)
        self.assertEqual(A.find_site(data, "236-CMCC"), "350-CMCC")

    def test_cell_from_another_operator_is_ignored(self):
        data = self._rich(30)
        self.assertEqual(A.find_site(data, "236-CTCC"), "236-CTCC")

    def test_richer_record_wins_over_empty_provisional(self):
        # 236 那条是临时记录（seen=0、bands 空），350 那条有真数据 → 并到 350
        data = self._rich(30)
        data["236-CMCC"] = {"seen": 0, "provisional": True, "bands": {},
                            "best": 40, "last": ago(hours=0.2)}
        self.assertEqual(A.find_site(data, "236-CMCC"), "350-CMCC")

    def test_used_more_wins(self):
        # 两条都有数据：用得多的那条是这个位置的本体
        data = self._rich(30)
        data["236-CMCC"] = {"seen": 20, "last": ago(hours=0.2),
                            "bands": {"40": {"dead": 0, "ok": 20, "best": 31.0}}}
        self.assertEqual(A.find_site(data, "236-CMCC"), "236-CMCC")

    def _no_bridge(self, last_hours_ago=30):
        """只有 350 一条本体，cells 里还不知道 236 —— 换频段那一刻的真实状态。"""
        return {"350-CMCC": {
            "seen": 9, "last": ago(hours=last_hours_ago), "best": 3,
            "bands": {"3": {"dead": 0, "ok": 9, "best": 46.8}},
            "cells": [{"pci": "350", "band": 3, "n": 9}],
        }}

    def test_cell_change_absorbs_observation_record(self):
        """设备换小区那一下，先把「只有观测」的新记录并进来，别留 5 分钟空窗。

        顺序躲不开：守护先写信号、后判「设备换小区了」，所以切到新频段那一拍
        必然先在陌生 PCI 下建条观测记录。桥接时不并掉，它就要等下一次写记忆
        （最长 5 分钟）才被吸收 —— 这段时间里守护读到的记忆还是没信号。
        """
        self.tmp.write_text(json.dumps(self._no_bridge(30)), encoding="utf-8")
        # 守护在陌生 PCI 236 下采到信号（这时还没人告诉它 236 是同一处）
        A.note_band_rsrp("236-CMCC", 3, -96)
        self.assertEqual(len(json.loads(self.tmp.read_text(encoding="utf-8"))), 2)
        A.note_cell_change("350-CMCC", "236-CMCC")
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["350-CMCC"])          # 观测记录被并掉
        self.assertEqual(data["350-CMCC"]["bands"]["3"]["rsrp"], -96)
        self.assertEqual(data["350-CMCC"]["bands"]["3"]["best"], 46.8)  # 速度没动

    def test_cell_change_keeps_measured_record_apart(self):
        """两条都真测过：桥接只连小区，不许把其中一条吃掉。"""
        self.tmp.write_text(json.dumps(self._no_bridge(30)), encoding="utf-8")
        A.record_site("236-CMCC", [{"band": 40, "ok": True, "mbps": 12.0}])
        before = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertIn("236-CMCC", before)
        A.note_cell_change("350-CMCC", "236-CMCC")
        after = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(sorted(after), sorted(before))
        self.assertEqual(after["236-CMCC"]["bands"]["40"]["best"], 12.0)

    def test_cell_change_anchors_on_measured_record_not_shell(self):
        """old_key 是空壳、new_key 才是本体时，锚点跟数据走，别把记忆挂到空壳上。

        守护的真实顺序：先在陌生 PCI 350 下写信号（建空壳记录），下一拍才发现
        「设备换小区了」，于是 note_cell_change 的 old_key 正是那条空壳 ——
        锚点若还是「old_key 的记录」，有数据的 236 就会被留在外面。
        """
        self.tmp.write_text(json.dumps(self._no_bridge(30)), encoding="utf-8")
        A.note_band_rsrp("236-CMCC", 3, -96)       # 空壳 236（新 PCI）
        A.note_cell_change("236-CMCC", "350-CMCC")  # 设备换回 350，人没动
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["350-CMCC"])
        self.assertEqual(data["350-CMCC"]["bands"]["3"]["rsrp"], -96)
        self.assertEqual(data["350-CMCC"]["bands"]["3"]["best"], 46.8)
        self.assertEqual({c["pci"] for c in data["350-CMCC"]["cells"]}, {"350", "236"})

    def test_absorb_keeps_cells_and_count_but_not_speeds(self):
        self.tmp.write_text(json.dumps(self._rich(30)), encoding="utf-8")
        A.record_site("236-CMCC", [{"band": 40, "ok": True, "mbps": 12.0}])
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["350-CMCC"])      # 只剩一条
        rec = data["350-CMCC"]
        self.assertEqual(rec["seen"], 10)               # 9 + 1
        self.assertEqual(rec["bands"]["3"]["best"], 46.8)   # 旧速度没被污染
        self.assertEqual(rec["bands"]["40"]["best"], 12.0)
        self.assertIn("236", [c["pci"] for c in rec["cells"]])

    def test_provisional_does_not_overwrite_measured_best(self):
        # 「先锁一下」不能把上次体检的结论改掉（实机：会让 BAND 3 最优变成 BAND 40）
        self.tmp.write_text(json.dumps(self._rich(0.2)), encoding="utf-8")
        A.mark_site_provisional("236-CMCC", 40)
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["350-CMCC"]
        self.assertEqual(rec["best"], 3)
        self.assertNotIn("provisional", rec)
        self.assertIn("236", [c["pci"] for c in rec["cells"]])


class ProvisionalLifecycle(unittest.TestCase):
    """「先锁一下、没测过」这个标记必须会消失。

    实机 2026-10-30 19:44：236-CMCC 在 18:14 已经实测过 40/3/8/39 四条频段，
    但 17:59 打上的 provisional 一直挂着 —— guard_memory_band 看到它就当成
    新地方，走了「快测当前、直接锁」，记忆里的最优和信号闸门一次都没被用到。
    更糟的是它永远不会触发体检，所以这个标记也没机会被清掉。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _prov(self):
        self.tmp.write_text("{}", encoding="utf-8")
        A.mark_site_provisional("236-CMCC", 40)
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["236-CMCC"]
        self.assertTrue(rec.get("provisional"))
        return rec

    def test_real_measurement_drops_the_flag(self):
        self._prov()
        A.record_site("236-CMCC", [{"band": 40, "ok": True, "mbps": 12.0,
                                    "rsrp": -77}])
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["236-CMCC"]
        self.assertNotIn("provisional", rec)
        self.assertTrue(A._site_measured(rec))

    def test_all_dead_still_counts_as_unmeasured(self):
        # 全都没信号 = 什么也没证明，标记留着，下次来补测
        self._prov()
        A.record_site("236-CMCC", [{"band": 38, "ok": False,
                                    "reason": "no_signal"}])
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["236-CMCC"]
        self.assertTrue(rec.get("provisional"))
        self.assertFalse(A._site_measured(rec))

    def test_measured_provisional_is_not_disposable(self):
        # 老数据留下的「标记为真、数据其实有」：不能当成空壳并到别处去
        data = {"350-CMCC": {"seen": 9, "last": ago(hours=30), "best": 3,
                             "bands": {"3": {"dead": 0, "ok": 9, "best": 46.8}}},
                "236-CMCC": {"seen": 1, "provisional": True,
                             "last": ago(hours=2), "best": 40,
                             "bands": {"40": {"dead": 0, "ok": 1, "best": 17.8}}}}
        self.assertEqual(A.find_site(data, "236-CMCC"), "236-CMCC")


class NoteCellChange(unittest.TestCase):
    """守护每 30 秒看一次 PCI：设备自己换小区（解锁后换频段）时要把它记下来，
    否则「窗口一过就分家」。实机 17:52：BAND 41 / 477 → BAND 40 / 236。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self.tmp.write_text("{}", encoding="utf-8")
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _write(self, last_hours_ago=9):
        self.tmp.write_text(json.dumps({"477-CMCC": {
            "seen": 4, "last": ago(hours=last_hours_ago), "best": 41,
            "bands": {"41": {"dead": 0, "ok": 4, "best": 26.0}}}}),
            encoding="utf-8")

    def test_new_cell_is_remembered(self):
        self._write()
        A.note_cell_change("477-CMCC", "236-CMCC")
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["477-CMCC"]
        self.assertEqual(sorted(c["pci"] for c in rec["cells"]), ["236", "477"])
        # 以后再看到 236，就认得是这处，哪怕隔了一天
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(A.find_site(data, "236-CMCC"), "477-CMCC")

    def test_other_operator_is_ignored(self):
        self._write()
        A.note_cell_change("477-CMCC", "236-CTCC")
        rec = json.loads(self.tmp.read_text(encoding="utf-8"))["477-CMCC"]
        self.assertNotIn("cells", rec)          # 换了运营商是另一张卡的事

    def test_unrecorded_place_is_left_alone(self):
        # 这处还没建过记录（新地方，第一次来）→ 什么也不做，不留半个壳
        A.note_cell_change("999-CMCC", "236-CMCC")
        self.assertEqual(json.loads(self.tmp.read_text(encoding="utf-8")), {})


class NoteBandRsrp(unittest.TestCase):
    """锁定那一刻把「这条频段此刻的信号」写回记忆。

    老记忆（2026-10-30 之前）只有速度没有 RSRP，守护因此只能跑完整体检 ——
    实机 21:56 为此白等 6 分钟、断了 7 次网，最后还是靠一次不可信的体检
    把设备切到了 -95dBm。锁定时的 RSRP 本来就在手上，顺手存了就没这回事。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "sites.json"
        self.tmp.write_text(json.dumps({"350-CMCC": {
            "seen": 3, "last": ago(hours=5), "best": 3,
            "bands": {"3": {"dead": 0, "ok": 3, "best": 44.6},
                      "40": {"dead": 0, "ok": 2, "best": 40.7}}}},
            ensure_ascii=False), encoding="utf-8")
        self._p = mock.patch.object(A, "SITES", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_writes_rsrp_for_that_band_only(self):
        A.note_band_rsrp("350-CMCC", 40, -77.4)
        bands = json.loads(self.tmp.read_text(encoding="utf-8"))["350-CMCC"]["bands"]
        self.assertEqual(bands["40"]["rsrp"], -77)
        self.assertEqual(bands["40"]["rsrp_med"], -77)
        self.assertNotIn("rsrp", bands["3"])      # 没测过的频段不能瞎写

    def test_new_band_gets_rsrp_but_not_marked_measured(self):
        """记忆里还没有的频段：信号可以记，但不能算「测过」。

        守护锁到哪条就采到哪条，这条观测本来就该留在记忆里（否则下次还得
        重新瞎猜）。但只有一条 RSRP 不等于测过 —— 不能写 ok / best，不然
        「这个位置有几条频段可用」就被一次采样骗了。
        """
        A.note_band_rsrp("350-CMCC", 8, -90)      # 这条记忆里原来没有 BAND 8
        bands = json.loads(self.tmp.read_text(encoding="utf-8"))["350-CMCC"]["bands"]
        self.assertEqual(bands["8"]["rsrp"], -90)
        self.assertNotIn("ok", bands["8"])        # 没测过
        self.assertNotIn("best", bands["8"])

    def test_empty_value_is_ignored(self):
        A.note_band_rsrp("350-CMCC", 40, None)    # 读不到信号
        bands = json.loads(self.tmp.read_text(encoding="utf-8"))["350-CMCC"]["bands"]
        self.assertNotIn("rsrp", bands["40"])

    def test_unknown_pci_gets_its_own_record(self):
        """还没建过档的 PCI 也要能写进去 —— 这就是「两个模式共用一套记忆」。

        2026-10-31 实机：记忆里只有昨天在 PCI 236 下测的，今天设备锁在 BAND 3
        上、PCI 是 350；守护在这个新 PCI 下采到的信号全被丢掉，因为原来
        `rec = data.get(find_site(...))` 找不到就直接 return。
        """
        A.note_band_rsrp("350-CMCC", 3, -96)
        A.note_band_rsrp("236-CMCC", 42, -80)     # 同运营商、另一个小区
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(data["350-CMCC"]["bands"]["3"]["rsrp"], -96)
        self.assertEqual(data["236-CMCC"]["bands"]["42"]["rsrp"], -80)
        # 新建的记录只是「来过」，不能算测过（没有 last → site_memory_state 判 stale）
        self.assertNotIn("last", data["236-CMCC"])
        self.assertEqual(data["236-CMCC"].get("seen"), 0)
        # 但「来过」得留下时间戳，否则排错时这条记录和「坏了的老记录」没区别
        self.assertIn("observed", data["236-CMCC"])

    def test_absorbing_observation_keeps_its_signal(self):
        """纯观测记录被并进实测记录时，信号/丢包要跟着走，别整条扔了。

        实机 2026-10-31：守护锁 BAND 3 时 PCI 是 350、auto 驻留 BAND 40 时是
        236，记忆本体在 236 名下。守护先在 350 这条空记录里采到 -96dBm，等
        `note_cell_change` 把两个 PCI 认成同一处之后，350 会被并进 236 ——
        原来 `_absorb_site` 只搬小区和次数，采来的信号就这么没了。
        """
        data = {
            "236-CMCC": {"seen": 2, "last": ago(hours=3), "best": 40,
                         "bands": {"40": {"dead": 0, "ok": 2, "best": 40.7}}},
            "350-CMCC": {"seen": 0, "bands": {
                "3": {"rsrp": -96, "rsrp_med": -96,
                      "samples": [[10, None, 80, 210, -96]]}}},
        }
        A._absorb_site(data, "236-CMCC", "350-CMCC")
        self.assertNotIn("350-CMCC", data)
        b3 = data["236-CMCC"]["bands"]["3"]
        self.assertEqual(b3["rsrp"], -96)
        self.assertEqual(b3["samples"][0][2], 80)
        self.assertNotIn("ok", b3)                 # 观测不等于测过
        # 实测过的那条不能被观测盖掉
        self.assertEqual(data["236-CMCC"]["bands"]["40"]["best"], 40.7)

    def test_absorbing_measured_record_does_not_merge_totals(self):
        """两条都实测过的记录合并时，速度成绩不许互相串（原有行为，守一下）。"""
        data = {
            "236-CMCC": {"seen": 2, "last": ago(hours=3), "best": 40,
                         "bands": {"40": {"dead": 0, "ok": 2, "best": 40.7}}},
            "350-CMCC": {"seen": 1, "last": ago(hours=1), "best": 3,
                         "bands": {"3": {"dead": 0, "ok": 1, "best": 23.0}}},
        }
        A._absorb_site(data, "236-CMCC", "350-CMCC")
        self.assertNotIn("3", data["236-CMCC"]["bands"])
        self.assertEqual(data["236-CMCC"]["bands"]["40"]["best"], 40.7)

    def test_gate_can_use_it_next_time(self):
        A.note_band_rsrp("350-CMCC", 40, -77)
        self.assertEqual(A.guard_memo_rsrp("350-CMCC", 40), -77.0)


if __name__ == "__main__":
    unittest.main()
