"""「现在多少 → 能到多少」的对比基准测试。

起因：设备处于「不锁频段」状态时（出厂态，或刚 --restore 完），
get_state()["band"] 永远是 0，旧代码拿 0 去和实测结果比，永远比不出来，
用户跑完 5 分钟只看到「未测（不在本次范围）」，等于白测。
真实驻留频段在 status1 的 lte_band 里，要用它当对比基准。
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


class FakeDev:
    name = "fake"
    description = "假设备"

    def __init__(self, band=0, channel=6, serving="3"):
        self.band = band
        self.channel = channel
        self.serving = serving
        self.calls = []

    def set_band(self, band):
        self.calls.append(band)
        self.band = band

    def get_state(self):
        return {"band": self.band, "channel": self.channel,
                "signal": {"rsrp": "-90dBm", "band": self.serving}}

    def capabilities(self):
        return {"bands": [3, 40], "channels": list(range(1, 14)),
                "can_lock_band": True, "can_set_channel": True}


def args_ns(**kw):
    # 只测频段：wifi 部分会真去调 netsh/nmcli，测试里不需要也不该碰系统。
    d = dict(wifi_only=False, lte_only=True, rounds=1, bands="default",
             ssid=None, scan_rounds=1)
    d.update(kw)
    return argparse.Namespace(**d)


def band_result(band, mbps, loss=0, avg=50):
    return {"band": band, "ok": True, "mbps": mbps, "mbps_all": [mbps],
            "mbps_min": mbps, "mbps_max": mbps, "avg": avg, "max": avg + 10,
            "jitter": 5, "loss": loss, "rounds": 1}


class PreviewCompare(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "plan.json"
        self._p = mock.patch.object(autotune, "PLAN", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if self.tmp.exists():
            self.tmp.unlink()

    def run_preview(self, dev, results):
        caps = dev.capabilities()

        def fake_measure(d, band, **kw):
            return results[band]

        with mock.patch.object(autotune, "measure_band", fake_measure), \
                mock.patch.object(autotune.time, "sleep", lambda s: None):
            autotune.run_preview(dev, caps, dev.get_state(), args_ns(), {}, None, None)
        return json.loads(self.tmp.read_text(encoding="utf-8"))

    def test_unlocked_device_compares_against_serving_band(self):
        """未锁频段：基线要用「实际驻留的 BAND 3」，而不是 0。"""
        dev = FakeDev(band=0, serving="3")
        plan = self.run_preview(dev, {3: band_result(3, 20), 40: band_result(40, 32)})
        self.assertEqual(plan["current"]["band"], 0)
        self.assertEqual(plan["current"]["serving_band"], 3)
        self.assertEqual(plan["expected"]["band_from"], 3)
        self.assertEqual(plan["expected"]["mbps_now"], 20)
        self.assertEqual(plan["expected"]["mbps_gain"], 12.0)
        self.assertTrue(plan["changes"])

    def test_unlocked_device_is_not_locked_when_already_on_best_band(self):
        """最优恰好是它当前驻留的频段时，锁死只会失去自动切换，不能改。"""
        dev = FakeDev(band=0, serving="40")
        plan = self.run_preview(dev, {3: band_result(3, 20), 40: band_result(40, 32)})
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["target"]["band"], 0)
        self.assertNotIn(40, dev.calls)      # 不该为了「一样快」去锁 BAND 40

    def test_locked_device_still_switches(self):
        """锁定状态的老行为不能变：BAND 3 → BAND 40。"""
        dev = FakeDev(band=3, serving="3")
        plan = self.run_preview(dev, {3: band_result(3, 20), 40: band_result(40, 32)})
        self.assertEqual(plan["expected"]["band_from"], 3)
        self.assertEqual(plan["target"]["band"], 40)
        self.assertTrue(plan["changes"])

    def test_unusable_best_band_is_not_chosen(self):
        """丢包 90% 的频段带宽再高也不能当最优（和 --apply 必须同一套标准）。"""
        dev = FakeDev(band=3, serving="3")
        plan = self.run_preview(dev, {3: band_result(3, 20),
                                      40: band_result(40, 60, loss=90, avg=1100)})
        self.assertEqual(plan["target"]["band"], 3)
        self.assertNotIn("40", json.dumps(plan["changes"]))


class MeasureAllBandsSafety(unittest.TestCase):
    """「测量 + 保证还原」是一份共用实现，两条路径都靠它守住设备。

    事故原型：测 BAND 8 时驱动崩溃、进程退出，设备被留在 BAND 8（丢包 60%），
    用户当场上不了网。这里锁住两个性质：异常照常抛出，但设备已经被切回。
    """

    def test_restores_when_measure_raises(self):
        dev = FakeDev(band=3)

        def boom(d, band, **kw):
            d.set_band(band)          # 已经切过去了，然后炸
            raise RuntimeError("驱动炸了")

        with mock.patch.object(autotune, "measure_band", boom), \
                mock.patch.object(autotune.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                autotune.measure_all_bands(dev, [40, 8], 1, 3, "BAND 3")
        self.assertEqual(dev.calls[:1], [40])
        self.assertEqual(dev.calls[-1], 3)     # 炸了也要切回来

    def test_ctrl_c_returns_and_restores(self):
        dev = FakeDev(band=3)

        def interrupt(d, band, **kw):
            d.set_band(band)
            raise KeyboardInterrupt

        with mock.patch.object(autotune, "measure_band", interrupt), \
                mock.patch.object(autotune.time, "sleep", lambda s: None):
            out = autotune.measure_all_bands(dev, [40], 1, 3, "BAND 3")
        self.assertEqual(out, [])
        self.assertEqual(dev.calls[-1], 3)

    def test_unlocked_device_is_restored_to_auto(self):
        dev = FakeDev(band=0, serving="3")

        def boom(d, band, **kw):
            d.set_band(band)
            raise RuntimeError("x")

        with mock.patch.object(autotune, "measure_band", boom), \
                mock.patch.object(autotune.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                autotune.measure_all_bands(dev, [40], 1, 0, "自动（当前 BAND 3）")
        self.assertEqual(dev.calls[-1], 0)     # 原来没锁，就得还原成「不锁」


class ConclusionWording(unittest.TestCase):
    """结论文案不能自相矛盾。

    真实输出曾经长这样（2026-10-29 本机实测）：
        最优只高 11.8 Mbps，不到 3 Mbps，不值得切换
    真正否决切换的原因是「BAND 3 两轮 9.9 / 46.6 波动过大、结论不可信」，
    却被写成了阈值问题 —— 用户会以为「以后都不用管了」，实际是该再测一次。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "plan.json"
        mock.patch.object(autotune, "PLAN", self.tmp).start()

    def tearDown(self):
        mock.patch.stopall()
        if self.tmp.exists():
            self.tmp.unlink()

    def _run(self, results):
        dev = FakeDev(band=3)
        out = io.StringIO()
        with mock.patch.object(autotune, "measure_all_bands", lambda *a, **k: results), \
                mock.patch.object(autotune.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            autotune.run_preview(dev, dev.capabilities(), dev.get_state(),
                                 args_ns(), {}, None, None)
        return out.getvalue()

    def test_unreliable_wording_does_not_blame_threshold(self):
        unstable = band_result(3, 28.2)
        unstable["mbps_all"] = [9.9, 46.6]          # 4.7 倍波动
        faster = band_result(40, 40.0)
        text = self._run([unstable, faster])
        # 真实原因是「数据不可信」，必须这么写；不能套用「差距小、不值得」的说法
        self.assertIn("数据波动", text)
        self.assertIn("数据不足以下结论", text)
        self.assertNotIn("不到 3 Mbps", text)
        self.assertNotIn("值得为它断一次网", text)

    def test_healthy_gap_below_threshold_says_not_worth(self):
        a = band_result(3, 30.0)
        b = band_result(40, 31.0)                    # 只高 1 Mbps，低于阈值
        text = self._run([a, b])
        self.assertIn("不到 3 Mbps 阈值", text)
        self.assertIn("本次不改动", text)
        self.assertNotIn("数据不足以下结论", text)


class ContestStartsFromHealthyState(unittest.TestCase):
    """决赛必须在设备健康时测。

    连跑两次实测都出现：BAND 41（无覆盖）刚跳过，紧接着决赛 BAND 3，
    复测只有 8.7 / 9.9 Mbps，而第一轮是 40.1 / 46.6 Mbps —— 差 4~5 倍。
    原因是设备刚从死频段切回来，数据通路没恢复，不是频段本身慢。
    """

    def test_restores_before_contest(self):
        dev = FakeDev(band=3)
        events = []
        orig = dev.set_band

        def recorded(band):
            events.append(("set_band", band))
            orig(band)

        dev.set_band = recorded

        def fake_measure(d, band, **kw):
            events.append(("measure", band))
            if band == 41:
                d.band = 41                      # 死频段：设备停在 41
                return {"band": 41, "ok": False, "mbps": 0, "mbps_all": [],
                        "reason": "no_signal", "waited": 15}
            d.band = band
            return band_result(band, {3: 40.0, 40: 38.0}[band])

        with mock.patch.object(autotune, "measure_band", fake_measure), \
                mock.patch.object(autotune.time, "sleep", lambda s: None):
            autotune.measure_all_bands(dev, [3, 40, 41], 1, 3, "BAND 3", contest_band=3)

        # 41 测完之后、决赛测之前，必须有一次「切回 BAND 3」
        dead = [i for i, e in enumerate(events) if e == ("measure", 41)][0]
        last = len(events) - 1 - events[::-1].index(("measure", 3))
        self.assertGreater(last, dead)
        self.assertIn(("set_band", 3), events[dead:last],
                      "决赛前没有把设备切回原频段，复测会偏低")


class ServingBandParse(unittest.TestCase):
    def test_locked_band_wins(self):
        self.assertEqual(autotune._serving_band({"band": 3}), 3)

    def test_falls_back_to_status1_band(self):
        for raw in ("3", "BAND 3", "band 40", " 8 "):
            st = {"band": 0, "signal": {"band": raw}}
            self.assertEqual(autotune._serving_band(st), int("".join(c for c in raw if c.isdigit())))

    def test_no_service_returns_none(self):
        self.assertIsNone(autotune._serving_band({"band": 0, "signal": {"band": ""}}))
        self.assertIsNone(autotune._serving_band({"band": 0, "signal": {"band": "0"}}))

    def test_label_shows_serving_band(self):
        self.assertEqual(autotune._band_label(3), "BAND 3")
        self.assertEqual(autotune._band_label(0, 3), "自动（当前 BAND 3）")
        self.assertEqual(autotune._band_label(0), "自动")


if __name__ == "__main__":
    unittest.main()
