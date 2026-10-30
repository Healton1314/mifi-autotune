# -*- coding: utf-8 -*-
"""改信道时「设备无响应」不等于失败。

2026-10-30 手机实机：

    [应用] 切换 WiFi 信道到 1 …
    [×] 应用过程中设备无响应：DeviceUnreachable: 192.168.0.1 暂时没有响应
    [!] 已发出改动，但读不回设备状态

而那次信道其实已经切成功了。原因是这台设备的 WiFi 就是手机唯一的通道：
改信道 → 设备重启 AP → 手机先断网 → 紧接着的 get_state() 必然失败。
旧代码把「读不到」当成「改失败」，让用户以为白跑一趟。

这组测试锁住三件事：
  ① 指令发出时连接被掐断，只要事后核对信道对了，就算成功；
  ② 事后核对发现没改成，才报失败并恢复原值；
  ③ 设备一直不回来，要说「连不回去」而不是「应用出错」。
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
from drivers.base import DeviceUnreachable


class FlakyDev:
    """模拟「指令到了、设备重启、连接断掉」的真机行为。"""

    name = "fake"
    description = "假设备"

    def __init__(self, band=3, channel=13, switch_works=True, down_rounds=1):
        self.band = band
        self.channel = channel
        self.switch_works = switch_works
        self.down = down_rounds          # get_state 前几次失败；None = 永远不回来
        self.channels_set = []

    def set_band(self, band):
        self.band = band

    def set_channel(self, ch):
        self.channels_set.append(ch)
        if self.switch_works:
            self.channel = ch
        # 无论成没成，这次请求都在设备重启中被掐断了
        raise DeviceUnreachable("设备 192.168.0.1 暂时没有响应")

    def get_state(self):
        if self.down is None:
            raise DeviceUnreachable("还没连回来")
        if self.down > 0:
            self.down -= 1
            raise DeviceUnreachable("还没连回来")
        return {"band": self.band, "channel": self.channel,
                "signal": {"rsrp": "-90dBm", "band": 3}}

    def capabilities(self):
        return {"bands": [3, 40], "channels": list(range(1, 14)),
                "can_lock_band": True, "can_set_channel": True}


def _args(**kw):
    d = dict(strategy=None, dry_run=False, interactive=False)
    d.update(kw)
    return argparse.Namespace(**d)


class ChannelSwitchSurvivesDisconnect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan = Path(self.tmp) / "plan.json"
        self.plan.write_text(json.dumps({
            "current": {"band": 3, "channel": 13},
            "target": {"band": 3, "channel": 1},
            "strategy": "auto",
            "options": {},
            "changes": ["信道 13 → 1"],
            "expected": {},
        }, ensure_ascii=False), encoding="utf-8")

    def _run(self, dev, wait_ok=None):
        """wait_ok=None 走真实的 wait_device_back；给值则注入结果。

        注入是必需的：真实 wait_device_back 的超时按墙钟算，而测试里把
        sleep 变成 no-op 之后，验证「一直回不来」就得真的等 90 秒（超时被杀过）。
        超时逻辑本身由 WaitDeviceBack 那组用例单独覆盖。
        """
        st0 = {"band": dev.band, "channel": 13}
        buf = io.StringIO()
        patches = [
            mock.patch.object(autotune, "PLAN", self.plan),
            mock.patch.object(autotune, "STATE", Path(self.tmp) / "state.json"),
            mock.patch.object(autotune, "save_state", lambda *a, **k: None),
            mock.patch.object(autotune, "append_log", lambda *a, **k: None),
            mock.patch.object(autotune, "_mark_applied", lambda *a, **k: None),
            mock.patch.object(autotune.time, "sleep", lambda s: None),
            contextlib.redirect_stdout(buf),
        ]
        if wait_ok is not None:
            patches.append(mock.patch.object(autotune, "wait_device_back",
                                             lambda *a, **k: wait_ok))
        with contextlib.ExitStack() as stack:
            for pt in patches:
                stack.enter_context(pt)
            code = autotune.run_apply_plan(dev, dev.capabilities(), st0,
                                           _args(), {}, "223.5.5.5")
        return code, buf.getvalue()

    def test_disconnect_after_send_is_not_a_failure(self):
        dev = FlakyDev(switch_works=True)
        code, out = self._run(dev)
        self.assertEqual(dev.channels_set, [1])
        self.assertEqual(dev.channel, 1)
        self.assertIn("指令已发出", out)          # 断连被解释成「正常现象」
        self.assertIn("已生效", out)
        self.assertNotIn("应用过程中设备无响应", out)   # 旧文案，会让人以为失败
        self.assertNotIn("读不回设备状态", out)
        self.assertEqual(code, 0)

    def test_channel_really_not_changed_rolls_back(self):
        dev = FlakyDev(switch_works=False)
        code, out = self._run(dev)
        self.assertIn("信道未生效", out)
        self.assertEqual(dev.channels_set, [1, 13])   # 试过恢复原值

    def test_device_never_comes_back_says_so(self):
        dev = FlakyDev(switch_works=True, down_rounds=None)
        code, out = self._run(dev, wait_ok=False)
        self.assertIn("90 秒还没连回来", out)
        self.assertIn("--status", out)          # 给的是下一步动作，不是 traceback
        self.assertNotIn("应用过程中设备无响应", out)


class WaitDeviceBack(unittest.TestCase):
    def test_returns_true_once_reachable(self):
        dev = FlakyDev(down_rounds=2)
        with mock.patch.object(autotune.time, "sleep", lambda s: None):
            self.assertTrue(autotune.wait_device_back(dev, timeout=60, poll=5))

    def test_returns_false_when_timeout(self):
        dev = FlakyDev(down_rounds=None)
        with mock.patch.object(autotune.time, "sleep", lambda s: None), \
                mock.patch.object(autotune.time, "time",
                                  side_effect=[0, 0, 100, 100, 100, 100, 100]):
            self.assertFalse(autotune.wait_device_back(dev, timeout=10, poll=1))


if __name__ == "__main__":
    unittest.main()
