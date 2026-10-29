"""两条建议线：按速度换 / 按稳定换。

背景（2026-10-29 本机连续三次实跑）：
    BAND 3 在 8.7 ~ 46.6 Mbps 之间反复，BAND 40 稳定在 38~41。
    「按速度」因为波动过大不敢给建议，用户拿到的是「本次不做任何改动」——
    但数据其实足够说明「哪个更稳」。所以加第二条线：只承诺稳定，不承诺更快。
"""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune


def res(band, mbps, samples=None):
    samples = samples or [mbps]
    return {"band": band, "ok": True, "mbps": mbps, "mbps_all": list(samples),
            "mbps_min": min(samples), "mbps_max": max(samples),
            "avg": 60, "max": 80, "jitter": 5, "loss": 0, "rounds": len(samples)}


class DecideBand(unittest.TestCase):
    """自动裁决规则（用户 2026-10-29 定）：

      1) 一方速度强太多 → 选快的
      2) 速度差不多     → 选更稳的
      3) 稳定性差不多   → 选更快的
    """

    def test_speed_wins_when_far_ahead(self):
        """按「最差一轮」比较，别拿峰值吹牛。"""
        cur = res(3, 24.9, [8.7, 41.1])
        cand = [res(40, 39.5, [38.4, 40.7])]
        opt, why = autotune.decide_band(cur, cand, 3)
        self.assertEqual(opt["kind"], "speed")
        self.assertEqual(opt["band"], 40)
        self.assertIn("强太多", opt["reason"])

    def test_peak_only_speed_is_not_decisive(self):
        """峰值高但最差一轮不行 —— 不算「强太多」，不能拿它去骗人切频段。"""
        cur = res(3, 30.0, [29.0, 31.0])
        cand = [res(40, 55.0, [12.0, 98.0])]      # 中位 55，但最差 12
        opt, _why = autotune.decide_band(cur, cand, 3)
        # 最差一轮 12 < 29*1.5，速度不算强太多；候选又在乱跳，不该选它
        self.assertIsNone(opt)

    def test_stability_wins_when_speed_is_similar(self):
        """本机实跑数据：38.9（28.0~49.7）vs 37.4（33.7~41.1）—— 速度差 4%，该选稳的。"""
        cur = res(3, 38.9, [28.0, 49.7])
        cand = [res(40, 37.4, [33.7, 41.1])]
        opt, _why = autotune.decide_band(cur, cand, 3)
        self.assertIsNotNone(opt)
        self.assertEqual(opt["kind"], "stability")
        self.assertEqual(opt["band"], 40)

    def test_speed_wins_when_stability_is_similar(self):
        cur = res(3, 30.0, [29.0, 31.0])
        cand = [res(40, 40.0, [39.0, 41.0])]
        opt, _why = autotune.decide_band(cur, cand, 3)
        self.assertEqual(opt["kind"], "speed")
        self.assertIn("稳定性差不多", opt["reason"])

    def test_no_change_when_both_similar(self):
        cur = res(3, 30.0, [29.0, 31.0])
        cand = [res(40, 31.0, [30.0, 32.0])]      # 只快 1 Mbps
        opt, why = autotune.decide_band(cur, cand, 3)
        self.assertIsNone(opt)
        self.assertIn("差不多", why)

    def test_refuses_slower_band_even_if_stable(self):
        """更稳但明显更慢，换过去是降速，不能拿「稳」当借口。"""
        cur = res(3, 40.0, [30.0, 50.0])
        cand = [res(40, 30.0, [29.0, 31.0])]
        opt, _why = autotune.decide_band(cur, cand, 3)
        self.assertIsNone(opt)

    def test_ignores_current_band_in_candidates(self):
        cur = res(3, 30.0, [29.0, 31.0])
        opt, why = autotune.decide_band(cur, [res(3, 30.0, [29.0, 31.0])], 3)
        self.assertIsNone(opt)
        self.assertIn("没有可比", why)


class PreviewRendersAutoDecision(unittest.TestCase):
    """auto 裁决出「换稳定」时，结论必须能正常打印。

    踩过：stability 选项当初没带 reason，结论那行直接 KeyError 崩掉 ——
    单元测试只测了选项函数本身，没覆盖「打印」这一步。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "plan.json"
        mock.patch.object(autotune, "PLAN", self.tmp).start()

    def tearDown(self):
        mock.patch.stopall()
        if self.tmp.exists():
            self.tmp.unlink()

    def test_renders_stability_decision(self):
        dev = FakeDev()
        got = [res(3, 38.9, [28.0, 49.7]), res(40, 37.4, [33.7, 41.1])]
        out = io.StringIO()
        args = argparse.Namespace(wifi_only=False, lte_only=True, rounds=1,
                                  bands="default", ssid=None, scan_rounds=1)
        with mock.patch.object(autotune, "measure_all_bands", lambda *a, **k: got), \
                mock.patch.object(autotune.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            autotune.run_preview(dev, dev.capabilities(), dev.get_state(), args, {}, None, None)
        text = out.getvalue()
        self.assertIn("[自动] 切到 BAND 40", text)
        self.assertIn("但它稳得多", text)
        self.assertIn("按稳定", text)
        plan = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(plan["target"]["band"], 40)
        self.assertEqual(plan["strategy"], "auto")
        # 换稳定性不能承诺提速，界面才不会被误导成「能快多少」
        self.assertIsNone(plan["expected"]["mbps_gain"])

    def test_keeps_current_when_nothing_better(self):
        dev = FakeDev()
        got = [res(3, 30.0, [29.0, 31.0]), res(40, 31.0, [30.0, 32.0])]
        out = io.StringIO()
        args = argparse.Namespace(wifi_only=False, lte_only=True, rounds=1,
                                  bands="default", ssid=None, scan_rounds=1)
        with mock.patch.object(autotune, "measure_all_bands", lambda *a, **k: got), \
                mock.patch.object(autotune.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            autotune.run_preview(dev, dev.capabilities(), dev.get_state(), args, {}, None, None)
        text = out.getvalue()
        self.assertIn("[自动] 保持现状", text)
        plan = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(plan["target"]["band"], 3)
        self.assertEqual(plan["changes"], [])


class SpeedOption(unittest.TestCase):
    def test_needs_enough_gain(self):
        cur, best = res(3, 30.0), res(40, 32.0)
        self.assertIsNone(autotune.speed_option(cur, best, 3, []))
        best = res(40, 34.0)
        opt = autotune.speed_option(cur, best, 3, [])
        self.assertEqual(opt["band"], 40)
        self.assertEqual(opt["gain"], 4.0)
        self.assertEqual(opt["gain_pct"], 13)

    def test_refuses_when_data_unreliable(self):
        """按速度这条线的铁律：波动大就不出建议，宁可让人再测一次。"""
        opt = autotune.speed_option(res(3, 30.0), res(40, 60.0), 3, ["波动过大"])
        self.assertIsNone(opt)

    def test_same_band_is_not_a_switch(self):
        self.assertIsNone(autotune.speed_option(res(3, 30.0), res(3, 30.0), 3, []))


class StabilityOption(unittest.TestCase):
    def test_picks_stable_band_when_current_is_jumpy(self):
        cur = res(3, 24.9, [8.7, 41.1])          # 4.7 倍
        cand = [res(3, 24.9, [8.7, 41.1]), res(40, 39.5, [38.4, 40.7])]
        opt = autotune.stability_option(cur, cand, 3)
        self.assertEqual(opt["band"], 40)
        self.assertEqual(opt["cur_ratio"], 4.7)
        self.assertEqual(opt["best_ratio"], 1.1)

    def test_no_switch_when_current_is_already_stable(self):
        cur = res(3, 30.0, [29.0, 31.0])
        cand = [res(40, 40.0, [39.0, 41.0])]
        self.assertIsNone(autotune.stability_option(cur, cand, 3))

    def test_no_switch_when_candidate_is_also_jumpy(self):
        cur = res(3, 10.0, [5.0, 30.0])
        cand = [res(40, 40.0, [10.0, 60.0])]
        self.assertIsNone(autotune.stability_option(cur, cand, 3))

    def test_no_switch_to_slower_band(self):
        """稳但更慢，换过去就是亏 —— 不能拿「稳」当借口降速。"""
        cur = res(3, 30.0, [8.0, 40.0])
        cand = [res(40, 20.0, [19.5, 20.5])]
        self.assertIsNone(autotune.stability_option(cur, cand, 3))

    def test_prefers_stable_over_merely_fast(self):
        """最快的不一定最稳：要挑「稳且不慢」的，而不是只看排名第一。"""
        cur = res(3, 24.9, [8.7, 41.1])
        fast_but_jumpy = res(40, 55.0, [20.0, 90.0])     # 4.5 倍
        stable = res(8, 39.0, [38.0, 40.0])
        opt = autotune.stability_option(cur, [fast_but_jumpy, stable], 3)
        self.assertEqual(opt["band"], 8)

    def test_single_sample_is_not_enough(self):
        cur = res(3, 24.9, [8.7, 41.1])
        cand = [res(40, 39.5)]                            # 只测了一轮
        self.assertIsNone(autotune.stability_option(cur, cand, 3))


class FakeDev:
    name = "fake"
    description = "假设备"

    def __init__(self, band=3, channel=13):
        self.band, self.channel = band, channel
        self.calls = []

    def set_band(self, band):
        self.calls.append(band)
        self.band = band

    def set_channel(self, ch):
        self.channel = ch

    def get_state(self):
        return {"band": self.band, "channel": self.channel,
                "signal": {"rsrp": "-95dBm", "band": str(self.band)}}

    def capabilities(self):
        return {"bands": [3, 40], "channels": list(range(1, 14)),
                "can_lock_band": True, "can_set_channel": True}


class ApplyPlanStrategy(unittest.TestCase):
    """--apply-plan --strategy 必须按指定的那条线走，且 --dry-run 绝不碰设备。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "plan.json"
        mock.patch.object(autotune, "PLAN", self.tmp).start()
        self.tmp.write_text(json.dumps({
            "target": {"band": 3, "channel": 13},     # 默认策略选了「不改」
            "strategy": "speed",
            "options": {"speed": None,
                        "stability": {"band": 40, "cur_range": [8.7, 41.1],
                                      "best_range": [38.4, 40.7],
                                      "cur_ratio": 4.7, "best_ratio": 1.1}},
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        mock.patch.stopall()
        if self.tmp.exists():
            self.tmp.unlink()

    def _args(self, **kw):
        d = dict(strategy=None, dry_run=False)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_stability_strategy_applies_its_band(self):
        dev = FakeDev()
        with mock.patch.object(autotune.time, "sleep", lambda s: None), \
                mock.patch.object(autotune.probe, "ping_stats",
                                  lambda *a, **k: {"avg": 50, "loss": 0, "jitter": 3,
                                                   "min": 45, "max": 60}):
            rc = autotune.run_apply_plan(dev, dev.capabilities(), dev.get_state(),
                                         self._args(strategy="stability"), {}, None)
        self.assertEqual(rc, 0)
        self.assertIn(40, dev.calls)

    def test_dry_run_does_not_touch_device(self):
        dev = FakeDev()
        rc = autotune.run_apply_plan(dev, dev.capabilities(), dev.get_state(),
                                     self._args(strategy="stability", dry_run=True), {}, None)
        self.assertEqual(rc, 0)
        self.assertEqual(dev.calls, [])

    def test_unknown_strategy_is_reported_not_guessed(self):
        """按速度那条线没有建议时，不能偷偷套用稳定那条。"""
        dev = FakeDev()
        rc = autotune.run_apply_plan(dev, dev.capabilities(), dev.get_state(),
                                     self._args(strategy="speed"), {}, None)
        self.assertEqual(rc, 0)
        self.assertEqual(dev.calls, [])


if __name__ == "__main__":
    unittest.main()
