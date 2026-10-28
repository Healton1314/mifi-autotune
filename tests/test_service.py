"""「无信号频段要尽快跳过」的测试。

背景：设备声明支持 6 个频段，但运营商在当地可能只部署了 2 个。
原来的实现是固定 sleep 26 秒再打一轮包，等于在每个死频段上白等半分钟，
6 个频段里如果有 2~3 个没信号，光等待就浪费一分多钟。

wait_for_service 改成轮询设备状态：拿到可用 RSRP 就立刻开始测，
连续几次都没有可用信号就判定"当地没这个频段"并提前返回。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autotune  # noqa: E402

FAST = dict(step=0.01, timeout=0.3, dead_confirm=3)


class FakeDev:
    """按预设序列返回状态；序列用完后一直重复最后一个。"""

    def __init__(self, states, raise_always=False):
        self.states = states
        self.i = 0
        self.raise_always = raise_always

    def get_state(self):
        if self.raise_always:
            raise OSError("device unreachable")
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return s


def state(band=3, rsrp="-95dBm"):
    return {"band": band, "channel": 13, "signal": {"rsrp": rsrp, "sinr": "12dB"}}


class TestSignalUsable(unittest.TestCase):
    def test_normal_value(self):
        self.assertTrue(autotune._signal_usable({"signal": {"rsrp": "-95dBm"}}))
        self.assertTrue(autotune._signal_usable({"signal": {"rsrp": "-105"}}))

    def test_no_service_values(self):
        for bad in ["", "0", "0dBm", "--", "NA", None]:
            self.assertFalse(autotune._signal_usable({"signal": {"rsrp": bad}}),
                             f"{bad!r} 不该算有信号")

    def test_missing_signal_block(self):
        self.assertFalse(autotune._signal_usable({}))


class TestWaitForService(unittest.TestCase):
    def test_registers_immediately(self):
        dev = FakeDev([state(band=40, rsrp="-88dBm")])
        status, waited = autotune.wait_for_service(dev, 40, **FAST)
        self.assertEqual(status, "ok")
        self.assertLess(waited, 0.3)

    def test_dead_band_skipped_early(self):
        """一直报无效 RSRP → 应该判 no_signal，而不是耗到 timeout。"""
        dev = FakeDev([state(band=38, rsrp="0")])
        status, waited = autotune.wait_for_service(dev, 38, **FAST)
        self.assertEqual(status, "no_signal")
        self.assertLess(waited, 0.3)

    def test_wrong_band_times_out(self):
        """有信号但一直停在别的频段 → 说明锁频段没生效，判 timeout。"""
        dev = FakeDev([state(band=3, rsrp="-90dBm")])
        status, _ = autotune.wait_for_service(dev, 40, **FAST)
        self.assertEqual(status, "timeout")

    def test_device_errors_do_not_crash(self):
        dev = FakeDev([], raise_always=True)
        status, _ = autotune.wait_for_service(dev, 40, **FAST)
        self.assertEqual(status, "timeout")

    def test_band_zero_means_no_target(self):
        """band=0（不锁定）时只要拿到可用信号即可。"""
        dev = FakeDev([state(band=3, rsrp="-92dBm")])
        status, _ = autotune.wait_for_service(dev, 0, **FAST)
        self.assertEqual(status, "ok")


if __name__ == "__main__":
    unittest.main()
