# -*- coding: utf-8 -*-
"""断网这件事：要发现得快、不要瞎测、不要干等。

实测中 的三句话，就是这组测试要钉住的规则：

  · 「检测到没网跳出太慢了」—— 一拍从 47 秒压回十几秒（ping 早停）。
  · 「在一个地方重复检测到没网，后续就不测，等一段时间没来才能进行全量测试」
    —— 冷却窗口。
  · 「在一处没信号的频段等了很久，最后还是我手动换的」—— 未锁 + 连续不通
    必须自己换一条，不能只等。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import autotune as A
import probe as P


class PingStopsEarly(unittest.TestCase):
    """整条链路断了的时候，10 个包凑够 20 秒毫无意义。"""

    def test_three_dead_probes_are_enough(self):
        calls = []

        def fake_icmp(n, host):
            calls.append(n)
            return []                       # 一个都不回

        with mock.patch.object(P, "_icmp_rtts", side_effect=fake_icmp), \
                mock.patch.object(P, "_tcp_latency", return_value=[]):
            st = P.ping_stats(10, "1.1.1.1")
        self.assertTrue(st["down"])
        self.assertEqual(st["loss"], 100)
        self.assertIsNone(st["avg"])
        self.assertEqual(calls, [3])        # 只发了 3 个，没发满 10 个

    def test_icmp_blocked_is_not_outage(self):
        """ICMP 全被禁但 TCP 能握手 —— 这是「这个位置禁 ICMP」，不是没网。"""
        with mock.patch.object(P, "_icmp_rtts", return_value=[]), \
                mock.patch.object(P, "_tcp_latency", return_value=[42.0, 45.0, 44.0]):
            st = P.ping_stats(10, "1.1.1.1")
        self.assertFalse(st["down"])
        self.assertEqual(st["method"], "tcp")
        self.assertEqual(st["loss"], 0)
        self.assertEqual(st["avg"], 44)

    def test_healthy_link_still_measures_all_packets(self):
        """有回显就补齐剩下的包，丢包率精度不变。"""
        calls = []

        def fake_icmp(n, host):
            calls.append(n)
            return [30.0] * n

        with mock.patch.object(P, "_icmp_rtts", side_effect=fake_icmp):
            st = P.ping_stats(10, "1.1.1.1")
        self.assertEqual(calls, [3, 7])
        self.assertEqual(st["sent"], 10)
        self.assertEqual(st["loss"], 0)
        self.assertFalse(st["down"])

    def test_partial_loss_counts_against_full_sample(self):
        seq = {3: [30.0], 7: [30.0, 30.0]}

        def fake_icmp(n, host):
            return seq[n]

        with mock.patch.object(P, "_icmp_rtts", side_effect=fake_icmp):
            st = P.ping_stats(10, "1.1.1.1")
        self.assertEqual(st["sent"], 10)
        self.assertEqual(st["recv"], 3)
        self.assertEqual(st["loss"], 70)


class NetDownDoesNotMeasure(unittest.TestCase):
    def test_decide_refuses_measure_when_net_down(self):
        action, why = A.guard_decide(
            moving=False, still_sec=900, settle_sec=600, locked=False,
            bad_streak=A.GUARD_BAD_HITS, cooldown_left=0,
            outage_after_unlock=True, net_ok=False)
        self.assertIsNone(action)
        self.assertIn("断", why)

    def test_decide_still_measures_when_net_ok(self):
        action, _ = A.guard_decide(
            moving=False, still_sec=900, settle_sec=600, locked=False,
            bad_streak=A.GUARD_BAD_HITS, cooldown_left=0,
            outage_after_unlock=True, net_ok=True)
        self.assertEqual(action, "measure")

    def test_measure_refuses_and_does_not_run_cycle(self):
        with mock.patch.object(A, "run_cycle") as run:
            self.assertIsNone(A._guard_measure(mock.Mock(), prefer=41, net_down=True))
        run.assert_not_called()


class OutageCooldown(unittest.TestCase):
    """「同一处反复没网就先别测」—— 跨进程要能读到，所以落盘。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self._p = [mock.patch.object(A, "OUTAGE", d / "guard_outage.json")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_no_record_means_no_cooldown(self):
        self.assertEqual(A.outage_left({"signal": {"pci": "350"}}), 0.0)

    def test_record_blocks_for_the_window(self):
        st = {"signal": {"pci": "350", "operator": "CMCC"}}
        A.note_outage(st, loss=100, band=41)
        left = A.outage_left(st)
        self.assertGreater(left, A.GUARD_OUTAGE_COOLDOWN - 10)
        self.assertLessEqual(left, A.GUARD_OUTAGE_COOLDOWN)

    def test_记录在别的键上也拦得住(self):
        """断网时读不到 PCI，记录落在 "*" 上；换成能读到时也不该立刻又测。"""
        A.note_outage({"signal": {}}, loss=100)
        self.assertGreater(A.outage_left({"signal": {"pci": "236"}}), 0)

    def test_expired_record_lets_it_try_again(self):
        st = {"signal": {"pci": "350"}}
        A.note_outage(st, loss=100)
        later = datetime.now() + timedelta(seconds=A.GUARD_OUTAGE_COOLDOWN + 5)
        self.assertEqual(A.outage_left(st, now=later), 0.0)

    def test_same_place_counts_attempts(self):
        st = {"signal": {"pci": "350"}}
        A.note_outage(st, loss=100)
        rec = A.note_outage(st, loss=100)
        self.assertEqual(rec["n"], 2)
        self.assertEqual(len(A.load_outage()), 1)


class PlanBandNeverLocksDeadBand(unittest.TestCase):
    """体检把当前那条判成「无信号」时，别因为「数据不可信」又把它锁回去。

    实机 2026-11-02 18:55：41 明确无信号，BAND 3 单轮 [27.0, 23.4, 2.8]
    波动大被判 reliable=False → 退回 prefer=41 → 锁回死频段，19:56 依旧 100% 丢包。
    """

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.plan = d / "plan.json"
        self._p = [mock.patch.object(A, "PLAN", self.plan)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _write(self, rows, reliable=False):
        self.plan.write_text(json.dumps({"lte": rows, "reliable": reliable}),
                             encoding="utf-8")

    def test_dead_prefer_is_replaced_by_a_live_band(self):
        self._write([
            {"band": 41, "ok": False, "reason": "no_signal"},
            {"band": 3, "ok": True, "mbps": 23.4, "avg": 86},
        ])
        self.assertEqual(A._guard_plan_band(prefer=41), 3)

    def test_live_prefer_is_kept_when_numbers_are_noisy(self):
        self._write([
            {"band": 41, "ok": True, "mbps": 4.3, "avg": 74},
            {"band": 3, "ok": True, "mbps": 23.4, "avg": 86},
        ])
        self.assertEqual(A._guard_plan_band(prefer=41), 41)

    def test_all_dead_gives_nothing_to_lock(self):
        self._write([
            {"band": 41, "ok": False, "reason": "no_signal"},
            {"band": 38, "ok": False, "reason": "no_signal"},
        ])
        self.assertIsNone(A._guard_plan_band(prefer=41))


class OutageChoice(unittest.TestCase):
    """没网又不许体检时，换哪一条：有依据才换，没依据就如实说。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.sites = d / "sites.json"
        self.decisions = d / "decisions.json"
        self._p = [mock.patch.object(A, "SITES", self.sites),
                   mock.patch.object(A, "DECISIONS", self.decisions)]
        for p in self._p:
            p.start()
        A.save_sites({})

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _seed(self, bands):
        from datetime import datetime as dt
        A.save_sites({"999-CMCC": {
            "seen": 2, "best": bands[0],
            "last": dt.now().isoformat(timespec="seconds"),
            "bands": {str(b): {"ok": 1, "best": float(12 - i),
                               "last_ok": dt.now().isoformat(timespec="seconds")}
                      for i, b in enumerate(bands)},
        }})

    def test_pin_wins_when_location_matches(self):
        band, why = A.guard_outage_choice({"signal": {"pci": "350"}},
                                          serving=41, pinned=3)
        self.assertEqual(band, 3)
        self.assertIn("指定", why)

    def test_already_tried_band_is_skipped(self):
        band, why = A.guard_outage_choice({"signal": {"pci": "350"}},
                                          serving=41, pinned=3, tried={"3"})
        self.assertIsNone(band)
        self.assertIn("试过", why)

    def test_borrows_from_another_site(self):
        self._seed([39])
        band, why = A.guard_outage_choice({"signal": {"pci": "350"}},
                                          serving=41)
        self.assertEqual(band, 39)
        self.assertIn("别处", why)

    def test_second_candidate_after_the_first_fails(self):
        """试过 39 就换 40 —— 只给一条候选等于「换一次就没了」（模拟里踩到）。"""
        self._seed([39, 40])
        band, _ = A.guard_outage_choice({"signal": {"pci": "350"}},
                                        serving=41, tried={"39"})
        self.assertEqual(band, 40)

    def test_nothing_to_go_on(self):
        band, why = A.guard_outage_choice({"signal": {"pci": "350"}}, serving=41)
        self.assertIsNone(band)
        self.assertEqual(why, "")

    def test_candidate_never_repeats_the_serving_band(self):
        self._seed([41, 39])
        band, _ = A.guard_outage_choice({"signal": {"pci": "350"}}, serving=41)
        self.assertEqual(band, 39)


class LedgerKeepsOutageDecisions(unittest.TestCase):
    """读不到 PCI 的时候最需要留痕 —— 那是用户最想问「刚才发生了什么」的几分钟。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self._p = [mock.patch.object(A, "DECISIONS", d / "decisions.json")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_empty_key_still_records(self):
        r = A.note_decision("", {"t": "2026-11-02T18:47:00", "kind": "outage_try",
                                 "band": 39})
        self.assertIsInstance(r, dict)
        data = A.load_decisions()
        self.assertEqual(len(data), 1)
        key = next(iter(data))
        self.assertTrue(key.startswith("?-"))
        self.assertEqual(data[key][0]["kind"], "outage_try")

    def test_empty_entry_is_still_rejected(self):
        self.assertEqual(A.note_decision("350-CMCC", None), "没有位置键")


class PinNeedsTheSameBand(unittest.TestCase):
    """pin_cells 是纯 PCI 数字，而 PCI 每个频段各有一套 —— 必须带上频段比。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.cfg = d / "config.json"
        self._p = [mock.patch.object(A, "CONFIG", self.cfg)]
        for p in self._p:
            p.start()
        A.save_config({"pin_band": 3, "pin_at": datetime.now().isoformat(),
                       "pin_site": "350-CMCC", "pin_cells": [350, 141]})

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_same_cell_same_band_hits(self):
        st = {"band": 3, "signal": {"pci": "350", "operator": "CMCC"}}
        self.assertEqual(A.device_pin_here(st), 3)

    def test_site_key_alone_is_not_enough_on_another_band(self):
        """位置键是「PCI-运营商」，不带频段 —— 换条频段读到同样的数字不算老地方。"""
        st = {"band": 40, "signal": {"pci": "350", "operator": "CMCC"}}
        self.assertIsNone(A.device_pin_here(st))

    def test_second_cell_same_band_hits(self):
        st = {"band": 3, "signal": {"pci": "141", "operator": "CMCC"}}
        self.assertEqual(A.device_pin_here(st), 3)

    def test_second_cell_other_band_misses(self):
        """PCI 每个频段各有一套编号 —— BAND 40 上的 141 不是住处那个小区。"""
        st = {"band": 40, "signal": {"pci": "141", "operator": "CMCC"}}
        self.assertIsNone(A.device_pin_here(st))

    def test_other_site_misses(self):
        st = {"band": 3, "signal": {"pci": "88", "operator": "CMCC"}}
        self.assertIsNone(A.device_pin_here(st))


class MeasureEta(unittest.TestCase):
    """别再写死「约 5 分钟」—— 实机跑过 8 分 9 秒，等的人以为卡死了。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.plan = d / "plan.json"
        self._p = [mock.patch.object(A, "PLAN", self.plan)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_counts_bands_from_last_plan(self):
        self.plan.write_text(json.dumps({"lte": [{"band": b} for b in (3, 8, 39, 40, 41)]}),
                             encoding="utf-8")
        eta = A._measure_eta()
        self.assertIn("5 个频段", eta)
        self.assertIn("6 分钟", eta)     # 5 × 70s ≈ 5.8 分

    def test_falls_back_without_plan(self):
        self.assertEqual(A._measure_eta(), "约 5~8 分钟")


if __name__ == "__main__":
    unittest.main()
