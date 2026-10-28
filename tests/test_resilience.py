"""容错回归测试。

这三条都对应真实事故，不是假设：
1) 测 BAND 8 时设备 HTTP 超时 → 裸 URLError 冒到最外层 → 整轮体检崩掉，
   设备被留在 BAND 8（丢包 60%），用户当场上不了网。
2) 所以：设备超时必须变成可识别的 DeviceUnreachable，单个频段失败不能让
   整轮体检中断。
3) 自动信道（channel=0）时不能算出「干扰分 None」这种无意义结论。
"""

import unittest
import urllib.error
from unittest import mock

import autotune
import drivers.zte_rta as zte
from drivers.base import DeviceUnreachable


class FakeDev:
    """最小设备替身：可控地在 set_band 时报错。"""

    name = "fake"

    def __init__(self, raise_on_set=None):
        self.raise_on_set = raise_on_set
        self.band = 40

    def set_band(self, band):
        if self.raise_on_set:
            raise self.raise_on_set
        self.band = band

    def get_state(self):
        return {"band": self.band, "channel": 0, "signal": {"rsrp": "-90dBm"}}

    def capabilities(self):
        return {"bands": [3, 40], "channels": list(range(1, 14)),
                "can_lock_band": True, "can_set_channel": True}


class TestDeviceTimeout(unittest.TestCase):
    def test_raw_converts_url_error_to_device_unreachable(self):
        c = zte.ZteRtaClient("192.168.0.1", "admin", "admin", 1)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(DeviceUnreachable) as ctx:
                c._raw("/xml_action.cgi?method=get&module=duster&file=wan")
        self.assertIn("暂时没有响应", str(ctx.exception))

    def test_get_retries_then_succeeds(self):
        """第一次超时、第二次正常 —— 必须重试成功，不能直接失败。"""
        c = zte.ZteRtaClient("192.168.0.1", "admin", "admin", 1)
        ok = mock.MagicMock()
        ok.status = 200
        ok.read.return_value = b"<wan><Prefer_Band>3</Prefer_Band></wan>" + b"x" * 50
        ok.headers = {}
        ok.__enter__ = lambda s: s
        ok.__exit__ = lambda *a: False

        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("timed out")
            return ok

        with mock.patch("urllib.request.urlopen", side_effect=flaky), \
                mock.patch("time.sleep", return_value=None), \
                mock.patch.object(c, "_challenge", return_value=True), \
                mock.patch.object(c, "_auth", return_value="Digest x"):
            txt = c.get("wan", retries=2)
        self.assertIn("<wan>", txt)
        self.assertGreaterEqual(calls["n"], 2)


class TestMeasureBandTolerance(unittest.TestCase):
    def test_set_band_failure_marks_band_not_raises(self):
        dev = FakeDev(raise_on_set=DeviceUnreachable("设备暂时没有响应"))
        r = autotune.measure_band(dev, 8, rounds=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["band"], 8)
        self.assertEqual(r["reason"], "device_error")
        self.assertIn("设备暂时没有响应", r["error"])

    def test_unexpected_exception_also_contained(self):
        dev = FakeDev(raise_on_set=ValueError("坏数据"))
        r = autotune.measure_band(dev, 3, rounds=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "device_error")


class TestAutoChannel(unittest.TestCase):
    def test_auto_channel_gets_null_score_not_zero(self):
        """channel=0 表示自动：cur_score 必须是 None，界面才能显示「自动」。"""
        rows = [{"ch": 7, "score": 0.24}, {"ch": 1, "score": 0.9}]
        cur_ch = 0
        ch_auto = not cur_ch
        cur_score = (None if ch_auto
                     else next((r["score"] for r in rows if r["ch"] == cur_ch), None))
        self.assertTrue(ch_auto)
        self.assertIsNone(cur_score)

    def test_signal_usable_rejects_dash_and_zero(self):
        for raw in ("", "0", "-0", "--", "0dBm"):
            self.assertFalse(autotune._signal_usable({"signal": {"rsrp": raw}}), raw)
        for raw in ("-97dBm", "-80"):
            self.assertTrue(autotune._signal_usable({"signal": {"rsrp": raw}}), raw)


class TestReliability(unittest.TestCase):
    """测不准时不能硬给建议 —— 这是「0 Mbps 也敢推荐切频段」那次事故的回归测试。"""

    def test_healthy_result_is_reliable(self):
        lte = [{"band": 3, "ok": True, "mbps": 40.0, "mbps_all": [38.0, 42.0]},
               {"band": 40, "ok": True, "mbps": 22.0, "mbps_all": [21.0, 23.0]}]
        caveats = autotune.assess_reliability(lte, lte[0], lte[0])
        self.assertEqual(caveats, [])

    def test_speed_source_dead_is_flagged(self):
        lte = [{"band": 3, "ok": False, "reason": "speed_fail", "mbps_all": []},
               {"band": 40, "ok": True, "mbps": 16.0, "mbps_all": [16.0]}]
        caveats = autotune.assess_reliability(lte, lte[0], lte[1])
        self.assertTrue(any("测不出速度" in c for c in caveats))

    def test_very_slow_network_is_flagged(self):
        lte = [{"band": 3, "ok": True, "mbps": 1.2, "mbps_all": [1.2]},
               {"band": 40, "ok": True, "mbps": 2.0, "mbps_all": [2.0]}]
        caveats = autotune.assess_reliability(lte, lte[0], lte[1])
        self.assertTrue(any("参考线" in c for c in caveats))

    def test_huge_swing_is_flagged(self):
        # 0 与 31.8 差 30 倍：这种波动下「最优频段」纯属运气
        lte = [{"band": 40, "ok": True, "mbps": 15.9, "mbps_all": [1.0, 31.8]}]
        caveats = autotune.assess_reliability(lte, lte[0], lte[0])
        self.assertTrue(any("波动过大" in c for c in caveats))

    def test_zero_samples_do_not_crash(self):
        lte = [{"band": 3, "ok": True, "mbps": 0.0, "mbps_all": [0.0, 0.0]}]
        caveats = autotune.assess_reliability(lte, lte[0], lte[0])
        self.assertTrue(caveats)   # 已经是 0 Mbps，必然被标不可信


if __name__ == "__main__":
    unittest.main()
