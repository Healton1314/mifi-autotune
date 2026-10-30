"""交互流程的提问时机：只改信道时也必须问一句。

起因（2026-10-30 用户实机）：结论写着
    信道：11（干扰分 1.58）→ 最优 7（干扰分 0.41）
          切换后干扰下降 1.17（约 74%）
紧接着却直接是「应用方式：python autotune.py --apply-plan」，
中间没有任何「要不要现在改」的提示。

原因是我前一天加了「没建议就别重复问」的早退，而那个判断只看频段的
options（auto/speed/stability），把「频段保持现状、只有信道要改」的
情况一起吞掉了 —— 用户以为工具不让改。
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

    def __init__(self, band=3, channel=11):
        self.band, self.channel = band, channel

    def get_state(self):
        return {"band": self.band, "channel": self.channel,
                "signal": {"rsrp": "-96dBm", "band": str(self.band)}}


class InteractiveAsk(unittest.TestCase):
    def _run(self, plan_obj, answer="", argv=None):
        tmp = Path(tempfile.mkdtemp()) / "plan.json"
        tmp.write_text(json.dumps(plan_obj, ensure_ascii=False), encoding="utf-8")
        called = {}

        def fake_apply(dev, caps, st, args, cfg, host):
            called["strategy"] = getattr(args, "strategy", None)
            return 0

        out = io.StringIO()
        args = argparse.Namespace(**{k: None for k in
                                     ("strategy", "dry_run", "wifi_only", "lte_only")})
        if argv:
            args.strategy = argv
        with mock.patch.object(autotune, "PLAN", tmp), \
                mock.patch.object(autotune, "run_preview", lambda *a, **k: 0), \
                mock.patch.object(autotune, "run_apply_plan", fake_apply), \
                mock.patch("builtins.input", lambda *a: answer), \
                contextlib.redirect_stdout(out):
            rc = autotune._run_interactive(FakeDev(), {}, {"band": 3, "channel": 11},
                                           args, {}, "", "")
        return rc, out.getvalue(), called

    def test_only_channel_asks_and_applies(self):
        plan = {"target": {"band": 3, "channel": 7},
                "options": {"auto": None, "speed": None, "stability": None},
                "changes": ["信道 11 → 7"], "strategy": "auto"}
        rc, text, called = self._run(plan)
        self.assertIn("只改信道", text)
        self.assertTrue(called, "回车后应当去应用，而不是直接结束")
        self.assertEqual(rc, 0)

    def test_only_channel_declined(self):
        plan = {"target": {"band": 3, "channel": 7},
                "options": {"auto": None, "speed": None, "stability": None},
                "changes": ["信道 11 → 7"], "strategy": "auto"}
        rc, text, called = self._run(plan, answer="n")
        self.assertIn("已跳过", text)
        self.assertFalse(called, "输入 n 之后不应该动设备")

    def test_nothing_to_change_is_silent(self):
        plan = {"target": {"band": 3, "channel": 11},
                "options": {"auto": None, "speed": None, "stability": None},
                "changes": [], "strategy": "auto"}
        _rc, text, called = self._run(plan)
        self.assertNotIn("要现在就应用吗", text)   # 结论已说明，不重复问
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
