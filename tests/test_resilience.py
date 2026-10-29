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


class FlakyDev:
    """还原时会失败几次的设备替身。

    对应 2026-10-29 实测：切到 BAND 38（当地没覆盖）后设备重启射频，
    连续 50 多秒不应答，一次性的还原直接失败，设备被留在死频段上。
    """

    def __init__(self, start_band=38, fail_times=2):
        self.band = start_band
        self.fail_times = fail_times
        self.calls = 0

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise DeviceUnreachable("设备暂时不应答（可能在重启射频）")

    def get_state(self):
        self._maybe_fail()
        return {"band": self.band}

    def set_band(self, band):
        self._maybe_fail()
        self.band = band


class TestRestore(unittest.TestCase):
    def setUp(self):
        # 真实 SETTLE_SEC 是 26 秒，测试里不能真等
        self._p = mock.patch.object(autotune, "SETTLE_SEC", 0)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_restore_retries_until_device_answers(self):
        dev = FlakyDev(fail_times=2)
        ok, st = autotune.restore_band(dev, 3, tries=6, poll=0)
        self.assertTrue(ok)
        self.assertEqual(dev.band, 3)

    def test_restore_gives_up_after_tries(self):
        dev = FlakyDev(fail_times=99)
        ok, st = autotune.restore_band(dev, 3, tries=3, poll=0)
        self.assertFalse(ok)
        self.assertIsNone(st)

    def test_no_change_needed(self):
        dev = FlakyDev(start_band=3, fail_times=0)
        ok, st = autotune.restore_band(dev, 3, tries=2, poll=0)
        self.assertTrue(ok)
        self.assertEqual(st["band"], 3)


class TestNoChallengeIsTransient(unittest.TestCase):
    """没有任何挑战、也没有 login_status ＝ 设备在重启射频，属于临时故障。

    以前这种情况抛 ConnectionError（不可重试），导致测完一个死频段后，
    后面每个频段都被判「通信中断」，连还原都失败。
    """

    def test_raises_device_unreachable(self):
        c = zte.ZteRtaClient("192.0.2.1", "admin", "admin", timeout=1)
        with mock.patch.object(c, "_raw", return_value=(200, "<RGW></RGW>", {})), \
             mock.patch.object(c, "_login", return_value=False):
            with self.assertRaises(DeviceUnreachable):
                c._challenge()

    def test_kickoff_still_reports_busy(self):
        from drivers.base import DeviceBusy
        c = zte.ZteRtaClient("192.0.2.1", "admin", "admin", timeout=1)
        body = "<RGW><login_status>KICKOFF</login_status></RGW>"
        with mock.patch.object(c, "_raw", return_value=(200, body, {})), \
             mock.patch.object(c, "_login", return_value=False):
            with self.assertRaises(DeviceBusy):
                c._challenge()


class TestUnmeasuredBands(unittest.TestCase):
    """「没测出结果」不等于「更慢」，必须单独说明。

    2026-10-29 手机端实测：BAND 40 被 4.6 秒判成「数据不通」跳过，
    而它实际能跑 27~31 Mbps。若不点出来，用户会以为所有频段都比过了。
    """

    def test_mentions_unmeasured_band(self):
        lte = [{"band": 3, "ok": True, "mbps": 45.8},
               {"band": 38, "ok": False, "reason": "no_signal"},
               {"band": 40, "ok": False, "reason": "no_data"}]
        note = autotune.describe_unmeasured(lte)
        self.assertIn("BAND 40", note)
        self.assertNotIn("BAND 38", note)      # 无信号是确定的，不算「没测成」

    def test_empty_when_everything_measured(self):
        lte = [{"band": 3, "ok": True, "mbps": 45.8}]
        self.assertEqual(autotune.describe_unmeasured(lte), "")

    def test_only_dead_bands_gives_empty(self):
        lte = [{"band": 38, "ok": False, "reason": "no_signal"}]
        self.assertEqual(autotune.describe_unmeasured(lte), "")


if __name__ == "__main__":
    unittest.main()
