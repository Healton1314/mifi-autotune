# -*- coding: utf-8 -*-
"""应用后的观察期：抓「刚切能通、过一会儿就断」。

2026-10-30 用户实机：切到 BAND 40 后 3~5 分钟断网几秒。体检时它 20 秒跑出
29.4 Mbps（当时的 BAND 3 只有 12.0），完全看不出这种模式；切完只 ping 6 个包
也看不出来。「峰值多高」和「能不能久待」是两件事，得分开验。
"""

import unittest
from unittest import mock

import autotune


class Clock:
    """每次被调用就前进 step 秒，让观察期的循环次数可预期。"""

    def __init__(self, step=20):
        self.t = 0.0
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def _ping(avg=55, loss=0, jitter=5):
    return lambda *a, **k: {"avg": avg, "loss": loss, "jitter": jitter,
                            "min": avg, "max": avg}


class ObserveAfterApply(unittest.TestCase):
    def _run(self, ping, seconds=60, step=15):
        with mock.patch.object(autotune.time, "time", side_effect=Clock(step)), \
                mock.patch.object(autotune.time, "sleep", lambda s: None), \
                mock.patch.object(autotune.probe, "ping_stats", ping):
            return autotune.observe_after_apply("1.1.1.1", seconds=seconds)

    def test_zero_seconds_skips(self):
        self.assertIsNone(autotune.observe_after_apply("1.1.1.1", seconds=0))

    def test_healthy_run(self):
        v = self._run(_ping())
        self.assertEqual(v["rounds"], 3)        # 60 秒 / 每 15 秒一轮
        self.assertEqual(v["bad"], 0)
        self.assertEqual(v["avg"], 55)

    def test_total_outage_counts_as_bad(self):
        v = self._run(_ping(avg=None))
        self.assertGreaterEqual(v["bad"], 2)
        self.assertIsNone(v["avg"])

    def test_heavy_loss_counts_as_bad(self):
        v = self._run(_ping(loss=80))
        self.assertGreaterEqual(v["bad"], 2)

    def test_mild_loss_is_tolerated(self):
        # 丢包 20%（移动网络正常范围）不该报警，否则每次都说「不稳」
        v = self._run(_ping(loss=20))
        self.assertEqual(v["bad"], 0)


class ApplyReportsUnstable(unittest.TestCase):
    """观察期判断为不稳时，应用流程要给出回滚指引并返回非 0。"""

    def test_unstable_verdict_suggests_restore(self):
        import argparse, contextlib, io, json, tempfile
        from pathlib import Path

        tmp = tempfile.mkdtemp()
        plan = Path(tmp) / "plan.json"
        plan.write_text(json.dumps({
            "current": {"band": 3, "channel": 1},
            "target": {"band": 40, "channel": 1},
            "strategy": "auto", "options": {}, "changes": ["频段 BAND 3 → BAND 40"],
        }, ensure_ascii=False), encoding="utf-8")

        class Dev:
            name = description = "fake"

            def __init__(self):
                self.band, self.channel = 3, 1

            def set_band(self, b):
                self.band = b

            def set_channel(self, c):
                self.channel = c

            def get_state(self):
                return {"band": self.band, "channel": self.channel,
                        "signal": {"rsrp": "-90dBm"}}

            def capabilities(self):
                return {"can_lock_band": True, "can_set_channel": True}

        dev = Dev()
        buf = io.StringIO()
        args = argparse.Namespace(strategy=None, dry_run=False, observe=60)
        with mock.patch.object(autotune, "PLAN", plan), \
                mock.patch.object(autotune, "STATE", Path(tmp) / "state.json"), \
                mock.patch.object(autotune, "save_state", lambda *a, **k: None), \
                mock.patch.object(autotune, "append_log", lambda *a, **k: None), \
                mock.patch.object(autotune, "_mark_applied", lambda *a, **k: None), \
                mock.patch.object(autotune.time, "sleep", lambda s: None), \
                mock.patch.object(autotune.probe, "ping_stats", _ping()), \
                mock.patch.object(autotune, "observe_after_apply",
                                  lambda *a, **k: {"rounds": 3, "bad": 3, "avg": None}), \
                contextlib.redirect_stdout(buf):
            rc = autotune.run_apply_plan(dev, dev.capabilities(), dev.get_state(),
                                         args, {}, "1.1.1.1")
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("撑不住", out)
        self.assertIn("--restore", out)


if __name__ == "__main__":
    unittest.main()
