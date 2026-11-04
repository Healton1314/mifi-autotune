# -*- coding: utf-8 -*-
"""体检耗时：能省的省掉，估时的数字要靠谱。

实测中：「在提升软件的运行速度，感觉等的时间越来越久了」。
查下来是两件事叠在一起：

  · 说「约 5 分钟（4 个频段）」，实际测了 6 个频段、跑了 9 分钟 ——
    估算用的是 plan.json 里上一轮剩下的条数，不是这次真要测的条数。
  · 「决赛」（把第一名和第二名各补测一轮）无条件跑，每次多切两次频段、
    约 70 秒。而它的目的只是「差距太小、怕是噪声」——差距明显时纯属白等。

两处都在这里钉住：省，只许在「明显赢了」的时候省；估，宁多不少。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune as A


def row(band, mbps, avg=80, loss=0, samples=None):
    return {"band": band, "ok": True, "mbps": mbps, "avg": avg, "loss": loss,
            "mbps_all": samples if samples is not None else [mbps]}


class ContestCanBeSkipped(unittest.TestCase):
    def test_clear_win_skips_contest(self):
        # 实机 11-04 09:58：40 号 32.6，8 号 19.9，9 号 8.6 —— 一眼就看出来了
        why = A._contest_skip_reason([row(40, 32.6), row(8, 19.9), row(39, 8.6)],
                                     contest_band=41)
        self.assertIsNotNone(why)
        self.assertIn("40", why)

    def test_close_call_still_contests(self):
        # 31.5 对 29.0（差 8%）—— 不复测等于拿运气当结论
        self.assertIsNone(A._contest_skip_reason(
            [row(40, 31.5), row(3, 29.0)], contest_band=41))

    def test_unstable_winner_still_contests(self):
        # 单轮 [27.0, 2.8] 这种过山车，领先得再多也不敢省
        self.assertIsNone(A._contest_skip_reason(
            [row(3, 27.0, samples=[27.0, 2.8]), row(40, 8.0)], contest_band=41))

    def test_current_band_among_leaders_still_contests(self):
        # 当前驻留那条要留作「提升多少」的基准，不能省掉它的复测
        self.assertIsNone(A._contest_skip_reason(
            [row(40, 32.6), row(3, 8.6)], contest_band=3))

    def test_single_candidate_has_nothing_to_skip(self):
        self.assertIsNone(A._contest_skip_reason([row(40, 32.6)], contest_band=3))

    def test_dead_bands_do_not_count(self):
        dead = {"band": 38, "ok": False, "reason": "no_signal", "mbps": 0}
        why = A._contest_skip_reason([row(40, 32.6), row(8, 8.0), dead],
                                     contest_band=41)
        self.assertIsNotNone(why)


class EtaTellsTheTruth(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = [mock.patch.object(A, "PLAN", self.tmp / "plan.json"),
                   mock.patch.object(A, "CONFIG", self.tmp / "config.json")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_uses_the_declared_band_count_not_the_old_plan(self):
        # 旧 plan 只剩 4 条，实际有 6 条 —— 不能再报「4 个频段 / 5 分钟」
        (self.tmp / "plan.json").write_text(json.dumps(
            {"lte": [{"band": b} for b in (3, 8, 39, 40)]}), encoding="utf-8")
        (self.tmp / "config.json").write_text(json.dumps(
            {"candidate_bands": [3, 8, 38, 39, 40, 41]}), encoding="utf-8")
        eta = A._measure_eta()
        self.assertIn("6 个频段", eta)
        self.assertNotIn("4 个频段", eta)

    def test_never_promises_less_than_five_minutes_for_a_full_sweep(self):
        (self.tmp / "config.json").write_text("{}", encoding="utf-8")
        eta = A._measure_eta()
        self.assertIn("6 个频段", eta)
        # 6 段 ≈ 5.5 分钟 + 复核，报整分钟不能少于 6
        self.assertRegex(eta, r"约 [6-9] 分钟")

    def test_mentions_the_extra_contest_round(self):
        eta = A._measure_eta()
        self.assertIn("复核", eta)


class DeadBandGoesStraightToNext(unittest.TestCase):
    """没信号就直奔下一条，不要「先切回原频段再切过去」。

    实测中：「我检测到没信号了，那就应该直接进行下一个信号的检测，
    而不是先回原频道然后再切频道」。这是对的：多一次射频重启 = 多 20~40 秒
    白等，而「没覆盖」恰恰是最常见的跳过原因。
    """

    def _run(self, results, bands=(3, 38, 40), cur=3):
        calls = {"restore": 0, "measured": []}

        def fake_measure(dev, b, **kw):
            calls["measured"].append(b)
            return dict(results[b])

        def fake_restore(dev, b, **kw):
            calls["restore"] += 1
            return True, {}

        with mock.patch.object(A, "measure_band", side_effect=fake_measure), \
                mock.patch.object(A, "restore_band", side_effect=fake_restore), \
                mock.patch.object(A, "_CYCLE_CANCEL") as cancel:
            cancel.is_set.return_value = False
            A.measure_all_bands(mock.Mock(), list(bands), 1, cur, "BAND 3",
                                contest_band=None)
        return calls

    def ok(self, band):
        return {"band": band, "ok": True, "mbps": 10.0, "avg": 80, "loss": 0,
                "jitter": 5.0, "mbps_all": [10.0], "rsrp": -95}

    def dead(self, band):
        return {"band": band, "ok": False, "reason": "no_signal", "mbps": 0,
                "avg": None, "loss": 100, "rsrp": None, "pci": None}

    def test_no_signal_does_not_switch_back(self):
        calls = self._run({3: self.ok(3), 38: self.dead(38), 40: self.ok(40)})
        # 只有最后收尾那一次还原，中途一次都没回原频段
        self.assertEqual(calls["restore"], 1)
        self.assertEqual(calls["measured"], [3, 38, 40])

    def test_device_error_restores_and_retries_once(self):
        bad = {"band": 38, "ok": False, "reason": "device_error",
               "mbps": 0, "avg": None, "loss": 100, "rsrp": None, "pci": None}
        calls = self._run({3: self.ok(3), 38: bad, 40: self.ok(40)})
        # 中途还原一次 + 收尾一次；失败那条被重测一次
        self.assertEqual(calls["restore"], 2)
        self.assertEqual(calls["measured"], [3, 38, 38, 40])

    def test_timeout_still_restores(self):
        def to(band):
            return {"band": band, "ok": False, "reason": "timeout", "mbps": 0,
                    "avg": None, "loss": 100, "rsrp": None, "pci": None}
        calls = self._run({3: self.ok(3), 38: to(38), 40: self.ok(40)})
        self.assertEqual(calls["restore"], 2)
        self.assertEqual(calls["measured"], [3, 38, 40])


if __name__ == "__main__":
    unittest.main()
