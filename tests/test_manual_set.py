# -*- coding: utf-8 -*-
"""手动指定频段 / 信道（不经体检、不裁决，只写一次）。

这条路的由来是用户的一句话：「BAND 40 晚上丢包，只要 BAND 3，别让我再等
6 分钟体检、再被『当前这条更快』说服一遍。」

它和体检的区别决定了这里必须锁住的三件事：
  ① **0 是合法值**（解除锁定 / 交回自动）。`if args.lock_band:` 会把 0 当假值
     吞掉，界面上点「自动」就变成毫无反应 —— 这是最容易回归的一处。
  ② **解锁不能拿服务频段去比对**。锁定值变 0 才是成功，而服务频段一直都在，
     误比对会永远报「没切成功」。
  ③ **改信道会把自己掐断**。手机就是通过这台设备的 WiFi 操作的，指令发出后
     读不回状态是必然现象，不能当失败（10-30 那天已经踩过一次）。
"""

import argparse
import contextlib
import io
import unittest
from unittest import mock

import autotune
from drivers.base import DeviceUnreachable


class FakeDev:
    """够用的假设备：能读状态、能被设置，可选择「设置后断连」。"""

    name = "fake"
    description = "假设备"

    def __init__(self, band=8, channel=13, apply_works=True, drop_after_set=True):
        self.band = band
        self.channel = channel
        self.apply_works = apply_works
        self.drop_after_set = drop_after_set
        self.set_calls = []

    def set_band(self, band):
        self.set_calls.append(("band", band))
        if self.apply_works:
            self.band = band
        if self.drop_after_set:
            raise DeviceUnreachable("换频段重启射频，连接断了")

    def set_channel(self, ch):
        self.set_calls.append(("channel", ch))
        if self.apply_works:
            self.channel = ch
        if self.drop_after_set:
            raise DeviceUnreachable("改信道重启 AP，连接断了")

    def get_state(self):
        return {"band": self.band, "channel": self.channel,
                "signal": {"pci": "236", "band": "8", "operator": "CMCC",
                           "rsrp": "-90dBm"}}


def _ns(**kw):
    d = dict(lock_band=None, set_channel=None)
    d.update(kw)
    return argparse.Namespace(**d)


def _run(fn, dev, args, wait_ok=True):
    """跑一条命令，顺便把 stdout 收回来（断言文案要用）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
            mock.patch.object(autotune, "wait_device_back", return_value=wait_ok):
        code = fn(dev, args)
    return code, buf.getvalue()


class SetChannel(unittest.TestCase):
    def test_applies_even_if_link_drops(self):
        """指令发出后连接被掐断，只要事后核对对了就算成功。"""
        dev = FakeDev(channel=13)
        code, out = _run(autotune.run_set_channel, dev, _ns(set_channel=1))
        self.assertEqual(code, 0)
        self.assertEqual(dev.channel, 1)
        self.assertIn("已切到信道 1", out)

    def test_auto_is_zero_not_none(self):
        """0 = 自动，必须被当成一个真实的命令执行掉。"""
        dev = FakeDev(channel=6)
        code, out = _run(autotune.run_set_channel, dev, _ns(set_channel=0))
        self.assertEqual(code, 0)
        self.assertEqual(dev.set_calls, [("channel", 0)])
        self.assertIn("已切到自动", out)

    def test_reports_when_device_did_not_take_it(self):
        """设备没接受（读回来还是旧值）要如实报，不能假装成功。"""
        dev = FakeDev(channel=13, apply_works=False)
        code, out = _run(autotune.run_set_channel, dev, _ns(set_channel=5))
        self.assertEqual(code, 1)
        self.assertIn("读回来是信道 13", out)

    def test_says_unreachable_instead_of_failure(self):
        """设备一直没回来：说「连不回去」，别说「设置失败」。"""
        dev = FakeDev(channel=13)
        code, out = _run(autotune.run_set_channel, dev, _ns(set_channel=3), wait_ok=False)
        self.assertEqual(code, 1)
        self.assertIn("60 秒没回来", out)


class LockBandZero(unittest.TestCase):
    """band=0 是「解除锁定」，走的是一条专门的验收分支。"""

    def test_unlock_succeeds_even_though_serving_band_is_not_zero(self):
        """设备此刻驻留 BAND 8 是正常的；锁定值归零就算解锁成功。"""
        dev = FakeDev(band=0)
        dev.band = 0
        with mock.patch.object(autotune, "_clear_device_pin") as cleared, \
                mock.patch.object(autotune, "set_device_pin") as pinned:
            code, out = _run(autotune.run_lock_band, dev, _ns(lock_band=0))
        self.assertEqual(code, 0)
        self.assertIn("已解除锁定", out)
        cleared.assert_called_once()
        # 解锁不能反过来又记一条 pin，否则守护下次启动会把它重新锁回去
        pinned.assert_not_called()

    def test_unlock_fails_loudly_when_lock_value_still_set(self):
        # apply_works=False：设备把这条指令吞了，锁定值还是 3。这时候必须报出来，
        # 否则用户以为已经交回自动，实际还被锁在旧频段上。
        dev = FakeDev(band=3, apply_works=False)
        code, out = _run(autotune.run_lock_band, dev, _ns(lock_band=0))
        self.assertEqual(code, 1)
        self.assertIn("可能没解除", out)

    def test_lock_to_band_still_pins(self):
        """普通锁定（band=3）不能被上面的改动带坏：还是要记 pin。"""
        dev = FakeDev(band=3)
        with mock.patch.object(autotune, "_clear_device_pin") as cleared, \
                mock.patch.object(autotune, "set_device_pin") as pinned, \
                mock.patch.object(autotune, "pin_site_best"):
            code, out = _run(autotune.run_lock_band, dev, _ns(lock_band=3))
        self.assertEqual(code, 0)
        self.assertIn("已锁定 BAND 3", out)
        self.assertEqual(pinned.call_args[0][0], 3)
        cleared.assert_not_called()


class StatusPingCache(unittest.TestCase):
    """状态轮询的延迟数字必须缓存，否则那条调用线程永远被占着。

    实测代价：`ping -c 6` 在电脑上是 6 秒；安卓没有 ICMP 权限、退回 6 次
    TCP 握手，也要好几秒。而界面每 5 秒问一次状态 —— 不缓存的话，进度查询
    和「任务结束该复位按钮了」永远排在它后面，用户看到的就是
    「点了没反应 / 跑完了按钮还挂着『正在…』」。
    """

    def setUp(self):
        import server
        self.server = server
        server._PING_CACHE.update(ts=0.0, key=None, val=None)

    def test_second_call_hits_cache(self):
        with mock.patch.object(self.server.probe, "ping_stats",
                               return_value={"avg": 50, "loss": 0}) as m:
            a = self.server._cached_ping(3, "223.5.5.5")
            b = self.server._cached_ping(3, "223.5.5.5")
        self.assertEqual(a, b)
        self.assertEqual(m.call_count, 1, "10 秒内不该再测一次")

    def test_different_host_or_count_misses(self):
        with mock.patch.object(self.server.probe, "ping_stats",
                               return_value={"avg": 50, "loss": 0}) as m:
            self.server._cached_ping(3, "223.5.5.5")
            self.server._cached_ping(6, "223.5.5.5")   # 包数不同 = 新的一次
            self.server._cached_ping(3, "119.29.29.29")
        self.assertEqual(m.call_count, 3)

    def test_expires(self):
        with mock.patch.object(self.server.probe, "ping_stats",
                               return_value={"avg": 50, "loss": 0}) as m:
            self.server._cached_ping(3, "223.5.5.5")
            self.server._cached_ping(3, "223.5.5.5", ttl=0)
        self.assertEqual(m.call_count, 2, "缓存过期后要重新测")


if __name__ == "__main__":
    unittest.main()
